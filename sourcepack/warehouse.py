"""DuckDB 事实仓：可重复重建的事实表、人工对账轮次与查询接口。

事实表不是手工维护的结论，而是由"原始层当前行 + 已确认对账决策"
每次重建得到；对账轮次保存决策前快照，供比较当前归属与原对账。
"""
from __future__ import annotations

import datetime
import json
from decimal import Decimal
from pathlib import Path

import duckdb

from .ingestion import Ingestion
from .model import (
    ACCEPTED,
    DISPUTED,
    MANUAL_STATUSES,
    PENDING,
    adjudicate,
    build_domain,
    compute_positions,
    execution_facts,
    match_cash,
)
from .money import from_cents
from .schema import TABLES

FACT_DDL = """
CREATE TABLE IF NOT EXISTS fact_fulfillment_link (
    link_id VARCHAR PRIMARY KEY,
    execution_id VARCHAR NOT NULL,
    source_slot_id VARCHAR NOT NULL,
    effective_slot_id VARCHAR NOT NULL,
    units DOUBLE NOT NULL,
    execution_consumed_units DOUBLE NOT NULL,
    fit_type VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    revenue_cents BIGINT NOT NULL,
    reasons VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_execution (
    execution_id VARCHAR PRIMARY KEY,
    request_id VARCHAR NOT NULL,
    person VARCHAR NOT NULL,
    service_code VARCHAR NOT NULL,
    team VARCHAR NOT NULL,
    units DOUBLE NOT NULL,
    service_date VARCHAR NOT NULL,
    "at" VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    request_state VARCHAR NOT NULL,
    link_count INTEGER NOT NULL,
    accepted_link_count INTEGER NOT NULL,
    revenue_cents BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_slot_position (
    slot_id VARCHAR PRIMARY KEY,
    package_id VARCHAR NOT NULL,
    service_code VARCHAR NOT NULL,
    units DOUBLE NOT NULL,
    relative_price DOUBLE NOT NULL,
    exclusive_group VARCHAR NOT NULL,
    contract_cents BIGINT NOT NULL,
    accepted_units DOUBLE NOT NULL,
    pending_claim_units DOUBLE NOT NULL,
    disputed_claim_units DOUBLE NOT NULL,
    refunded_units DOUBLE NOT NULL,
    clean_refund_units DOUBLE NOT NULL,
    disputed_refund_units DOUBLE NOT NULL,
    verified_contract_cents BIGINT NOT NULL,
    clean_refund_contract_cents BIGINT NOT NULL,
    disputed_refund_contract_cents BIGINT NOT NULL,
    unfulfilled_contract_cents BIGINT NOT NULL,
    unresolved_contract_cents_memo BIGINT NOT NULL,
    review_flags VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_package_position (
    package_id VARCHAR PRIMARY KEY,
    person VARCHAR NOT NULL,
    price_cents BIGINT NOT NULL,
    received_cash_cents BIGINT NOT NULL,
    refunded_cash_cents BIGINT NOT NULL,
    net_cash_cents BIGINT NOT NULL,
    verified_contract_cents BIGINT NOT NULL,
    clean_refund_contract_cents BIGINT NOT NULL,
    disputed_refund_contract_cents BIGINT NOT NULL,
    unfulfilled_contract_cents BIGINT NOT NULL,
    unresolved_contract_cents_memo BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_refund (
    refund_id VARCHAR PRIMARY KEY,
    slot_id VARCHAR NOT NULL,
    units DOUBLE NOT NULL,
    actual_amount_cents BIGINT NOT NULL,
    clean_units DOUBLE NOT NULL,
    disputed_units DOUBLE NOT NULL,
    clean_contract_cents BIGINT NOT NULL,
    disputed_contract_cents BIGINT NOT NULL,
    entitlement_cents BIGINT NOT NULL,
    difference_cents BIGINT NOT NULL,
    cash_match_status VARCHAR NOT NULL,
    review_flags VARCHAR NOT NULL,
    "at" VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_cash (
    cash_id VARCHAR PRIMARY KEY,
    package_id VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    amount_cents BIGINT NOT NULL,
    "at" VARCHAR NOT NULL,
    match_status VARCHAR NOT NULL,
    matched_refund_id VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS recv_decision (
    round_id INTEGER NOT NULL,
    link_id VARCHAR NOT NULL,
    decision VARCHAR NOT NULL,
    new_slot_id VARCHAR,
    reason VARCHAR NOT NULL,
    recorded_at VARCHAR NOT NULL,
    PRIMARY KEY (round_id, link_id)
);
CREATE TABLE IF NOT EXISTS recv_reconciliation (
    round_id INTEGER PRIMARY KEY,
    status VARCHAR NOT NULL,
    opened_at VARCHAR NOT NULL,
    confirmed_at VARCHAR,
    before_snapshot JSON
);
"""

