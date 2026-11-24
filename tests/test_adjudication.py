"""裁决边界规则测试：同人、容量、替代折算、销售时点、取值域。"""
import csv
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from sourcepack.ingestion import read_source_file
from sourcepack.model import ACCEPTED, DISPUTED, adjudicate, build_domain
from sourcepack.warehouse import Warehouse

from fixtures import base_dataset, write_dataset

ALL_TABLES = [
    "packages", "slots", "requests", "executions",
    "fulfillment_links", "substitutions", "refunds", "cash",
]


def build_warehouse(dataset):
    tmp = tempfile.TemporaryDirectory()
    folder = Path(tmp.name) / "in"
    folder.mkdir()
    write_dataset(folder, dataset)
    wh = Warehouse(Path(tmp.name) / "w.duckdb")
    wh.ingest_directory(folder)
    rows = {name: wh.ingestion.current_rows(name) for name in ALL_TABLES}
    return wh, tmp, build_domain(rows)


def dataset_domain(dataset, *, bypass_reference_for=None):
    """在内存仓构建领域对象；bypass_reference_for 的表跳过引用检查直写原始层。"""
    wh = Warehouse(":memory:")
    for name, spec in dataset.items():
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / f"{name}.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=spec["columns"])
                writer.writeheader()
                writer.writerows(spec["rows"])
            if name in (bypass_reference_for or set()):
                import datetime
                from sourcepack.schema import TABLES
                parsed = read_source_file(path, name)
                seq = wh.ingestion._next_seq()
                wh.con.execute(
                    "INSERT INTO recv_batch VALUES (?, ?, ?)",
                    [seq, datetime.datetime.now(datetime.timezone.utc), name],
                )
                cols = [c["name"] for c in TABLES[name]["columns"]]
                for row in parsed:
                    wh.con.execute(
                        f'INSERT INTO raw_{name} (batch_seq, '
                        + ", ".join(f'"{c}"' for c in cols)
                        + ") VALUES (?, " + ", ".join("?" * len(cols)) + ")",
                        [seq] + [row[c] for c in cols],
                    )
            else:
                wh.ingest_file(path, name)
    rows = {name: wh.ingestion.current_rows(name) for name in ALL_TABLES}
    return wh, build_domain(rows)


class AdjudicationRuleTests(unittest.TestCase):
    def test_person_mismatch_is_disputed(self):
        dataset = base_dataset()
        dataset["fulfillment_links"]["rows"] = [
            {"link_id": "F9", "execution_id": "X1", "slot_id": "S4", "units": "1"},
        ]
        wh, tmp, domain = build_warehouse(dataset)
        facts = adjudicate(domain)
        self.assertEqual(facts["F9"]["status"], DISPUTED)
        self.assertIn("person_mismatch", facts["F9"]["hard_reasons"])
        wh.close()
        tmp.cleanup()

    def test_execution_before_sale_flagged(self):
        dataset = base_dataset()
        dataset["packages"]["rows"][0] = dict(
            dataset["packages"]["rows"][0], sold_at="2026-10-20T08:00:00+08:00"
        )
        dataset["fulfillment_links"]["rows"] = [
            {"link_id": "F3", "execution_id": "X2", "slot_id": "S2", "units": "1"},
        ]
        wh, tmp, domain = build_warehouse(dataset)
        facts = adjudicate(domain)
        self.assertEqual(facts["F3"]["status"], DISPUTED)
        self.assertIn("execution_before_sale", facts["F3"]["hard_reasons"])
        wh.close()
        tmp.cleanup()

    def test_slot_capacity_exceeded(self):
        dataset = base_dataset()
        # S4 总量 2，两条同人引用各主张 1.5，合计 3 超出容量
        dataset["fulfillment_links"]["rows"] = [
            {"link_id": "F4", "execution_id": "X3", "slot_id": "S4", "units": "1.5"},
            {"link_id": "F6", "execution_id": "X3", "slot_id": "S4", "units": "1.5"},
        ]
        wh, tmp, domain = build_warehouse(dataset)
        facts = adjudicate(domain)
        self.assertEqual(facts["F4"]["status"], DISPUTED)
        self.assertEqual(facts["F6"]["status"], DISPUTED)
        # F4 先到但执行容量（X3 仅 1 单位）即超；F6 同时触发权益容量
        self.assertIn("execution_capacity_exceeded", facts["F4"]["hard_reasons"])
        self.assertIn("slot_capacity_exceeded", facts["F6"]["hard_reasons"])
        wh.close()
        tmp.cleanup()

    def test_substitution_unit_ratio_consumption(self):
        dataset = base_dataset()
        dataset["substitutions"]["rows"][0]["unit_ratio"] = "2"
        dataset["executions"]["rows"] = [
            {"execution_id": "X3", "request_id": "R3", "person": "P2",
             "service_code": "B_ALT", "team": "T2", "units": "2",
             "at": "2026-10-14T08:00:00+08:00", "status": "confirmed"},
        ]
        dataset["fulfillment_links"]["rows"] = [
            {"link_id": "F4", "execution_id": "X3", "slot_id": "S4", "units": "1"},
        ]
        wh, tmp, domain = build_warehouse(dataset)
        facts = adjudicate(domain)
        self.assertEqual(facts["F4"]["status"], ACCEPTED)
        self.assertEqual(facts["F4"]["execution_consumed_units"], Decimal("0.5"))
        wh.close()
        tmp.cleanup()

    def test_missing_reference_preserved_as_disputed(self):
        dataset = base_dataset()
        dataset["fulfillment_links"]["rows"].append(
            {"link_id": "F7", "execution_id": "GHOST", "slot_id": "S2", "units": "1"}
        )
        wh, domain = dataset_domain(dataset, bypass_reference_for={"fulfillment_links"})
        facts = adjudicate(domain)
        self.assertEqual(facts["F7"]["status"], DISPUTED)
        self.assertIn("missing_reference", facts["F7"]["hard_reasons"])
        wh.close()

    def test_unknown_domain_value_rejected_at_ingestion(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "executions.csv"
            path.write_text(
                "execution_id,request_id,person,service_code,team,units,at,status\n"
                "X1,R1,P1,A,T1,1,2026-10-10T08:00:00+08:00,done\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "取值域"):
                read_source_file(path, "executions")


if __name__ == "__main__":
    unittest.main()
