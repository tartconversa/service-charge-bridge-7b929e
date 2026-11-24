"""命令行入口：接收资料、重建事实、人工对账与运营查询。

示例：
  python -m sourcepack.cli -w warehouse.duckdb ingest-dir data
  python -m sourcepack.cli -w warehouse.duckdb rebuild
  python -m sourcepack.cli -w warehouse.duckdb packages
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

from .warehouse import Warehouse


def _print(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _connect(args):
    return Warehouse(args.warehouse)


def cmd_ingest_file(args):
    with _connect(args) as wh:
        result = wh.ingest_file(
            args.file, args.table,
            received_at=_parse_at(args.received_at), label=args.label,
        )
        if args.rebuild and result["status"] == "loaded":
            result["rebuild"] = wh.rebuild()
        _print(result)


def cmd_ingest_dir(args):
    with _connect(args) as wh:
        results = wh.ingest_directory(
            args.directory,
            received_at=_parse_at(args.received_at), label=args.label,
        )
        if args.rebuild and any(r["status"] == "loaded" for r in results):
            _print({"files": results, "rebuild": wh.rebuild()})
        else:
            _print({"files": results})


def cmd_rebuild(args):
    with _connect(args) as wh:
        _print(wh.rebuild())


def cmd_packages(args):
    with _connect(args) as wh:
        _print(wh.package_positions())


def cmd_slots(args):
    with _connect(args) as wh:
        _print(wh.slot_positions(args.package))


def cmd_department(args):
    with _connect(args) as wh:
        _print(wh.department_summary())


def cmd_executions(args):
    with _connect(args) as wh:
        _print(wh.department_volume(
            team=args.team,
            service_date_from=args.date_from,
            service_date_to=args.date_to,
        ))


def cmd_links(args):
    with _connect(args) as wh:
        _print(wh.links(status=args.status, execution_id=args.execution, slot_id=args.slot))


def cmd_refunds(args):
    with _connect(args) as wh:
        _print(wh.refunds())


def cmd_cash(args):
    with _connect(args) as wh:
        _print(wh.cash_flows())


def cmd_gaps(args):
    with _connect(args) as wh:
        _print(wh.evidence_gaps())


def cmd_open_round(args):
    with _connect(args) as wh:
        _print({"round_id": wh.open_round()})


def cmd_decide(args):
    with _connect(args) as wh:
        _print(wh.record_decision(
            args.round_id, args.link_id, args.decision,
            new_slot_id=args.new_slot, reason=args.reason,
        ))


def cmd_confirm_round(args):
    with _connect(args) as wh:
        _print(wh.confirm_round(args.round_id))


def cmd_rounds(args):
    with _connect(args) as wh:
        _print(wh.list_rounds())


def _parse_at(value):
    if not value:
        return None
    return datetime.datetime.fromisoformat(value)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="sourcepack",
        description="服务执行与收费关联仓：事实接收、对账与查询，不做医疗判断。",
    )
    parser.add_argument(
        "-w", "--warehouse", default="warehouse.duckdb",
        help="DuckDB 仓文件路径，默认 warehouse.duckdb",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest-file", help="接收单个来源 CSV（同摘要不增事实）")
    p.add_argument("file")
    p.add_argument("table", choices=[
        "packages", "slots", "requests", "executions", "fulfillment_links",
        "substitutions", "refunds", "cash",
    ])
    p.add_argument("--received-at", help="ISO 时刻，缺省取当前 UTC")
    p.add_argument("--label")
    p.add_argument("--rebuild", action="store_true", help="接收后重建事实表")
    p.set_defaults(func=cmd_ingest_file)

    p = sub.add_parser("ingest-dir", help="按引用顺序接收目录下标准命名 CSV")
    p.add_argument("directory")
    p.add_argument("--received-at")
    p.add_argument("--label")
    p.add_argument("--rebuild", action="store_true")
    p.set_defaults(func=cmd_ingest_dir)

    p = sub.add_parser("rebuild", help="从原始层与已确认决策重建事实表")
    p.set_defaults(func=cmd_rebuild)

    p = sub.add_parser("packages", help="收费处视角：销售包已兑现/未兑现/退款/无法归属金额")
    p.set_defaults(func=cmd_packages)

    p = sub.add_parser("slots", help="权益级合同归属，可按包下钻")
    p.add_argument("--package")
    p.set_defaults(func=cmd_slots)

    p = sub.add_parser("department", help="科室视角：按日期、团队汇总服务量与对应收入")
    p.set_defaults(func=cmd_department)

    p = sub.add_parser("executions", help="执行明细（可下钻到同一条履约关系）")
    p.add_argument("--team")
    p.add_argument("--date-from")
    p.add_argument("--date-to")
    p.set_defaults(func=cmd_executions)

    p = sub.add_parser("links", help="履约引用裁决明细")
    p.add_argument("--status", choices=["accepted", "pending", "disputed"])
    p.add_argument("--execution")
    p.add_argument("--slot")
    p.set_defaults(func=cmd_links)

    p = sub.add_parser("refunds", help="退款撤回范围、资格额与实退差异")
    p.set_defaults(func=cmd_refunds)

    p = sub.add_parser("cash", help="现金流水及与退款的匹配状态")
    p.set_defaults(func=cmd_cash)

    p = sub.add_parser("gaps", help="两侧缺证：无有效兑现的执行与无兑现的收费")
    p.set_defaults(func=cmd_gaps)

    p = sub.add_parser("open-round", help="开启一轮人工对账并保存确认前快照")
    p.set_defaults(func=cmd_open_round)

    p = sub.add_parser("decide", help="在开放轮次中登记人工决策（可改引权益）")
    p.add_argument("round_id", type=int)
    p.add_argument("link_id")
    p.add_argument("decision", choices=["accepted", "pending", "disputed"])
    p.add_argument("--new-slot", help="改引套餐/权益的目标 slot_id")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("confirm-round", help="确认对账轮次，返回与原对账快照的明细差异")
    p.add_argument("round_id", type=int)
    p.set_defaults(func=cmd_confirm_round)

    p = sub.add_parser("rounds", help="对账轮次列表")
    p.set_defaults(func=cmd_rounds)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
