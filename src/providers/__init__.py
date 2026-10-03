"""推論 provider:本地 Ollama、Cloudflare Workers AI、Mock。

每個 provider 只負責「影像 → 模型原始 JSON → ExtractionResult」;
清洗規則在 src/parsing.py,前處理在 src/preprocess.py,兩者共用。
雲端模式一律包在 RoutingAnalyzer 裡做隱私分流(藥袋、選了敏感大類、未指定類型只走本機)。
"""
from __future__ import annotations

import logging

from ..config import AppConfig
from ..models import CATEGORIES, SENSITIVE_CATEGORIES
from .base import Analyzer
from .mock import MockAnalyzer
from .ollama import OllamaAnalyzer
from .routing import RoutingAnalyzer

log = logging.getLogger(__name__)

__all__ = ["Analyzer", "MockAnalyzer", "OllamaAnalyzer", "RoutingAnalyzer", "create_analyzer"]


def create_analyzer(cfg: AppConfig, mock: bool = False) -> Analyzer:
    """依設定建立 provider;mock=True(命令列 --mock)優先於設定檔。

    provider 為 workers_ai 時回傳 RoutingAnalyzer:非敏感且有指定類型的文件送雲端,
    其餘(含使用者選了敏感大類、Pipeline 要求本機的文件)在本機 Ollama 辨識;
    本機端延遲建立,只用雲端時不需要 Ollama 服務。
    """
    provider = "mock" if mock else cfg.provider
    if provider == "mock":
        log.info("使用 Mock 模式(不呼叫任何模型)")
        return MockAnalyzer(cfg)
    if provider == "ollama":
        return OllamaAnalyzer(cfg)
    if provider == "workers_ai":
        from .workers_ai import WorkersAIAnalyzer

        log.info("使用雲端模式(Workers AI);%s、選了大類「%s」的文件,以及未指定類型的文件仍只在本機處理",
                 "、".join(cfg.local_only_doc_types),
                 "、".join(c for c in CATEGORIES if c in SENSITIVE_CATEGORIES))
        return RoutingAnalyzer(
            local=lambda: OllamaAnalyzer(cfg),
            cloud=WorkersAIAnalyzer(cfg),
            cfg=cfg,
        )
    raise ValueError(f"未知的 provider:{provider!r}")
