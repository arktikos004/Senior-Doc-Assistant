"""Cloudflare Workers AI provider(雲端模式):GPU 電腦沒開時,非敏感文件也能辨識。

藥袋等 local_only 類型不會走到這裡——分流在 src/providers/routing.py。
帳號 ID 與 API Token 只從環境變數讀(見 WorkersAIConfig),不寫進設定檔、不寫進 log。

依據的官方文件(2026-09 查閱):
- 端點與驗證:POST https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model},
  標頭 Authorization: Bearer <token>;回應外層為 {"result": ..., "success", "errors", "messages"}。
  https://developers.cloudflare.com/workers-ai/get-started/rest-api/
- 模型輸入/輸出 schema:messages 的 user content 可為 [{type:"image_url", image_url:{url}}, {type:"text"}],
  url 可放 data URI(data:image/jpeg;base64,…);輸出為 chat.completion,文字在 choices[0].message.content;
  chat_template_kwargs.enable_thinking 預設為 true。
  https://developers.cloudflare.com/workers-ai/models/gemma-4-26b-a4b-it/(頁面附的 sync-input.json /
  sync-output.json)、https://developers.cloudflare.com/api/resources/ai/methods/run/
- 結構化輸出:https://developers.cloudflare.com/workers-ai/features/json-mode/
  注意兩份文件不一致:JSON Mode 頁寫 response_format.json_schema 直接放 schema、回應在 result.response,
  且支援清單沒有列出 Gemma 4;模型頁的 schema 則是 OpenAI 格式 json_schema: {name, schema, strict}。
  這裡以「模型專屬 schema」為準送出請求,解析時兩種回應格式都接受;模型仍可能不守 schema,
  所以解析失敗時重試一次。
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import httpx

from ..config import AppConfig
from ..models import ExtractionResult
from ..parsing import ask_json, to_result
from ..preprocess import prepare_image
from ..prompts import get_prompt

API_BASE = "https://api.cloudflare.com/client/v4"
# 輸出上限:藥袋品項、白話解說可能讓 JSON 變長,留足空間避免被截斷成不合法 JSON
MAX_COMPLETION_TOKENS = 2048


class WorkersAIConfigError(RuntimeError):
    """缺少帳號 ID 或 API Token 的環境變數。"""


class WorkersAIError(RuntimeError):
    """Workers AI 回傳錯誤(HTTP 錯誤或 success=false)。"""


class WorkersAIAnalyzer:
    """透過 Cloudflare Workers AI REST API 進行多模態文件辨識。

    client 可注入 httpx.Client(測試用 MockTransport);未注入則自行建立並重複使用連線。
    """

    def __init__(self, cfg: AppConfig, client: httpx.Client | None = None):
        self.cfg = cfg
        self.client = client if client is not None else httpx.Client()

    def analyze(self, file_path: Path, doc_type_hint: str | None = None, *,
                local_only: bool = False) -> ExtractionResult:
        # 要求本機的文件(local_only)在 RoutingAnalyzer 就分到本機,不會走到這裡;收下參數只為符合介面
        account_id, token = self._credentials()  # 先檢查設定,缺了就不必讀檔
        model = self.cfg.workers_ai.model
        image = prepare_image(file_path)
        prompt, schema = get_prompt(doc_type_hint)
        url = f"{API_BASE}/accounts/{account_id}/ai/run/{model}"

        # 回覆不是合法 JSON 就附上提醒重試一次;HTTP 錯誤(WorkersAIError)不是解析問題,不重試
        raw = ask_json(
            lambda text: self._run(url, token, self._payload(image, text, schema)),
            prompt, "Workers AI", file_path.name,
        )
        result = to_result(raw)
        result.source_model = f"workers_ai:{model}"
        return result

    def _credentials(self) -> tuple[str, str]:
        """從環境變數讀帳號與金鑰;缺少時錯誤訊息只列出變數名稱,不含任何值。"""
        wcfg = self.cfg.workers_ai
        account_id = os.environ.get(wcfg.account_id_env, "").strip()
        token = os.environ.get(wcfg.api_token_env, "").strip()
        missing = [
            name
            for name, value in ((wcfg.account_id_env, account_id), (wcfg.api_token_env, token))
            if not value
        ]
        if missing:
            raise WorkersAIConfigError(
                f"Workers AI 缺少環境變數:{'、'.join(missing)}。"
                "請設定 Cloudflare 帳號 ID 與 Workers AI API Token 後再試,"
                "或把 model.provider 改回 ollama。"
            )
        return account_id, token

    def _payload(self, image, text: str, schema: dict) -> dict[str, Any]:
        data_uri = f"data:{image.mime_type};base64,{base64.b64encode(image.data).decode('ascii')}"
        return {
            "messages": [
                {
                    "role": "user",
                    # 影像放在文字前面(Gemma 系列建議的多模態提示順序)
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_uri}},
                        {"type": "text", "text": text},
                    ],
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "document_extraction", "schema": schema},
            },
            "temperature": 0,           # 辨識任務不需要創意
            "max_completion_tokens": MAX_COMPLETION_TOKENS,
            "stream": False,            # JSON Mode 不支援串流
            # 轉錄任務不需要推理過程;關掉可縮短回應時間(Gemma 4 預設開啟)
            "chat_template_kwargs": {"enable_thinking": False},
        }

    def _run(self, url: str, token: str, payload: dict[str, Any]) -> Any:
        """送出請求並取出模型回覆(字串,或已解析的 dict)。"""
        response = self.client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=self.cfg.workers_ai.timeout,
        )
        try:
            envelope = response.json()
        except ValueError:
            envelope = None

        if response.status_code >= 400 or not isinstance(envelope, dict) or envelope.get("success") is False:
            raise WorkersAIError(f"Workers AI 請求失敗(HTTP {response.status_code}):{_error_text(envelope)}")
        return _extract_reply(envelope.get("result"))


def _error_text(envelope: Any) -> str:
    """取出 Cloudflare 信封裡的錯誤訊息(不含請求內容與標頭,不會洩漏 token)。"""
    if isinstance(envelope, dict):
        errors = envelope.get("errors") or []
        messages = [
            str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors
        ]
        if messages:
            return ";".join(messages)[:300]
    return "回應格式無法辨識"


def _extract_reply(result: Any) -> Any:
    """chat.completion 格式取 choices[0].message.content;JSON Mode 舊格式取 result.response。"""
    if not isinstance(result, dict):
        raise WorkersAIError("Workers AI 回應缺少 result")
    choices = result.get("choices")
    if choices:
        message = choices[0].get("message") or {}
        return message.get("content")
    if "response" in result:
        return result["response"]
    raise WorkersAIError("Workers AI 回應缺少模型輸出(choices / response)")
