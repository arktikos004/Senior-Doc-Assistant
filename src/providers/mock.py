"""Mock provider:不需任何模型即可展示與測試完整流程。"""
from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

from ..config import AppConfig
from ..models import ExtractionResult


class MockAnalyzer:
    """模擬辨識器:依檔名產生固定結果。

    檔名規則:
    - 含「收據」或 receipt  → 判為收據
    - 含「low」或「模糊」   → 回傳低信心分數(展示待確認流程)
    - 其餘                  → 判為發票、高信心
    """

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg

    def analyze(self, file_path: Path, doc_type_hint: str | None = None, *,
                local_only: bool = False) -> ExtractionResult:
        # local_only 不用看:模擬讀值本來就不會送出這台電腦
        name = file_path.stem.lower()
        doc_type = "收據" if ("收據" in name or "receipt" in name) else "發票"
        low_conf = "low" in name or "模糊" in name
        # 以檔名雜湊產生穩定的假金額,讓 demo 可重現
        digest = int(hashlib.md5(file_path.stem.encode("utf-8")).hexdigest(), 16)
        return ExtractionResult(
            doc_type=doc_type,
            date=date.today().isoformat(),
            vendor="測試商店",
            amount=float(digest % 9000 + 100),
            invoice_number=None if doc_type == "收據" else "AB12345678",
            confidence=0.45 if low_conf else 0.92,
            notes="Mock 模式產生的模擬結果",
        )
