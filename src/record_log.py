"""處理紀錄模組:系統 log 與每筆文件的 JSONL 處理紀錄。"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

RECORDS_FILENAME = "records.jsonl"


def setup_logging(logs_dir: Path) -> None:
    """同時輸出到主控台與 logs/app.log(UTF-8,避免中文亂碼)。"""
    logs_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(logs_dir / "app.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    # httpx 的 INFO log 會印出完整請求網址(含 Cloudflare 帳號 ID),只保留警告以上
    logging.getLogger("httpx").setLevel(logging.WARNING)


def timestamp() -> str:
    """處理紀錄的「時間」(到秒)。同一筆紀錄只取一次,records.jsonl 與 SQLite 共用同一個值。"""
    return datetime.now().isoformat(timespec="seconds")


def append_record(logs_dir: Path, record: dict[str, Any]) -> None:
    """每處理一筆文件即追加一行 JSON 至 records.jsonl。record 已有「時間」就沿用,不覆寫。"""
    logs_dir.mkdir(parents=True, exist_ok=True)
    record = {"時間": timestamp(), **record}
    with open(logs_dir / RECORDS_FILENAME, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def clear_records(logs_dir: Path) -> None:
    """清空 records.jsonl(「刪除全部資料」用):裡面每一行都含讀值,資料庫刪了它也要跟著刪,畫面上的話才成立。"""
    (logs_dir / RECORDS_FILENAME).unlink(missing_ok=True)


def load_records(logs_dir: Path) -> list[dict[str, Any]]:
    path = logs_dir / RECORDS_FILENAME
    if not path.exists():
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def summarize(logs_dir: Path) -> dict[str, Any]:
    """統計處理結果,供 `python main.py stats` 使用。"""
    records = load_records(logs_dir)
    summary: dict[str, Any] = {
        "總處理筆數": len(records),
        "自動歸檔": 0,
        "待人工確認": 0,
        "處理失敗": 0,
    }
    confidences = []
    for r in records:
        action = r.get("動作")
        if action == "archive":
            summary["自動歸檔"] += 1
        elif action == "review":
            summary["待人工確認"] += 1
        elif action == "failed":
            summary["處理失敗"] += 1
        conf = (r.get("AI辨識結果") or {}).get("confidence")
        if isinstance(conf, (int, float)):
            confidences.append(conf)
    if confidences:
        summary["平均信心分數"] = round(sum(confidences) / len(confidences), 3)
    if len(records) > 0:
        summary["自動化比例"] = f"{summary['自動歸檔'] / len(records):.0%}"
    return summary
