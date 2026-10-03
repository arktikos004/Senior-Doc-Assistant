"""模型輸出清洗:把任一 provider 回傳的 JSON 轉成 ExtractionResult。

與 provider 無關——Ollama、Workers AI、評測工具都走這裡,確保清洗規則只有一份;
「回覆怎樣才算合法 JSON、不合法時怎麼重試」也在這裡(parse_json_object、ask_json)。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from .dates import parse_date
from .models import COMMON_FIELDS, DOC_TYPES, ExtractionResult
from .prompts import JSON_ONLY_REMINDER, TYPE_FIELDS

log = logging.getLogger(__name__)


def parse_json_object(reply: Any) -> dict[str, Any]:
    """把模型回覆轉成 dict;容忍 ```json 圍欄與前後說明文字,其餘視為解析失敗(ValueError)。

    Ollama 的 format=schema 與 Workers AI 的 JSON 模式都不保證模型照做,兩邊共用這一份寬鬆規則;
    回覆已經是 dict(Workers AI 舊回應格式)就直接使用。
    """
    if isinstance(reply, dict):
        return reply
    if not isinstance(reply, str) or not reply.strip():
        raise ValueError("回覆為空")
    text = reply.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("回覆中找不到 JSON 物件") from None
        value = json.loads(text[start : end + 1])  # 仍不合法會丟 JSONDecodeError(ValueError 子類)
    if not isinstance(value, dict):
        raise ValueError(f"回覆是 {type(value).__name__},不是 JSON 物件")
    return value


def ask_json(ask: Callable[[str], Any], prompt: str, source: str, file_name: str) -> dict[str, Any]:
    """用 ask(提示詞) 取得模型回覆並解析成 JSON 物件;第一次不合法就附上 JSON_ONLY_REMINDER 再問一次。

    兩次都不合法丟 ValueError(Pipeline 會記成 failed)。ask 自己丟的例外(連線、HTTP 錯誤)
    不是解析問題,不重試、直接往外丟。source 只用於 log 與錯誤訊息(例「Ollama」);
    log 只記檔名與解析錯誤,不記模型回覆的內容。
    """
    last_error: ValueError | None = None
    for attempt, text in enumerate((prompt, prompt + JSON_ONLY_REMINDER), start=1):
        reply = ask(text)
        try:
            return parse_json_object(reply)
        except ValueError as exc:
            last_error = exc
            log.warning("%s 第 %d 次回應不是合法 JSON:%s(%s)", source, attempt, file_name, exc)
    raise ValueError(f"{source} 連續兩次未回傳合法 JSON:{last_error}") from last_error


def clean_date(value) -> str | None:
    """把日期正規化為 YYYY-MM-DD(只取年月日);解析一律交給 dates.parse_date。

    模型可能回傳帶時間的字串(如 '2026-08-03 17:08:26')、個位數月日(如 '2026-8-3')
    或沒換算的民國年(如 '115/08/03');冒號會讓歸檔檔名在 Windows 失敗,故一律只取年月日。
    抽不出年月日、或日期不存在(如 2 月 30 日)則回 None,不猜。
    """
    parsed = parse_date(value)
    return parsed.isoformat() if parsed else None


# 常見幣別別名 → ISO 代碼(無法判斷預設 NTD:系統只處理台灣文件)
_CURRENCY_ALIASES = {
    "NT": "NTD", "NT$": "NTD", "TWD": "NTD", "元": "NTD", "新臺幣": "NTD", "新台幣": "NTD",
    "$": "USD", "US": "USD", "US$": "USD", "USD$": "USD", "美元": "USD",
    "¥": "JPY", "日圓": "JPY", "€": "EUR", "RMB": "CNY", "人民幣": "CNY",
}


def clean_currency(value) -> str:
    """幣別正規化為 ISO 代碼;無法辨識預設 NTD。"""
    if not value:
        return "NTD"
    code = str(value).strip().upper()
    return _CURRENCY_ALIASES.get(code, _CURRENCY_ALIASES.get(str(value).strip(), code or "NTD"))


def _text(value) -> str | None:
    """文字欄位一律轉成去頭尾空白的字串,空字串視為沒有值(None)。

    雲端 JSON 模式不保證遵守 schema,模型可能把店名、號碼填成數字(例如 7111);
    不轉成字串的話,歸檔命名、網頁顯示等只接受字串的地方會拋 TypeError。
    """
    if value is None:
        return None
    return str(value).strip() or None


def _clean_unreadable(value) -> list[str]:
    """只保留認得的欄位名稱;類型專屬欄位以 'fields.<名稱>' 表示。"""
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        name = str(item).strip()
        if name in COMMON_FIELDS or name.startswith("fields."):
            out.append(name)
    return out


def _clean_fields(value, doc_type: str) -> dict[str, Any]:
    """只保留該類型的專屬欄位。

    通用提示詞的 schema 是所有類型欄位的聯集,模型會替其他類型的欄位填哨兵值;
    若不清掉,例如發票的結果裡會出現空的 due_date / items,網頁與驗證都得額外過濾。
    「其他」類型沒有專屬欄位,原樣保留供人工參考。
    """
    if not isinstance(value, dict):
        return {}
    allowed = TYPE_FIELDS.get(doc_type)
    if allowed is None:
        return dict(value)
    fields = {k: v for k, v in value.items() if k in allowed}
    # 帳單期限:模型可能照抄民國年(115/10/15),由程式換成西元(原則 2,使用者 10/2 決定);
    # 換不了就保留原文,交給核對判「沒通過」,不猜
    if doc_type == "帳單" and fields.get("due_date"):
        fields["due_date"] = clean_date(fields["due_date"]) or fields["due_date"]
    return fields


def to_result(raw: dict[str, Any]) -> ExtractionResult:
    """把模型回傳的 JSON 轉為 ExtractionResult,並做基本清洗。"""
    doc_type = raw.get("doc_type", "其他")
    if doc_type not in DOC_TYPES:
        doc_type = "其他"

    confidence = raw.get("confidence", 0.0)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.0

    amount = raw.get("amount")
    try:
        amount = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount = None
    # schema 以必填 number 取代 ["number","null"](小模型會偷懶選 null),
    # 提示詞約定「看不到填 0」,此處把 0 清洗回 None(金額為 0 的單據不存在)
    if amount == 0:
        amount = None

    return ExtractionResult(
        doc_type=doc_type,
        date=clean_date(raw.get("date")),
        vendor=_text(raw.get("vendor")),
        amount=amount,
        currency=clean_currency(raw.get("currency")),
        invoice_number=_text(raw.get("invoice_number")),
        confidence=confidence,
        notes=_text(raw.get("notes")) or "",
        raw=raw,
        unreadable=_clean_unreadable(raw.get("unreadable")),
        fields=_clean_fields(raw.get("fields"), doc_type),
        plain_summary=str(raw.get("plain_summary") or ""),
    )
