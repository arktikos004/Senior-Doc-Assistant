"""隱私分流:依使用者選的大類與文件類型,決定在本機還是雲端辨識。

provider 設為 workers_ai 時由 create_analyzer 建立。規則刻意保守——只有「明確知道不是敏感類型」
的文件才送雲端,其餘(要求本機的敏感大類、沒提示、藥袋、「其他」、看不懂的提示)一律本機。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Union

from ..config import AppConfig
from ..models import DOC_TYPES, ExtractionResult
from .base import Analyzer

log = logging.getLogger(__name__)

# 可傳 analyzer 本身,或零參數的建立函式(第一次用到才建立,例如沒開 GPU 電腦時不必連 Ollama)
AnalyzerSource = Union[Analyzer, Callable[[], Analyzer]]


def _normalize(doc_type_hint: str | None) -> str | None:
    """去掉前後空白;空字串(網頁表單「不確定」可能送來)視為沒有提示。"""
    hint = (doc_type_hint or "").strip()
    return hint or None


class _Lazy:
    """第一次取用時才建立 analyzer,之後重複使用;網頁多執行緒同時上傳時只建立一次。"""

    def __init__(self, source: AnalyzerSource):
        self._source = source
        self._instance: Analyzer | None = source if hasattr(source, "analyze") else None
        self._lock = threading.Lock()

    def get(self) -> Analyzer:
        if self._instance is None:
            with self._lock:
                if self._instance is None:
                    self._instance = self._source()
        return self._instance


class RoutingAnalyzer:
    """依 local_only 與 doc_type_hint 分流到本機(local)或雲端(cloud)analyzer。

    走雲端的條件(四者皆成立):
    1. 沒有要求本機(local_only=False;Pipeline 在使用者選了醫療與保險、身分證明這類敏感大類時要求本機);
    2. 有提示(None、空字串視為沒有提示);
    3. 提示是系統認得的明確類型(models.DOC_TYPES,但不含「其他」)——打錯字、未知類型或
       「其他」都等於說不出是什麼文件,可能是敏感文件,不冒險;
    4. 不在 cfg.local_only_doc_types(預設為藥袋)。
    其餘一律本機;走本機時類型提示照樣交給本機 provider,類型專屬提示詞照用。

    殘餘風險:分流只能依「使用者選的大類與類型」事先決定。使用者若把藥袋誤選成發票,
    影像在辨識前就已送到雲端,事後無法收回。此時若雲端判斷結果屬於 local_only 類型,
    會在 result.notes 註記並寫 warning log,讓使用者與管理者知道發生過一次誤送;
    這是告知,不是防護。要完全避免,只能把 provider 設回 ollama(全部本機)。
    """

    def __init__(self, local: AnalyzerSource, cloud: AnalyzerSource, cfg: AppConfig):
        self.cfg = cfg
        self._local = _Lazy(local)
        self._cloud = _Lazy(cloud)

    def use_cloud(self, doc_type_hint: str | None, *, local_only: bool = False) -> bool:
        hint = _normalize(doc_type_hint)
        return (
            not local_only
            and hint is not None and hint in DOC_TYPES and hint != "其他"
            and hint not in self.cfg.local_only_doc_types
        )

    def analyze(self, file_path: Path, doc_type_hint: str | None = None, *,
                local_only: bool = False) -> ExtractionResult:
        hint = _normalize(doc_type_hint)
        if not self.use_cloud(hint, local_only=local_only):
            return self._local.get().analyze(file_path, doc_type_hint=hint)

        result = self._cloud.get().analyze(file_path, doc_type_hint=hint)
        if result.doc_type in self.cfg.local_only_doc_types:
            self._flag_misrouted(result, hint, file_path)
        return result

    def _flag_misrouted(self, result: ExtractionResult, hint: str, file_path: Path) -> None:
        note = (
            f"隱私提醒:這份文件被選為「{hint}」而送雲端辨識,但辨識結果是「{result.doc_type}」。"
            f"「{result.doc_type}」應只在本機處理,請確認類型是否選錯;下次請選「{result.doc_type}」或「不確定」。"
        )
        result.notes = f"{result.notes}\n{note}" if result.notes else note
        # log 只記類型與檔名,不記辨識出的內容
        log.warning(
            "隱私分流:%s 提示為「%s」已送雲端,但辨識結果為 local-only 類型「%s」",
            file_path.name, hint, result.doc_type,
        )