FACT_TABLES = [
    "fact_fulfillment_link",
    "fact_execution",
    "fact_slot_position",
    "fact_package_position",
    "fact_refund",
    "fact_cash",
]


def _jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    raise TypeError(f"不可序列化的类型：{type(value)}")


def _iso(value):
    """时刻统一存带偏移的 ISO 字符串，避免数据库时区配置改写业务时点。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


class Warehouse:
    """业务事实仓入口，封装接收、重建、对账与查询。"""

    def __init__(self, path=None):
        self.path = str(path) if path else ":memory:"
        self.con = duckdb.connect(self.path)
        self.ingestion = Ingestion(self.con)
        self.con.execute(FACT_DDL)

    def close(self):
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- 接收 ----------------------------------------------------------
    def ingest_file(self, path, table_name, *, received_at=None, label=None):
        return self.ingestion.ingest_file(
            path, table_name, received_at=received_at, label=label
        )

    def ingest_directory(self, directory, *, order=None, received_at=None, label=None):
        """按表间引用顺序接收一个目录下的标准命名 CSV。"""
        order = order or [
            "packages", "slots", "requests", "executions",
            "fulfillment_links", "substitutions", "refunds", "cash",
        ]
        results = []
        directory = Path(directory)
        for table_name in order:
            path = directory / f"{table_name}.csv"
            if path.exists():
                results.append(
                    self.ingest_file(
                        path, table_name,
                        received_at=received_at,
                        label=label or f"directory:{directory.name}",
                    )
                )
        return results

    # ---- 重建 ----------------------------------------------------------
    def _confirmed_decisions(self):
        rows = self.con.execute(
            """
            SELECT d.link_id, d.decision, d.new_slot_id
            FROM recv_decision d
            JOIN recv_reconciliation r ON r.round_id = d.round_id
            WHERE r.status = 'confirmed'
            """
        ).fetchall()
        return [
            {"link_id": link_id, "decision": decision, "new_slot_id": new_slot_id}
            for link_id, decision, new_slot_id in rows
        ]

    def rebuild(self, *, as_of_seq=None):
        """从原始层当前行与已确认决策重建全部事实表。"""
        rows = {
            name: self.ingestion.current_rows(name, as_of_seq=as_of_seq)
            for name in TABLES
        }
        domain = build_domain(rows)
        decisions = self._confirmed_decisions() if as_of_seq is None else []
        link_facts = adjudicate(domain, decisions)
        slot_positions, package_positions, refund_facts, _ = compute_positions(
            domain, link_facts
        )
        cash_facts = match_cash(domain, refund_facts)
        exec_facts = execution_facts(domain, link_facts)

        for table in FACT_TABLES:
            self.con.execute(f"DELETE FROM {table}")

        for fact in link_facts.values():
            self.con.execute(
                "INSERT INTO fact_fulfillment_link VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    fact["link_id"], fact["execution_id"], fact["source_slot_id"],
                    fact["effective_slot_id"], float(fact["units"]),
                    float(fact["execution_consumed_units"]), fact["fit_type"],
                    fact["status"], fact["revenue_cents"],
                    json.dumps(fact["reasons"], ensure_ascii=False),
                ],
            )
        for fact in exec_facts:
            self.con.execute(
                "INSERT INTO fact_execution VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    fact["execution_id"], fact["request_id"], fact["person"],
                    fact["service_code"], fact["team"], float(fact["units"]),
                    fact["service_date"], _iso(fact["at"]), fact["status"],
                    fact["request_state"], fact["link_count"],
                    fact["accepted_link_count"], fact["revenue_cents"],
                ],
            )
        for pos in slot_positions:
            self.con.execute(
                "INSERT INTO fact_slot_position VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    pos["slot_id"], pos["package_id"], pos["service_code"],
                    float(pos["units"]), float(pos["relative_price"]),
                    pos["exclusive_group"], pos["contract_cents"],
                    float(pos["accepted_units"]), float(pos["pending_claim_units"]),
                    float(pos["disputed_claim_units"]), float(pos["refunded_units"]),
                    float(pos["clean_refund_units"]), float(pos["disputed_refund_units"]),
                    pos["verified_contract_cents"], pos["clean_refund_contract_cents"],
                    pos["disputed_refund_contract_cents"],
                    pos["unfulfilled_contract_cents"],
                    pos["unresolved_contract_cents_memo"],
                    json.dumps(pos["review_flags"], ensure_ascii=False),
                ],
            )
        for pos in package_positions:
            self.con.execute(
                "INSERT INTO fact_package_position VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [pos[k] for k in (
                    "package_id", "person", "price_cents", "received_cash_cents",
                    "refunded_cash_cents", "net_cash_cents", "verified_contract_cents",
                    "clean_refund_contract_cents", "disputed_refund_contract_cents",
                    "unfulfilled_contract_cents", "unresolved_contract_cents_memo",
                )],
            )
        for refund in refund_facts:
            self.con.execute(
                "INSERT INTO fact_refund VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    refund["refund_id"], refund["slot_id"], float(refund["units"]),
                    refund["actual_amount_cents"], float(refund["clean_units"]),
                    float(refund["disputed_units"]), refund["clean_contract_cents"],
                    refund["disputed_contract_cents"], refund["entitlement_cents"],
                    refund["difference_cents"], refund["cash_match_status"],
                    json.dumps(refund["review_flags"], ensure_ascii=False), _iso(refund["at"]),
                ],
            )
        for cash in cash_facts:
            self.con.execute(
                "INSERT INTO fact_cash VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    cash["cash_id"], cash["package_id"], cash["kind"],
                    cash["amount_cents"], _iso(cash["at"]), cash["match_status"],
                    cash["matched_refund_id"],
                ],
            )
        return {
            "links": len(link_facts),
            "executions": len(exec_facts),
            "slots": len(slot_positions),
            "packages": len(package_positions),
            "refunds": len(refund_facts),
            "cash": len(cash_facts),
        }

    # ---- 对账轮次 -------------------------------------------------------
    def open_round(self):
        """开启一轮人工对账，快照保存当前（重建前）事实。"""
        self.rebuild()
        next_id = self.con.execute(
            "SELECT COALESCE(MAX(round_id), 0) + 1 FROM recv_reconciliation"
        ).fetchone()[0]
        snapshot = {
            table: self._table_snapshot(table)
            for table in FACT_TABLES
        }
        now = datetime.datetime.now(datetime.timezone.utc)
        self.con.execute(
            "INSERT INTO recv_reconciliation VALUES (?, ?, ?, ?, ?)",
            [next_id, "open", _iso(now), None,
             json.dumps(snapshot, ensure_ascii=False, default=_jsonable)],
        )
        return next_id

    def record_decision(self, round_id, link_id, decision, *, new_slot_id=None, reason=""):
        """登记一条人工决策；轮次必须处于 open。"""
        if decision not in MANUAL_STATUSES:
            raise ValueError("决策取值不合法")
        status = self.con.execute(
            "SELECT status FROM recv_reconciliation WHERE round_id = ?", [round_id]
        ).fetchone()
        if status is None:
            raise ValueError("对账轮次不存在")
        if status[0] != "open":
            raise ValueError("对账轮次已确认，不可再改")
        exists = self.con.execute(
            "SELECT 1 FROM fact_fulfillment_link WHERE link_id = ?", [link_id]
        ).fetchone()
        if not exists:
            raise ValueError("兑现引用不存在")
        if new_slot_id:
            slot = self.con.execute(
                "SELECT 1 FROM fact_slot_position WHERE slot_id = ?", [new_slot_id]
            ).fetchone()
            if not slot:
                raise ValueError("改引目标权益不存在")
        self.con.execute(
            "INSERT OR REPLACE INTO recv_decision VALUES (?, ?, ?, ?, ?, ?)",
            [round_id, link_id, decision, new_slot_id, reason,
             _iso(datetime.datetime.now(datetime.timezone.utc))],
        )
        return {
            "round_id": round_id, "link_id": link_id, "decision": decision,
            "new_slot_id": new_slot_id, "reason": reason,
        }

    def confirm_round(self, round_id):
        """确认一轮对账：决策生效并重建，返回与确认前快照的比较。"""
        row = self.con.execute(
            "SELECT status, before_snapshot FROM recv_reconciliation WHERE round_id = ?",
            [round_id],
        ).fetchone()
        if row is None:
            raise ValueError("对账轮次不存在")
        if row[0] != "open":
            raise ValueError("对账轮次已确认")
        before = json.loads(row[1])
        self.con.execute(
            "UPDATE recv_reconciliation SET status = 'confirmed', confirmed_at = ? WHERE round_id = ?",
            [_iso(datetime.datetime.now(datetime.timezone.utc)), round_id],
        )
        self.rebuild()
        after = {table: self._table_snapshot(table) for table in FACT_TABLES}
        diff = self._diff_snapshot(before, after)
        return {"round_id": round_id, "status": "confirmed", "changes": diff}

    def _table_snapshot(self, table):
        cols = [r[1] for r in self.con.execute(f"PRAGMA table_info('{table}')").fetchall()]
        records = self.con.execute(f"SELECT * FROM {table} ORDER BY {cols[0]}").fetchall()
        return [dict(zip(cols, self._snapshot_values(record))) for record in records]

    @staticmethod
    def _snapshot_values(record):
        return list(record)

    def _diff_snapshot(self, before, after):
        """逐表逐键比较确认前后事实，明细级变化而不仅是汇总差异。"""
        key_names = {
            "fact_fulfillment_link": "link_id",
            "fact_execution": "execution_id",
            "fact_slot_position": "slot_id",
            "fact_package_position": "package_id",
            "fact_refund": "refund_id",
            "fact_cash": "cash_id",
        }
        changes = {}
        for table in FACT_TABLES:
            key_name = key_names[table]
            before_map = {r[key_name]: r for r in before[table]}
            after_map = {r[key_name]: r for r in after[table]}
            table_changes = []
            for key in sorted(set(before_map) | set(after_map)):
                old, new = before_map.get(key), after_map.get(key)
                if old != new:
                    fields = sorted(
                        set(old or {}) | set(new or {})
                    )
                    table_changes.append({
                        "key": key,
                        "fields": {
                            f: {"before": (old or {}).get(f), "after": (new or {}).get(f)}
                            for f in fields
                            if (old or {}).get(f) != (new or {}).get(f)
                        },
                    })
            changes[table] = table_changes
        return changes

    def list_rounds(self):
        rows = self.con.execute(
            "SELECT round_id, status, opened_at, confirmed_at FROM recv_reconciliation ORDER BY round_id"
        ).fetchall()
        return [
            {
                "round_id": r[0], "status": r[1],
                "opened_at": r[2],
                "confirmed_at": r[3],
            }
            for r in rows
        ]

    # ---- 查询 ----------------------------------------------------------
    def package_positions(self):
        cols, rows = self._fetch("fact_package_position", "package_id")
        return [self._with_money(dict(zip(cols, r))) for r in rows]

    def slot_positions(self, package_id=None):
        if package_id:
            cols, rows = self._fetch(
                "fact_slot_position", "slot_id",
                "WHERE package_id = ?", [package_id],
            )
        else:
            cols, rows = self._fetch("fact_slot_position", "slot_id")
        return [self._with_money(dict(zip(cols, r))) for r in rows]

    def department_volume(self, *, team=None, service_date_from=None, service_date_to=None):
        """科室视角：按执行日期、团队统计已核实服务量及对应合同收入。

        pending 执行单列，退款不回冲服务次数；每行可经 link 明细下钻。
        """
        where, params = [], []
        if team:
            where.append("team = ?")
            params.append(team)
        if service_date_from:
            where.append("service_date >= ?")
            params.append(service_date_from)
        if service_date_to:
            where.append("service_date < ?")
            params.append(service_date_to)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        cols, rows = self._fetch("fact_execution", "service_date, team, execution_id", clause, params)
        return [self._with_money(dict(zip(cols, r))) for r in rows]

    def department_summary(self):
        rows = self.con.execute(
            """
            SELECT service_date, team, service_code,
                   SUM(CASE WHEN status = 'confirmed' THEN units ELSE 0 END) AS confirmed_units,
                   SUM(CASE WHEN status = 'pending' THEN units ELSE 0 END) AS pending_units,
                   SUM(revenue_cents) AS revenue_cents,
                   COUNT(*) AS execution_rows
            FROM fact_execution
            GROUP BY service_date, team, service_code
            ORDER BY service_date, team, service_code
            """
        ).fetchall()
        result = []
        for r in rows:
            result.append({
                "service_date": r[0], "team": r[1], "service_code": r[2],
                "confirmed_units": float(r[3]), "pending_units": float(r[4]),
                "revenue_cents": int(r[5]), "revenue_yuan": str(from_cents(r[5])),
                "execution_rows": int(r[6]),
            })
        return result

    def links(self, *, status=None, execution_id=None, slot_id=None):
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if execution_id:
            where.append("execution_id = ?")
            params.append(execution_id)
        if slot_id:
            where.append("effective_slot_id = ?")
            params.append(slot_id)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        cols, rows = self._fetch("fact_fulfillment_link", "link_id", clause, params)
        result = []
        for r in rows:
            item = dict(zip(cols, r))
            item["reasons"] = json.loads(item["reasons"])
            item["revenue_yuan"] = str(from_cents(item["revenue_cents"]))
            result.append(item)
        return result

    def refunds(self):
        cols, rows = self._fetch("fact_refund", "refund_id")
        result = []
        for r in rows:
            item = self._with_money(dict(zip(cols, r)))
            item["review_flags"] = json.loads(item["review_flags"])
            result.append(item)
        return result

    def cash_flows(self):
        cols, rows = self._fetch("fact_cash", "cash_id")
        return [self._with_money(dict(zip(cols, r))) for r in rows]

    def evidence_gaps(self):
        """两侧缺证清单：无有效兑现的执行，与无已核实兑现的收费权益。"""
        cursor = self.con.execute(
            """
            SELECT execution_id, status, service_code, team, "at"
            FROM fact_execution
            WHERE accepted_link_count = 0
            ORDER BY execution_id
            """
        )
        cols = [d[0] for d in cursor.description]
        missing_execution = [dict(zip(cols, record)) for record in cursor.fetchall()]
        missing_billing = []
        cols, rows = self._fetch(
            "fact_slot_position", "slot_id",
            "WHERE accepted_units = 0",
        )
        for r in rows:
            missing_billing.append(self._with_money(dict(zip(cols, r))))
        return {
            "executions_without_verified_fulfillment": missing_execution,
            "slots_without_verified_fulfillment": missing_billing,
        }

    def _fetch(self, table, order, clause="", params=None):
        cursor = self.con.execute(
            f"SELECT * FROM {table} {clause} ORDER BY {order}", params or []
        )
        cols = [d[0] for d in cursor.description]
        return cols, cursor.fetchall()

    @staticmethod
    def _with_money(item):
        for key, value in list(item.items()):
            if key.endswith("_cents"):
                item[key[: -len("_cents")] + "_yuan"] = str(from_cents(value))
        return item
