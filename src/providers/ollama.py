"""本地 Ollama provider:影像不離開本機。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..models import ExtractionResult
from ..parsing import ask_json, to_result
from ..preprocess import load_image_bytes
from ..prompts import get_prompt


class OllamaAnalyzer:
    """透過本地 Ollama 服務進行多模態文件辨識。

    client 可注入(測試用假物件,只需有 chat(**kwargs));未注入則連本機 Ollama。
    影像 token 預算:Ollama 官方 Modelfile 參數文件未列 Gemma 4 的影像預算選項,故不設定、用預設值。
    """

    def __init__(self, cfg: AppConfig, client: Any | None = None):
        self.cfg = cfg
        if client is None:
            import ollama

            client = ollama.Client(host=cfg.ollama.host, timeout=cfg.ollama.timeout)
        self.client = client

    def analyze(self, file_path: Path, doc_type_hint: str | None = None, *,
                local_only: bool = False) -> ExtractionResult:
        # local_only 不用看:本來就在本機推論
        image_bytes = load_image_bytes(file_path)
        prompt, schema = get_prompt(doc_type_hint)

        def ask(text: str) -> Any:
            response = self.client.chat(
                model=self.cfg.ollama.model,
                messages=[
                    {
                        "role": "user",
                        "content": text,
                        "images": [image_bytes],
                    }
                ],
                format=schema,              # 強制結構化 JSON 輸出(仍可能因輸出被截斷而不是合法 JSON)
                options={
                    "temperature": 0,       # 辨識任務不需要創意
                    # Ollama 預設 4096:手機照片加通用版提示詞就約 3,900 個 token,回答寫到一半被截斷(10/5 實測)
                    "num_ctx": self.cfg.ollama.num_ctx,
                },
                think=False,                # Gemma 4 預設先思考,輸出額度用完時 content 是空的(S0-3 實測)
            )
            return response.message.content

        # 回覆不是合法 JSON 就附上提醒重試一次;解析與重試規則與 Workers AI 共用(parsing.ask_json)
        raw = ask_json(ask, prompt, "Ollama", file_path.name)
        result = to_result(raw)
        result.source_model = f"ollama:{self.cfg.ollama.model}"
        return result
