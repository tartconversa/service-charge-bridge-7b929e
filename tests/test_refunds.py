"""退款分类与现金匹配的次序、边界测试。"""
import tempfile
import unittest
from pathlib import Path

from sourcepack.model import build_domain, classify_refunds, compute_positions, match_cash, adjudicate
from sourcepack.warehouse import Warehouse

from fixtures import base_dataset, write_dataset

ALL_TABLES = [
    "packages", "slots", "requests", "executions",
    "fulfillment_links", "substitutions", "refunds", "cash",
]


def build(dataset):
    tmp = tempfile.TemporaryDirectory()
    folder = Path(tmp.name) / "in"
    folder.mkdir()
    write_dataset(folder, dataset)
    wh = Warehouse(Path(tmp.name) / "w.duckdb")
    wh.ingest_directory(folder)
    rows = {name: wh.ingestion.current_rows(name) for name in ALL_TABLES}
    return wh, tmp, build_domain(rows)


class RefundOrderingTests(unittest.TestCase):
    def test_multiple_refunds_consume_available_then_dispute(self):
        dataset = base_dataset()
        # S4 总量 2、无任何已核实兑现：第一笔退 1 干净，第二笔退 2 时只剩 1
        dataset["refunds"]["rows"] = [
            {"refund_id": "RA", "slot_id": "S4", "units": "1", "amount": "200",
             "at": "2026-10-20T08:00:00+08:00"},
            {"refund_id": "RB", "slot_id": "S4", "units": "2", "amount": "400",
             "at": "2026-10-21T08:00:00+08:00"},
        ]
        dataset["cash"]["rows"] = [
            r for r in dataset["cash"]["rows"] if r["kind"] == "receive"
        ] + [
            {"cash_id": "C6", "package_id": "PK3", "kind": "refund", "amount": "200",
             "at": "2026-10-20T08:00:00+08:00"},
            {"cash_id": "C7", "package_id": "PK3", "kind": "refund", "amount": "400",
             "at": "2026-10-21T08:00:00+08:00"},
        ]
        wh, tmp, domain = build(dataset)
        facts = adjudicate(domain)
        refunds, units_map = classify_refunds(domain, facts)
        by_id = {r["refund_id"]: r for r in refunds}
        self.assertEqual(float(by_id["RA"]["clean_units"]), 1.0)
        self.assertEqual(float(by_id["RA"]["disputed_units"]), 0.0)
        self.assertEqual(float(by_id["RB"]["clean_units"]), 1.0)
        self.assertEqual(float(by_id["RB"]["disputed_units"]), 1.0)
        wh.close()
        tmp.cleanup()

    def test_refund_contract_components_sum_to_slot_contract(self):
        wh, tmp, domain = build(base_dataset())
        facts = adjudicate(domain)
        slot_positions, _, refund_facts, _ = compute_positions(domain, facts)
        for pos in slot_positions:
            total = (
                pos["verified_contract_cents"]
                + pos["clean_refund_contract_cents"]
                + pos["disputed_refund_contract_cents"]
                + pos["unfulfilled_contract_cents"]
            )
            self.assertEqual(total, pos["contract_cents"])
        # 退款资格额各成分之和与 slot 成分恒等
        for slot_id in {r["slot_id"] for r in refund_facts}:
            rows = [r for r in refund_facts if r["slot_id"] == slot_id]
            pos = next(p for p in slot_positions if p["slot_id"] == slot_id)
            self.assertEqual(
                sum(r["clean_contract_cents"] for r in rows),
                pos["clean_refund_contract_cents"],
            )
            self.assertEqual(
                sum(r["disputed_contract_cents"] for r in rows),
                pos["disputed_refund_contract_cents"],
            )
        wh.close()
        tmp.cleanup()


class CashMatchingTests(unittest.TestCase):
    def test_ambiguous_and_unmatched_cash_kept_for_review(self):
        dataset = base_dataset()
        # 两笔同包同额同时点的退款事实与两笔同额现金：无法唯一一一对应
        dataset["refunds"]["rows"] = [
            {"refund_id": "RA", "slot_id": "S1", "units": "1", "amount": "240",
             "at": "2026-10-16T08:00:00+08:00"},
            {"refund_id": "RB", "slot_id": "S1", "units": "1", "amount": "240",
             "at": "2026-10-16T08:00:00+08:00"},
        ]
        dataset["cash"]["rows"] = [
            r for r in dataset["cash"]["rows"] if r["kind"] == "receive"
        ] + [
            {"cash_id": "C8", "package_id": "PK1", "kind": "refund", "amount": "240",
             "at": "2026-10-16T08:00:00+08:00"},
            {"cash_id": "C9", "package_id": "PK1", "kind": "refund", "amount": "240",
             "at": "2026-10-16T08:00:00+08:00"},
        ]
        wh, tmp, domain = build(dataset)
        facts = adjudicate(domain)
        _, _, refund_facts, _ = compute_positions(domain, facts)
        cash_facts = match_cash(domain, refund_facts)
        statuses = {r["cash_id"]: r["match_status"] for r in cash_facts}
        self.assertEqual(statuses["C8"], "ambiguous")
        self.assertEqual(statuses["C9"], "ambiguous")
        wh.close()
        tmp.cleanup()

    def test_refund_without_cash_evidence_flagged(self):
        dataset = base_dataset()
        dataset["cash"]["rows"] = [r for r in dataset["cash"]["rows"] if r["kind"] == "receive"]
        wh, tmp, domain = build(dataset)
        facts = adjudicate(domain)
        _, _, refund_facts, _ = compute_positions(domain, facts)
        match_cash(domain, refund_facts)
        self.assertTrue(all(r["cash_match_status"] == "missing_cash_evidence" for r in refund_facts))
        wh.close()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
