"""看有(高齡家庭文書輔助)- 主程式入口

用法:
    python main.py watch              # 監控 inbox 資料夾,自動處理新文件
    python main.py process <路徑>     # 一次性處理單一檔案或整個資料夾
    python main.py stats              # 顯示處理紀錄統計
    python main.py export             # 匯出 Excel 對帳表
    python main.py migrate            # 把舊的 records.jsonl 匯入 SQLite(可重複執行)
    加上 --mock 可在沒有模型的情況下以模擬模式跑通流程
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from src.config import load_config
from src.pipeline import Pipeline
from src.providers import create_analyzer
from src.record_log import setup_logging, summarize
from src.watcher import run_watch

log = logging.getLogger("main")


def main() -> int:
    parser = argparse.ArgumentParser(description="看有(高齡家庭文書輔助)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_watch = sub.add_parser("watch", help="監控 inbox 資料夾")
    p_watch.add_argument("--mock", action="store_true", help="使用模擬辨識(免 Ollama)")

    p_process = sub.add_parser("process", help="一次性處理檔案或資料夾")
    p_process.add_argument("path", nargs="?", default=None,
                           help="檔案或資料夾路徑(預設為 inbox)")
    p_process.add_argument("--mock", action="store_true", help="使用模擬辨識(免 Ollama)")

    sub.add_parser("stats", help="顯示處理統計")

    p_export = sub.add_parser("export", help="彙整處理紀錄為 Excel 對帳表(.xlsx)")
    p_export.add_argument("--out", default=None,
                          help="輸出路徑(預設 logs/對帳表_YYYYMMDD.xlsx)")
    p_export.add_argument("--doc-type", choices=["發票", "收據"], default=None,
                          help="只匯出指定類型(國外訂閱同時有 Invoice/Receipt 時擇一避免重複計)")

    sub.add_parser("migrate", help="把 logs/records.jsonl 匯入 SQLite(已匯入的會略過)")

    args = parser.parse_args()

    cfg = load_config()
    cfg.ensure_dirs()
    setup_logging(cfg.paths.logs)

    if args.command == "stats":
        print(json.dumps(summarize(cfg.paths.logs), ensure_ascii=False, indent=2))
        return 0

    if args.command == "export":
        from src.export_ledger import export_ledger
        from src.record_log import load_records

        records = load_records(cfg.paths.logs)
        out = Path(args.out) if args.out else cfg.paths.logs / f"對帳表_{date.today():%Y%m%d}.xlsx"
        path, n, totals = export_ledger(records, out, doc_type=args.doc_type,
                                         low_conf=cfg.auto_threshold)
        summary = "、".join(f"{cur} {tot:,.2f}" for cur, tot in totals.items()) or "無"
        print(f"已彙整 {n} 筆;各幣別合計:{summary} → {path}")
        return 0

    if args.command == "migrate":
        from src.record_log import load_records
        from src.store import Store

        n = Store(cfg.paths.db_path).import_records(load_records(cfg.paths.logs))
        print(f"已匯入 {n} 筆紀錄 → {cfg.paths.db_path}")
        return 0

    analyzer = create_analyzer(cfg, mock=args.mock)
    pipeline = Pipeline(cfg, analyzer)

    if args.command == "watch":
        run_watch(cfg, pipeline)
        return 0

    # process 指令
    target = Path(args.path) if args.path else cfg.paths.inbox
    if not target.exists():
        log.error("路徑不存在:%s", target)
        return 1
    if target.is_dir():
        records = pipeline.process_folder(target)
        log.info("共處理 %d 個檔案", len(records))
    else:
        pipeline.process_file(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
