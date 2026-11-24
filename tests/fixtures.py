"""测试夹具：在独立目录生成来源 CSV，与受检数据目录隔离。"""
import csv
import datetime as dt
from pathlib import Path

BASE_TABLES = [
    "packages", "slots", "requests", "executions",
    "fulfillment_links", "substitutions", "refunds", "cash",
]


def write_csv(folder, name, rows, columns):
    path = Path(folder) / f"{name}.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def iso(hour, day=10):
    return dt.datetime(2026, 10, day, hour, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))).isoformat()


def base_dataset():
    """与仓库 data/ 同构的小型数据集，字段齐全、可按需改行。"""
    return {
        "packages": {
            "columns": ["package_id", "person", "sold_at", "price"],
            "rows": [
                {"package_id": "PK1", "person": "P1", "sold_at": iso(8, 1), "price": "800"},
                {"package_id": "PK2", "person": "P1", "sold_at": iso(8, 2), "price": "300"},
                {"package_id": "PK3", "person": "P2", "sold_at": iso(8, 3), "price": "500"},
            ],
        },
        "slots": {
            "columns": ["slot_id", "package_id", "service_code", "units", "relative_price", "exclusive_group"],
            "rows": [
                {"slot_id": "S1", "package_id": "PK1", "service_code": "A", "units": "2", "relative_price": "300", "exclusive_group": "G1"},
                {"slot_id": "S2", "package_id": "PK1", "service_code": "B", "units": "1", "relative_price": "400", "exclusive_group": ""},
                {"slot_id": "S3", "package_id": "PK2", "service_code": "A", "units": "1", "relative_price": "300", "exclusive_group": "G1"},
                {"slot_id": "S4", "package_id": "PK3", "service_code": "B", "units": "2", "relative_price": "250", "exclusive_group": ""},
            ],
        },
        "requests": {
            "columns": ["request_id", "person", "service_code", "state"],
            "rows": [
                {"request_id": "R1", "person": "P1", "service_code": "A", "state": "cancelled"},
                {"request_id": "R2", "person": "P1", "service_code": "B", "state": "requested"},
                {"request_id": "R3", "person": "P2", "service_code": "B", "state": "requested"},
            ],
        },
        "executions": {
            "columns": ["execution_id", "request_id", "person", "service_code", "team", "units", "at", "status"],
            "rows": [
                {"execution_id": "X1", "request_id": "R1", "person": "P1", "service_code": "A", "team": "T1", "units": "1", "at": iso(8, 10), "status": "confirmed"},
                {"execution_id": "X2", "request_id": "R2", "person": "P1", "service_code": "B", "team": "T2", "units": "1", "at": iso(8, 12), "status": "confirmed"},
                {"execution_id": "X3", "request_id": "R3", "person": "P2", "service_code": "B", "team": "T2", "units": "1", "at": iso(8, 14), "status": "pending"},
            ],
        },
        "fulfillment_links": {
            "columns": ["link_id", "execution_id", "slot_id", "units"],
            "rows": [
                {"link_id": "F1", "execution_id": "X1", "slot_id": "S1", "units": "1"},
                {"link_id": "F2", "execution_id": "X1", "slot_id": "S3", "units": "1"},
                {"link_id": "F3", "execution_id": "X2", "slot_id": "S2", "units": "1"},
                {"link_id": "F4", "execution_id": "X3", "slot_id": "S4", "units": "1"},
            ],
        },
        "substitutions": {
            "columns": ["rule_id", "slot_service", "actual_service", "unit_ratio"],
            "rows": [
                {"rule_id": "SU1", "slot_service": "B", "actual_service": "B_ALT", "unit_ratio": "1"},
            ],
        },
        "refunds": {
            "columns": ["refund_id", "slot_id", "units", "amount", "at"],
            "rows": [
                {"refund_id": "RF1", "slot_id": "S1", "units": "1", "amount": "240", "at": iso(8, 16)},
                {"refund_id": "RF2", "slot_id": "S2", "units": "1", "amount": "320", "at": iso(8, 17)},
            ],
        },
        "cash": {
            "columns": ["cash_id", "package_id", "kind", "amount", "at"],
            "rows": [
                {"cash_id": "C1", "package_id": "PK1", "kind": "receive", "amount": "800", "at": iso(8, 1)},
                {"cash_id": "C2", "package_id": "PK2", "kind": "receive", "amount": "300", "at": iso(8, 2)},
                {"cash_id": "C3", "package_id": "PK3", "kind": "receive", "amount": "500", "at": iso(8, 3)},
                {"cash_id": "C4", "package_id": "PK1", "kind": "refund", "amount": "240", "at": iso(8, 16)},
                {"cash_id": "C5", "package_id": "PK1", "kind": "refund", "amount": "320", "at": iso(8, 17)},
            ],
        },
    }


def write_dataset(folder, dataset):
    for name, spec in dataset.items():
        write_csv(folder, name, spec["rows"], spec["columns"])


def make_warehouse(folder, db_path, dataset=None):
    from sourcepack.warehouse import Warehouse
    dataset = dataset or base_dataset()
    write_dataset(folder, dataset)
    wh = Warehouse(db_path)
    results = wh.ingest_directory(folder)
    wh.rebuild()
    return wh, results
