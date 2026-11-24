"""业务事实仓测试：裁决、分摊、退款、对账、接收修订与可核对恒等式。"""
import json
import tempfile
import unittest
from pathlib import Path

from sourcepack.warehouse import Warehouse

from fixtures import BASE_TABLES, base_dataset, make_warehouse, write_csv, write_dataset


class WarehouseCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name) / "in"
        self.folder.mkdir()
        self.db = Path(self._tmp.name) / "w.duckdb"
        self.wh, _ = make_warehouse(self.folder, self.db)

    def tearDown(self):
        self.wh.close()
        self._tmp.cleanup()

    def link(self, link_id):
        rows = self.wh.links()
        return next(r for r in rows if r["link_id"] == link_id)

    def slot(self, slot_id):
        return next(r for r in self.wh.slot_positions() if r["slot_id"] == slot_id)

    def package(self, package_id):
        return next(r for r in self.wh.package_positions() if r["package_id"] == package_id)

    # ---- 裁决 ----------------------------------------------------------
    def test_exclusive_group_both_disputed_no_first_writer(self):
        f1, f2 = self.link("F1"), self.link("F2")
        self.assertEqual(f1["status"], "disputed")
        self.assertEqual(f2["status"], "disputed")
        self.assertTrue(any("exclusive_conflict" in x for x in f1["reasons"]))
        # 执行容量也被重复主张超用
        self.assertIn("execution_capacity_exceeded", f2["reasons"])

    def test_confirmed_execution_accepted_pending_kept_pending(self):
        self.assertEqual(self.link("F3")["status"], "accepted")
        self.assertEqual(self.link("F4")["status"], "pending")
        self.assertIn("execution_pending", self.link("F4")["reasons"])

    def test_cancelled_request_does_not_delete_execution(self):
        rows = self.wh.department_volume()
        x1 = next(r for r in rows if r["execution_id"] == "X1")
        self.assertEqual(x1["status"], "confirmed")
        self.assertEqual(x1["request_state"], "cancelled")

    # ---- 合同分摊 ------------------------------------------------------
    def test_fixed_denominator_contract_allocation(self):
        self.assertEqual(self.slot("S1")["contract_cents"], 48000)
        self.assertEqual(self.slot("S2")["contract_cents"], 32000)

    def test_partial_fulfillment_keeps_remainder_in_package(self):
        # F3 是 PK1 唯一已核实引用：只分 S2 的一半，另一半留包，不重新归一
        s2 = self.slot("S2")
        self.assertEqual(s2["verified_contract_cents"], 16000)
        pk1 = self.package("PK1")
        self.assertEqual(pk1["verified_contract_cents"], 16000)
        self.assertGreater(pk1["unfulfilled_contract_cents"], 0)

    def test_slot_position_components_sum_to_contract(self):
        for slot in self.wh.slot_positions():
            total = (
                slot["verified_contract_cents"]
                + slot["clean_refund_contract_cents"]
                + slot["disputed_refund_contract_cents"]
                + slot["unfulfilled_contract_cents"]
            )
            self.assertEqual(total, slot["contract_cents"], slot["slot_id"])

    def test_package_components_sum_to_price(self):
        for pkg in self.wh.package_positions():
            total = (
                pkg["verified_contract_cents"]
                + pkg["clean_refund_contract_cents"]
                + pkg["disputed_refund_contract_cents"]
                + pkg["unfulfilled_contract_cents"]
            )
            self.assertEqual(total, pkg["price_cents"], pkg["package_id"])

    def test_unresolved_memo_does_not_double_count_refunded_units(self):
        # S2 的争议引用单位已被已核实+争议退款占用，备忘不得再计一次
        s2 = self.slot("S2")
        self.assertEqual(s2["unresolved_contract_cents_memo"], 0)
        # S1 的争议单位尚未被退款全部覆盖，保留提示额
        self.assertEqual(self.slot("S1")["unresolved_contract_cents_memo"], 24000)

    # ---- 退款 ----------------------------------------------------------
    def test_refund_classification_clean_vs_touches_fulfilled(self):
        refunds = {r["refund_id"]: r for r in self.wh.refunds()}
        rf1, rf2 = refunds["RF1"], refunds["RF2"]
        # RF1 撤回的是 S1 未使用单位（已兑现在争议中未核实）
        self.assertEqual(float(rf1["clean_units"]), 1.0)
        self.assertEqual(float(rf1["disputed_units"]), 0.0)
        # RF2 撤回的 S2 已被 F3 兑现：争议金额保留，不倒删服务
        self.assertEqual(float(rf2["clean_units"]), 0.0)
        self.assertEqual(float(rf2["disputed_units"]), 1.0)
        self.assertIn("touches_fulfilled_entitlement", rf2["review_flags"])

    def test_refund_actual_vs_entitlement_difference_listed(self):
        rf2 = next(r for r in self.wh.refunds() if r["refund_id"] == "RF2")
        self.assertEqual(rf2["entitlement_cents"], 16000)
        self.assertEqual(rf2["actual_amount_cents"], 32000)
        self.assertEqual(rf2["difference_cents"], 16000)

    def test_refund_does_not_reverse_department_service_count(self):
        summary = {
            (r["service_date"], r["team"]): r
            for r in self.wh.department_summary()
        }
        row = summary[("2026-10-12", "T2")]
        self.assertEqual(float(row["confirmed_units"]), 1.0)

    def test_cash_and_refund_matched_but_not_doubled(self):
        cash = {r["cash_id"]: r for r in self.wh.cash_flows()}
        self.assertEqual(cash["C4"]["match_status"], "matched")
        self.assertEqual(cash["C4"]["matched_refund_id"], "RF1")
        self.assertEqual(cash["C5"]["matched_refund_id"], "RF2")
        pk1 = self.package("PK1")
        # 现金净额与合同归属分列，不重复计退
        self.assertEqual(pk1["refunded_cash_cents"], 56000)
        self.assertEqual(pk1["net_cash_cents"], 24000)

    # ---- 缺证 ----------------------------------------------------------
    def test_evidence_gaps_both_sides_retained(self):
        gaps = self.wh.evidence_gaps()
        ids = {r["execution_id"] for r in gaps["executions_without_verified_fulfillment"]}
        self.assertEqual(ids, {"X1", "X3"})
        slot_ids = {r["slot_id"] for r in gaps["slots_without_verified_fulfillment"]}
        self.assertEqual(slot_ids, {"S1", "S3", "S4"})

    # ---- 科室视角 ------------------------------------------------------
    def test_department_filters_and_drilldown(self):
        rows = self.wh.department_volume(team="T2", service_date_from="2026-10-12", service_date_to="2026-10-14")
        self.assertEqual({r["execution_id"] for r in rows}, {"X2"})
        # 同一条履约关系可下钻
        links = self.wh.links(execution_id="X2")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["effective_slot_id"], "S2")

    # ---- 接收与修订 ----------------------------------------------------
    def test_same_digest_is_unchanged(self):
        result = self.wh.ingest_directory(self.folder)
        self.assertTrue(all(r["status"] == "unchanged" for r in result))

    def test_same_key_revision_keeps_history_and_changes_current(self):
        path = self.folder / "packages.csv"
        dataset = base_dataset()
        rows = dataset["packages"]["rows"]
        rows[0] = dict(rows[0], price="900")
        write_csv(path.parent, "packages", rows, dataset["packages"]["columns"])
        result = self.wh.ingest_file(path, "packages", label="revise-price")
        self.assertEqual(result["status"], "loaded")
        self.wh.rebuild()
        self.assertEqual(self.package("PK1")["price_cents"], 90000)
        # 历史批次与原始行仍可追溯
        raw = self.wh.con.execute(
            "SELECT batch_seq, price FROM raw_packages WHERE package_id = 'PK1' ORDER BY batch_seq"
        ).fetchall()
        self.assertEqual([r[1] for r in raw], ["800", "900"])

    def test_broken_reference_rejected(self):
        path = self.folder / "slots.csv"
        dataset = base_dataset()
        rows = [dict(dataset["slots"]["rows"][0], package_id="NOPE")] + dataset["slots"]["rows"][1:]
        write_csv(path.parent, "slots", rows, dataset["slots"]["columns"])
        with self.assertRaisesRegex(ValueError, "资料引用不存在"):
            self.wh.ingest_file(path, "slots")

    # ---- 人工对账 ------------------------------------------------------
    def test_reconciliation_round_decision_and_snapshot_diff(self):
        round_id = self.wh.open_round()
        # 人工将 F1 判为 disputed（维持冲突结论但留痕），F2 改引 S1 不可行（互斥仍在）
        self.wh.record_decision(round_id, "F1", "disputed", reason="互斥待核")
        diff = self.wh.confirm_round(round_id)
        self.assertIn("fact_fulfillment_link", diff["changes"])
        rounds = self.wh.list_rounds()
        self.assertEqual(rounds[-1]["status"], "confirmed")
        # 已确认轮次不可再改
        with self.assertRaisesRegex(ValueError, "已确认"):
            self.wh.record_decision(round_id, "F1", "accepted")

    def test_reassign_slot_rebuilds_detail_not_just_summary(self):
        # 构造干净场景：X1 只引 S3（PK2），confirmed、适配、无互斥
        dataset = base_dataset()
        dataset["fulfillment_links"]["rows"] = [
            {"link_id": "F2", "execution_id": "X1", "slot_id": "S3", "units": "1"},
        ]
        with tempfile.TemporaryDirectory() as d2:
            folder2 = Path(d2) / "in"
            folder2.mkdir()
            db2 = Path(d2) / "w.duckdb"
            wh2, _ = make_warehouse(folder2, db2, dataset)
            self.assertEqual(
                next(r for r in wh2.links() if r["link_id"] == "F2")["effective_slot_id"],
                "S3",
            )
            round_id = wh2.open_round()
            wh2.record_decision(round_id, "F2", "accepted", new_slot_id="S1", reason="错引改包")
            wh2.confirm_round(round_id)
            fact = next(r for r in wh2.links() if r["link_id"] == "F2")
            # 明细级 effective_slot 已改到新包，来源 slot 保留可对照
            self.assertEqual(fact["source_slot_id"], "S3")
            self.assertEqual(fact["effective_slot_id"], "S1")
            self.assertEqual(fact["status"], "accepted")
            wh2.close()

    def test_manual_accept_cannot_override_pending_evidence(self):
        round_id = self.wh.open_round()
        self.wh.record_decision(round_id, "F4", "accepted", reason="尝试强确认")
        self.wh.confirm_round(round_id)
        self.assertEqual(self.link("F4")["status"], "pending")
        self.assertIn("manual_accept_blocked_pending_evidence", self.link("F4")["reasons"])

    # ---- 重建可重复 ----------------------------------------------------
    def test_rebuild_is_deterministic(self):
        first = json.dumps(self.wh.links(), sort_keys=True, ensure_ascii=False)
        self.wh.rebuild()
        second = json.dumps(self.wh.links(), sort_keys=True, ensure_ascii=False)
        self.assertEqual(first, second)

    def test_as_of_rebuild_ignores_later_revision(self):
        path = self.folder / "packages.csv"
        dataset = base_dataset()
        rows = dataset["packages"]["rows"]
        rows[0] = dict(rows[0], price="900")
        write_csv(path.parent, "packages", rows, dataset["packages"]["columns"])
        self.wh.ingest_file(path, "packages", label="revise")
        before = self.wh.ingestion.current_rows("packages", as_of_seq=1)
        self.assertEqual(before[0]["price"], "800")
        after = self.wh.ingestion.current_rows("packages")
        self.assertEqual(after[0]["price"], "900")


