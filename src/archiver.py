"""歸檔模組:自動命名、搬移文件,轉人工的文件附上辨識結果供人工複核。"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .config import AppConfig
from .models import ExtractionResult

# Windows 檔名不允許的字元
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')

# 待複核文件旁的 AI 辨識結果檔:<原檔名>.<副檔名>.ai.json
SIDECAR_SUFFIX = ".ai.json"


def sanitize(text: str, max_len: int = 30) -> str:
    """移除檔名非法字元、壓縮空白,並限制長度。"""
    cleaned = _INVALID_CHARS.sub("", text).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned[:max_len] if cleaned else "未知"


def build_filename(result: ExtractionResult, cfg: AppConfig, suffix: str) -> str:
    """依樣板產生歸檔檔名,例:20260704_發票_全聯實業_1250.jpg"""
    date_part = (result.date or "未知日期").replace("-", "")
    amount_part = (
        f"{result.amount:.0f}" if result.amount is not None else "未知金額"
    )
    name = cfg.filename_template.format(
        date=date_part,
        doc_type=sanitize(result.doc_type),
        vendor=sanitize(result.vendor or "未知商家"),
        amount=amount_part,
    )
    return f"{name}{suffix.lower()}"


def unique_path(target: Path) -> Path:
    """若檔名重複,自動加上 -1、-2… 序號避免覆蓋。"""
    if not target.exists():
        return target
    for i in range(1, 1000):
        candidate = target.with_stem(f"{target.stem}-{i}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"無法為 {target} 產生不重複檔名")


def archive_file(file_path: Path, result: ExtractionResult, cfg: AppConfig) -> Path:
    """決策為自動歸檔(archive)的文件:重新命名後歸檔至 archive/<類型>/(<年-月>/)。"""
    folder = cfg.paths.archive / result.doc_type
    if cfg.group_by_month and result.date:
        folder = folder / result.date[:7]  # YYYY-MM
    folder.mkdir(parents=True, exist_ok=True)

    target = unique_path(folder / build_filename(result, cfg, file_path.suffix))
    shutil.move(str(file_path), str(target))
    return target


def move_to_review(
    file_path: Path, result: ExtractionResult | None, reason: str, cfg: AppConfig
) -> Path:
    """決策為轉人工(review)的文件:保留原檔名移至待確認資料夾,並寫入同名 .json
    記錄 AI 辨識結果與原因,方便人工比對。"""
    cfg.paths.review.mkdir(parents=True, exist_ok=True)
    target = unique_path(cfg.paths.review / file_path.name)
    shutil.move(str(file_path), str(target))

    sidecar = target.with_suffix(target.suffix + SIDECAR_SUFFIX)
    sidecar.write_text(
        json.dumps(
            {
                "原因": reason,
                "AI辨識結果": result.to_dict() if result else None,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return target


def move_to_failed(file_path: Path, cfg: AppConfig) -> Path:
    """無法處理的文件:移至 failed 資料夾。"""
    cfg.paths.failed.mkdir(parents=True, exist_ok=True)
    target = unique_path(cfg.paths.failed / file_path.name)
    shutil.move(str(file_path), str(target))
    return target
