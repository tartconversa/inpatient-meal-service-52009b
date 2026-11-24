"""命令行入口：入仓、构建、查询、结算。

    python3 -m mealwh ingest --db wh.duckdb --batch BASE --data-dir data
    python3 -m mealwh build --db wh.duckdb
    python3 -m mealwh fulfillment --db wh.duckdb
    python3 -m mealwh pot-cost --db wh.duckdb --pot K1
    python3 -m mealwh trace --db wh.duckdb --slot S1
    python3 -m mealwh settle --db wh.duckdb --label 晚餐结算
    python3 -m mealwh settlement --db wh.duckdb --id 1 --compare 2
"""
import argparse
import json
import sys
from pathlib import Path

from . import facts, ingest, queries, settle, store


def _print(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _cmd_ingest(args):
    contract = ingest.load_contract(args.catalog)
    files = []
    if args.data_dir:
        for table_name in contract:
            path = Path(args.data_dir) / f"{table_name}.csv"
            if path.is_file():
                files.append((table_name, path))
        missing = sorted(set(contract) - {name for name, _ in files})
        if missing:
            print("目录中缺少表文件：" + ", ".join(missing), file=sys.stderr)
    for item in args.file or []:
        table_name, _, path = item.partition("=")
        if not path:
            raise SystemExit("--file 需要 表名=路径：" + item)
        files.append((table_name, path))
    if not files:
        raise SystemExit("没有可入仓的文件")
    with store.connect(args.db) as con:
        _print(ingest.receive(con, contract, files, args.batch,
                              received_at=args.received_at, note=args.note))


def _cmd_batches(args):
    with store.connect(args.db) as con:
        _print(ingest.list_batches(con))


def _cmd_build(args):
    with store.connect(args.db) as con:
        build_id = facts.build_facts(con, as_of=args.as_of)
        _print({"build_id": build_id})


def _cmd_builds(args):
    with store.connect(args.db) as con:
        _print(facts.list_builds(con))


def _cmd_fulfillment(args):
    with store.connect(args.db) as con:
        _print(queries.fulfillment(con, build_id=args.build,
                                   meal_at=args.meal_at, ward=args.ward))


def _cmd_pot_cost(args):
    with store.connect(args.db) as con:
        _print(queries.pot_cost(con, build_id=args.build, pot_id=args.pot))


def _cmd_trace(args):
    with store.connect(args.db) as con:
        _print(queries.patient_trace(con, build_id=args.build,
                                     slot_id=args.slot, portion_id=args.portion))


def _cmd_settle(args):
    with store.connect(args.db) as con:
        build_id = args.build if args.build is not None else facts.latest_build(con)
        settlement_id = settle.settle(con, build_id, args.label, note=args.note)
        _print({"settlement_id": settlement_id, "build_id": build_id})


def _cmd_settlements(args):
    with store.connect(args.db) as con:
        _print(settle.list_settlements(con))


def _cmd_settlement(args):
    with store.connect(args.db) as con:
        if args.compare is not None:
            _print(settle.compare_settlements(con, args.id, args.compare))
        else:
            _print(settle.get_settlement(con, args.id))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mealwh", description=__doc__.strip())
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="mealwh.duckdb", help="仓库文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", parents=[common], help="接收一批资料文件入仓")
    p.add_argument("--catalog", default=str(ingest.default_catalog()),
                   help="字段契约目录(catalog.json)")
    p.add_argument("--batch", required=True, help="批次编号(调用者明确)")
    p.add_argument("--received-at", help="接收时刻(带时区)，默认当前")
    p.add_argument("--note", help="批次说明")
    p.add_argument("--data-dir", help="按契约表名接收目录下全部表文件")
    p.add_argument("--file", action="append", help="单文件接收：表名=路径，可重复")
    p.set_defaults(func=_cmd_ingest)

    p = sub.add_parser("batches", parents=[common], help="查看接收历史")
    p.set_defaults(func=_cmd_batches)

    p = sub.add_parser("build", parents=[common], help="按可知截点构建一版事实")
    p.add_argument("--as-of", help="可知截点(带时区)，默认全部已接收")
    p.set_defaults(func=_cmd_build)

    p = sub.add_parser("builds", parents=[common], help="查看事实构建列表")
    p.set_defaults(func=_cmd_builds)

    p = sub.add_parser("fulfillment", parents=[common], help="按餐次×病区查需求兑现与浪费")
    p.add_argument("--build", type=int, help="构建编号，默认最新")
    p.add_argument("--meal-at", help="餐次(ISO 时刻)")
    p.add_argument("--ward", help="病区")
    p.set_defaults(func=_cmd_fulfillment)

    p = sub.add_parser("pot-cost", parents=[common], help="按锅次查实耗与各用途成本")
    p.add_argument("--build", type=int, help="构建编号，默认最新")
    p.add_argument("--pot", help="锅次编号")
    p.set_defaults(func=_cmd_pot_cost)

    p = sub.add_parser("trace", parents=[common], help="从患者餐记录回溯锅次与费用资格")
    p.add_argument("--build", type=int, help="构建编号，默认最新")
    p.add_argument("--slot", help="槽位编号")
    p.add_argument("--portion", help="餐份编号")
    p.set_defaults(func=_cmd_trace)

    p = sub.add_parser("settle", parents=[common], help="对构建做结算确认(只增不改)")
    p.add_argument("--build", type=int, help="构建编号，默认最新")
    p.add_argument("--label", required=True, help="结算名称")
    p.add_argument("--note", help="结算说明")
    p.set_defaults(func=_cmd_settle)

    p = sub.add_parser("settlements", parents=[common], help="查看结算列表")
    p.set_defaults(func=_cmd_settlements)

    p = sub.add_parser("settlement", parents=[common], help="查看结算依据或与另一次结算比较")
    p.add_argument("--id", type=int, required=True, help="结算编号")
    p.add_argument("--compare", type=int, help="与之比较的另一次结算编号")
    p.set_defaults(func=_cmd_settlement)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