class SubstitutionTests(unittest.TestCase):
    def test_substitution_consumes_entitlement_only_within_allowed_service(self):
        from sourcepack.model import build_domain, adjudicate
        dataset = base_dataset()
        # S4 承诺 B，实际执行 B_ALT，按 SU1 允许替代；再放一个不在规则内的服务
        dataset["executions"]["rows"] = [
            {"execution_id": "X3", "request_id": "R3", "person": "P2", "service_code": "B_ALT",
             "team": "T2", "units": "1", "at": "2026-10-14T08:00:00+08:00", "status": "confirmed"},
            {"execution_id": "X4", "request_id": "R3", "person": "P2", "service_code": "C",
             "team": "T2", "units": "1", "at": "2026-10-15T08:00:00+08:00", "status": "confirmed"},
        ]
        dataset["fulfillment_links"]["rows"] = [
            {"link_id": "F4", "execution_id": "X3", "slot_id": "S4", "units": "1"},
            {"link_id": "F5", "execution_id": "X4", "slot_id": "S4", "units": "1"},
        ]
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "in"
            folder.mkdir()
            write_dataset(folder, dataset)
            wh = Warehouse(Path(d) / "w.duckdb")
            wh.ingest_directory(folder)
            rows = {name: wh.ingestion.current_rows(name) for name in BASE_TABLES}
            facts = adjudicate(build_domain(rows))
            self.assertEqual(facts["F4"]["fit_type"], "substitution")
            self.assertEqual(facts["F4"]["status"], "accepted")
            self.assertEqual(facts["F5"]["status"], "disputed")
            self.assertIn("service_mismatch", facts["F5"]["hard_reasons"])
            wh.close()


class IngestionValidationTests(unittest.TestCase):
    def test_negative_amount_rejected(self):
        from sourcepack.ingestion import read_source_file
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cash.csv"
            path.write_text(
                "cash_id,package_id,kind,amount,at\n"
                "C1,PK1,receive,-1,2026-10-01T08:00:00+08:00\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "类型不匹配"):
                read_source_file(path, "cash")


if __name__ == "__main__":
    unittest.main()
