"""藥袋 → 服藥時間表。只整理模型「照抄」的印刷文字,不推論、不補充任何藥物知識。

medication_schedule 行動的 payload 契約(tier 一律 confirm,由家人確認後才生效):
    {"title": "服藥時間表",
     "hospital": str,                  # 醫療院所(ExtractionResult.vendor),可空
     "dispensed_date": "YYYY-MM-DD"|"",  # 調劑日期(ExtractionResult.date)
     "slots": [{"slot": "早"|"中"|"晚"|"睡前", "items": [品項…]}],  # 依固定順序,只列有藥的時段
     "prn": [品項…],                   # 「需要時」服用,另列、不排進時段
     "unscheduled": [品項…],           # 藥袋沒印時段的藥:不推算,請家人對照藥袋
     "items": [品項…],                 # 全部品項(確認頁逐項顯示用)
     "pharmacist_phone": str,
     "disclaimer": DISCLAIMER}
    品項:{"name", "dose_text", "frequency_text", "timing": [...], "prn": bool, "days": int}

刻意不做:不從「一天三次」推算成早/中/晚(那是用藥判斷,不是轉錄);模型若多給學名、用途等
鍵一律丟掉,只留契約內的欄位。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from ..models import ExtractionResult

TITLE = "服藥時間表"
DISCLAIMER = "本系統只協助閱讀,不提供醫療建議;用藥請依醫師與藥師指示。"
SLOTS: tuple[str, ...] = ("早", "中", "晚", "睡前")
# 結構化輸出沒擋住時(例如雲端 JSON 模式不強制 enum)的常見寫法;不認得的一律丟掉
_SLOT_ALIASES = {
    "早": "早", "早上": "早", "上午": "早",
    "中": "中", "午": "中", "中午": "中",
    "晚": "晚", "晚上": "晚",
    "睡前": "睡前", "睡覺前": "睡前",
}
_TRUE_TEXT = {"true", "1", "是", "yes"}

MAX_ITEMS = 30        # 一張藥袋不會有這麼多品項,超過代表模型亂抄
MAX_TEXT_LEN = 60
MAX_DAYS = 365


def clip(value: Any, max_len: int = MAX_TEXT_LEN) -> str:
    """文件文字只當資料:去控制字元、壓縮空白、限制長度。"""
    text = "".join(ch for ch in str(value or "") if unicodedata.category(ch) != "Cc" or ch in "\t\n")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _timing(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    slots = {_SLOT_ALIASES.get(str(v).strip()) for v in value} - {None}
    return [s for s in SLOTS if s in slots]


def _prn(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE_TEXT


def _days(value: Any) -> int:
    try:
        days = int(float(value))
    except (TypeError, ValueError):
        return 0
    return days if 0 <= days <= MAX_DAYS else 0


def normalize_items(raw_items: Any) -> list[dict[str, Any]]:
    """只保留契約內的鍵並清洗型別;沒有藥名的品項丟掉。"""
    if not isinstance(raw_items, list):
        return []
    items = []
    for raw in raw_items[:MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        name = clip(raw.get("name"))
        if not name:
            continue
        items.append({
            "name": name,
            "dose_text": clip(raw.get("dose_text")),
            "frequency_text": clip(raw.get("frequency_text")),
            "timing": _timing(raw.get("timing")),
            "prn": _prn(raw.get("prn")),
            "days": _days(raw.get("days")),
        })
    return items


def build_schedule(result: ExtractionResult) -> dict[str, Any] | None:
    """把藥袋品項依時段分組;沒有任何可用品項時回 None(不產生行動)。"""
    fields = result.fields if isinstance(result.fields, dict) else {}
    items = normalize_items(fields.get("items"))
    if not items:
        return None

    prn = [i for i in items if i["prn"]]
    scheduled = [i for i in items if not i["prn"] and i["timing"]]
    unscheduled = [i for i in items if not i["prn"] and not i["timing"]]
    slots = [
        {"slot": slot, "items": [i for i in scheduled if slot in i["timing"]]}
        for slot in SLOTS
    ]
    return {
        "title": TITLE,
        "hospital": clip(result.vendor),
        "dispensed_date": result.date or "",
        "slots": [s for s in slots if s["items"]],
        "prn": prn,
        "unscheduled": unscheduled,
        "items": items,
        "pharmacist_phone": clip(fields.get("pharmacist_phone"), 30),
        "disclaimer": DISCLAIMER,
    }
