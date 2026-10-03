"""provider 介面:所有推論來源都要能把一個檔案轉成 ExtractionResult。"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..models import ExtractionResult


class Analyzer(Protocol):
    def analyze(self, file_path: Path, doc_type_hint: str | None = None, *,
                local_only: bool = False) -> ExtractionResult:
        """辨識單一文件。失敗時丟例外,由 Pipeline 轉成 failed。

        doc_type_hint:使用者上傳時選的類型(可為 None)。用途有二:
        1. 隱私分流——提示為 local_only_doc_types(如藥袋)時絕不送雲端;
           沒有提示時也一律走本機,確保未知類型不會外流。
        2. 類型專屬提示詞——知道是藥袋/帳單/公文時可用對應 schema。
        local_only:一定要在本機辨識(使用者選的大類屬 SENSITIVE_CATEGORIES,例如醫療與保險、身分證明),
        不論類型提示是什麼;類型提示照樣用來挑提示詞。只有會分流的 RoutingAnalyzer 需要看它,
        本來就只在一處推論的 provider(本機、雲端、Mock)接受並忽略。
        """
        ...
