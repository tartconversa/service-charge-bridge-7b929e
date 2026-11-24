"""资料接收：原始层只追加、不覆盖，同键整行修订，同摘要不增事实。

每次接收产生一个批次（recv_batch），原始行写入 ``raw_<表>`` 并标注批次序号；
事实层永远从"每个业务键的最新行"重建，历史行保留可追溯。
"""
from __future__ import annotations

import csv
import datetime
import hashlib
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .schema import DOMAINS, TABLES

REFERENCES = {
    "slots": [("package_id", "packages", "package_id")],
    "executions": [("request_id", "requests", "request_id")],
    "fulfillment_links": [
        ("execution_id", "executions", "execution_id"),
        ("slot_id", "slots", "slot_id"),
    ],
    "refunds": [("slot_id", "slots", "slot_id")],
    "cash": [("package_id", "packages", "package_id")],
}

RAW_DDL = """
CREATE TABLE IF NOT EXISTS recv_batch (
    seq INTEGER PRIMARY KEY,
    received_at VARCHAR NOT NULL,
    label VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS recv_file (
    seq INTEGER PRIMARY KEY,
    batch_seq INTEGER NOT NULL,
    table_name VARCHAR NOT NULL,
    file_name VARCHAR NOT NULL,
    sha256 VARCHAR NOT NULL,
    byte_count BIGINT NOT NULL,
    content BLOB NOT NULL
);
"""


def _validate_row(table_name, row, line_no):
    """与通用检查器同口径的字段、类型、键校验，并补充取值域与非负检查。"""
    contract = TABLES[table_name]
    names = [c["name"] for c in contract["columns"]]
    if set(row.keys()) != set(names):
        raise ValueError(f"字段不匹配：{table_name}")
    for column in contract["columns"]:
        name = column["name"]
        value = row[name]
        if value is None:
            raise ValueError(f"列数不匹配：{table_name}:{line_no}")
        nullable = column.get("nullable", False)
        if value == "":
            if not nullable:
                raise ValueError(f"空值不允许：{table_name}:{line_no}:{name}")
            continue
        kind = column["type"]
        try:
            if kind == "number":
                number = Decimal(value)
                if not number.is_finite():
                    raise ValueError
                if number < 0:
                    raise ValueError
            elif kind == "datetime":
                stamp = datetime.datetime.fromisoformat(value)
                if stamp.tzinfo is None:
                    raise ValueError
        except (ValueError, InvalidOperation):
            raise ValueError(f"类型不匹配：{table_name}:{line_no}:{name}") from None
        domain = DOMAINS.get(f"{table_name}.{name}")
        if domain is not None and value not in domain:
            raise ValueError(f"取值域不匹配：{table_name}:{line_no}:{name}={value}")
    key = tuple(row[name] for name in contract["key"])
    if any(part == "" for part in key):
        raise ValueError(f"记录键缺失：{table_name}:{line_no}")
    return key


def read_source_file(path, table_name):
    """读取并校验一个来源 CSV，返回行字典列表。"""
    contract = TABLES[table_name]
    names = [c["name"] for c in contract["columns"]]
    with Path(path).open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != names:
            raise ValueError(f"字段不匹配：{path}")
        rows = list(reader)
    seen = set()
    for line_no, row in enumerate(rows, 2):
        key = _validate_row(table_name, row, line_no)
        if key in seen:
            raise ValueError(f"记录键重复：{table_name}:{line_no}")
        seen.add(key)
    return rows


class Ingestion:
    """DuckDB 原始层连接封装。"""

    def __init__(self, con):
        self.con = con
        con.execute(RAW_DDL)
        for table_name, contract in TABLES.items():
            cols = ", ".join(f'"{c["name"]}" VARCHAR' for c in contract["columns"])
            con.execute(
                f'CREATE TABLE IF NOT EXISTS raw_{table_name} ('
                f"batch_seq INTEGER NOT NULL, {cols})"
            )

    def _next_seq(self):
        row = self.con.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM recv_batch").fetchone()
        return int(row[0])

    def known_digests(self):
        return {
            r[0]
            for r in self.con.execute("SELECT sha256 FROM recv_file").fetchall()
        }

    def ingest_file(self, path, table_name, *, received_at=None, label=None):
        """接收单个来源文件。

        返回 ``{"status": "loaded"|"unchanged", ...}``。
        相同文件摘要不增加事实；同业务键的新行作为整行修订进入新批次。
        """
        if table_name not in TABLES:
            raise ValueError(f"未知来源表：{table_name}")
        path = Path(path)
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest in self.known_digests():
            return {
                "status": "unchanged",
                "table": table_name,
                "sha256": digest,
                "reason": "相同文件摘要不增加事实",
            }
        rows = read_source_file(path, table_name)
        self._check_references(table_name, rows)
        seq = self._next_seq()
        received_at = received_at or datetime.datetime.now(datetime.timezone.utc)
        label = label or f"{table_name}:{path.name}"
        self.con.execute(
            "INSERT INTO recv_batch VALUES (?, ?, ?)",
            [seq, received_at.isoformat(), label],
        )
        self.con.execute(
            "INSERT INTO recv_file VALUES (?, ?, ?, ?, ?, ?, ?)",
            [seq, seq, table_name, path.name, digest, len(content), content],
        )
        names = [c["name"] for c in TABLES[table_name]["columns"]]
        placeholders = ", ".join(["?"] * (len(names) + 1))
        collist = ", ".join(["batch_seq"] + [f'"{n}"' for n in names])
        for row in rows:
            self.con.execute(
                f"INSERT INTO raw_{table_name} ({collist}) VALUES ({placeholders})",
                [seq] + [row[n] for n in names],
            )
        return {
            "status": "loaded",
            "table": table_name,
            "seq": seq,
            "rows": len(rows),
            "sha256": digest,
            "received_at": received_at.isoformat(),
        }

    def _check_references(self, table_name, rows):
        for column, target, target_column in REFERENCES.get(table_name, []):
            allowed = {
                r[0]
                for r in self.con.execute(
                    f'SELECT "{target_column}" FROM raw_{target}'
                ).fetchall()
            }
            for row in rows:
                value = row[column]
                if value and value not in allowed:
                    raise ValueError(
                        f"资料引用不存在：{table_name}.{column}={value}"
                        f"（应指向 {target}.{target_column}）"
                    )

    def current_rows(self, table_name, *, as_of_seq=None):
        """每个业务键截至指定批次（默认最新）的最新行，返回字典列表。"""
        contract = TABLES[table_name]
        key_cols = contract["key"]
        cols = [c["name"] for c in contract["columns"]]
        partition = ", ".join(f'"{k}"' for k in key_cols)
        collist = ", ".join(f'"{c}"' for c in cols)
        where = "WHERE batch_seq <= ?" if as_of_seq is not None else ""
        params = [as_of_seq] if as_of_seq is not None else []
        sql = (
            f"SELECT {collist} FROM ("
            f"SELECT *, row_number() OVER (PARTITION BY {partition} "
            f"ORDER BY batch_seq DESC) AS rn FROM raw_{table_name} {where}"
            f") WHERE rn = 1 ORDER BY {partition}"
        )
        return [dict(zip(cols, record)) for record in self.con.execute(sql, params).fetchall()]

    def batches(self):
        return [
            {"seq": r[0], "received_at": r[1], "label": r[2]}
            for r in self.con.execute(
                "SELECT seq, received_at, label FROM recv_batch ORDER BY seq"
            ).fetchall()
        ]
