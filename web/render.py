"""網頁渲染:Jinja2 樣板(web/templates/)+ 本地樣式(web/static/),無外部 CDN,離線可用。

autoescape 全開:所有來自檔名、模型輸出的字串都會被跳脫,避免惡意檔名或
文件內容造成 XSS。這裡也負責把 SQLite 的文件/行動整理成樣板好用的「畫面資料」,
樣板只管排版,不做判斷。
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from src.actions import BILL_TITLES, CALENDAR, MEDICATION_SCHEDULE
from src.config import DEFAULT_OLLAMA_MODEL, DEFAULT_WORKERS_AI_MODEL, AppConfig
from src.dates import parse_date
from src.models import (
    CATEGORIES,
    CATEGORY_DOCS,
    CATEGORY_TYPES,
    DOC_LABELS,
    DOC_TYPES,
    IDENTITY_CATEGORY,
    SENSITIVE_CATEGORIES,
    UNCATEGORIZED,
    catalog_entry,
)
from src.parsing import clean_currency
from src.settings import PROVIDER, PURGED, THRESHOLD, THRESHOLD_CHOICES, THRESHOLD_MAX, THRESHOLD_MIN
from src.verify import VERIFY_ERROR_SUMMARY
from src.verify.einvoice import (
    CHECK_QR_BUYER,
    CHECK_QR_DATE,
    CHECK_QR_INVOICE,
    CHECK_QR_RANDOM,
    CHECK_QR_SELLER,
    CHECK_QR_TOTAL,
    INDEPENDENT_CHECKS,
)
from web.fields import field_spec, field_specs, required_specs

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def is_pdf(path: Any) -> bool:
    """原件是不是 PDF(看副檔名,上傳時已比對過檔頭);Path 或字串都可以,沒有就是 False。

    PDF 不能當 <img> 顯示,也讀不到寬高:結果頁、待複核與家人確認的縮圖都用這個判斷。
    """
    return str(path or "").lower().endswith(".pdf")


# 上傳時沒選大類的類型選項:(送出值, 畫面文字)。「不確定」送出後轉成 None,交給模型判斷。
# 送出值必須是 src.models.DOC_TYPES 裡的原字串:provider 的隱私分流只認這些值,
# 其他任何字串都會被當成「沒有提示」而留在本機。選了大類時改列那一類的常見文件(src.models.CATEGORY_DOCS)。
UNSURE = "不確定"
DOC_TYPE_CHOICES: tuple[tuple[str, str], ...] = (
    (UNSURE, "不確定"),
    ("藥袋", "藥袋"),
    ("帳單", "帳單"),
    ("公文", "公文"),
    ("發票", "發票"),
    ("收據", "收據"),
)
TYPE_VALUES: tuple[str, ...] = tuple(v for v, _ in DOC_TYPE_CHOICES if v != UNSURE and v in DOC_TYPES)

MEDICATION_DISCLAIMER = "本系統只協助閱讀,不提供醫療建議;用藥請依醫師與藥師指示。"

# 產品名稱集中在這裡(10/1 定案;舊的英文名稱撞名,已棄用)。短名稱「看有」= 台語 khuànn-ū「看得懂」,
# 副標說明用途;正式題目「結合視覺語言模型與分層核對機制之高齡家庭文書輔助系統」只用在報名與文件
BRAND_NAME = "看有"
BRAND_SUB = "高齡家庭文書輔助"
BRAND_FULL = f"{BRAND_NAME} {BRAND_SUB}"

# ---- 圖示 -------------------------------------------------------------------
# 手繪的線條圖示(24×24,描邊用 currentColor),內嵌 SVG 不需外部資源。
# 一律 aria-hidden:意思由旁邊的文字表達,圖示只是第二重提示。
_ICON_PATHS: dict[str, str] = {
    "camera": '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9'
              'a2 2 0 0 0-2-2h-3z"/><circle cx="12" cy="13" r="3.5"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/>'
             '<path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
    "speaker": '<path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/>'
               '<path d="M19 5a10 10 0 0 1 0 14"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "undo": '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    "dash": '<path d="M5 12h14"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "calendar": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4"/>'
                '<path d="M8 3v4"/><path d="M3 10h18"/><path d="m9 15.5 2 2 4-4"/>',
    "alert": '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9'
             'a2 2 0 0 0-3.4 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 16v-5"/><path d="M12 8h.01"/>',
    "hand": '<circle cx="12" cy="12" r="9"/><path d="M12 7v6"/><path d="M12 16.5h.01"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
    "external": '<path d="M15 3h6v6"/><path d="M10 14 21 3"/>'
                '<path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/>',
    "download": '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
    "lock": '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    "eye": '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "pill": '<path d="m10.5 20.5 10-10a4.95 4.95 0 1 0-7-7l-10 10a4.95 4.95 0 1 0 7 7z"/>'
            '<path d="m8.5 8.5 7 7"/>',
    "file": '<path d="M6 2h9l5 5v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z"/><path d="M14 2v6h6"/>',
    "pencil": '<path d="M21.2 6.8a2.5 2.5 0 0 0-3.5-3.5L4 17v3.5h3.5z"/><path d="m15 5 4 4"/>',
    "plus": '<path d="M12 5v14"/><path d="M5 12h14"/>',
    # 文件類型(圖示方塊裡的白色線條)
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.2a2.6 2.6 0 1 1 3.6 2.4c-.7.3-1.1.9-1.1 1.6v.5"/>'
            '<path d="M12 16.8h.01"/>',
    "bill": '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 7.5h8"/><path d="M8 11h5"/>'
            '<rect x="11.5" y="14" width="5" height="3.5" rx="1"/>',
    "envelope": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 7 8.5 6 8.5-6"/>',
    "qr": '<rect x="3.5" y="3.5" width="6.5" height="6.5" rx="1"/><rect x="14" y="3.5" width="6.5" height="6.5" rx="1"/>'
          '<rect x="3.5" y="14" width="6.5" height="6.5" rx="1"/><path d="M14 14h2.5v2.5H14z"/>'
          '<path d="M20.5 14v6.5H17"/>',
    "receipt": '<path d="M5 3h14v18l-2.3-1.4-2.4 1.4-2.3-1.4-2.3 1.4-2.4-1.4L5 21z"/><path d="M9 8h6"/>'
               '<path d="M9 12h6"/>',
    "circle": '<circle cx="12" cy="12" r="8.5"/>',
    # 導覽(電腦左側選單;手機「選單」)
    "menu": '<path d="M4 7h16"/><path d="M4 12h16"/><path d="M4 17h16"/>',
    "home":'<path d="M3.5 10.5 12 3.5l8.5 7"/><path d="M5.5 9v10.5a1 1 0 0 0 1 1H10v-6h4v6h3.5a1 1 0 0 0 1-1V9"/>',
    "family": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/>'
              '<path d="M16 4.8a3.5 3.5 0 0 1 0 6.4"/><path d="M18 14.3a6.5 6.5 0 0 1 3.5 5.7"/>',
    "tray": '<path d="M3 13.5 5.6 5.2A2 2 0 0 1 7.5 4h9a2 2 0 0 1 1.9 1.2l2.6 8.3"/>'
            '<path d="M3 13.5V19a1.5 1.5 0 0 0 1.5 1.5h15A1.5 1.5 0 0 0 21 19v-5.5h-5.5a3.5 3.5 0 0 1-7 0z"/>',
    "folder": '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5H9l2 2.5h8.5A1.5 1.5 0 0 1 21 9v9.5a1.5 1.5 0 0 1-1.5 1.5h-15'
              'A1.5 1.5 0 0 1 3 18.5z"/>',
    "gear": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1'
            'a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3'
            'l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1'
            'a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9'
            'a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1'
            'a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1'
            'a1.7 1.7 0 0 0-1.5 1z"/>',
    "sparkles": '<path d="M11 3.5 12.8 8.2l4.7 1.8-4.7 1.8L11 16.5l-1.8-4.7L4.5 10l4.7-1.8z"/>'
                '<path d="M18.5 14.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"/>',
    "shield": '<path d="M12 3 4.5 6v5.5c0 4.6 3.1 8.6 7.5 9.5 4.4-.9 7.5-4.9 7.5-9.5V6z"/><path d="m9 12 2 2 4-4"/>',
}

# 文件類型 → 圖示(類型選項、清單前面的小圖示;意思由旁邊的文字表達)
TYPE_ICONS: dict[str, str] = {
    UNSURE: "help", "藥袋": "pill", "帳單": "bill", "公文": "envelope", "發票": "qr", "收據": "receipt",
}


def type_icon(doc_type: Any) -> str:
    return TYPE_ICONS.get(str(doc_type or ""), "file")


def icon(name: str, cls: str = "") -> Markup:
    """回傳內嵌 SVG 圖示;name 只接受程式內的常數,不接受外部輸入。cls 是額外的修飾 class(icon 會自動加)。"""
    paths = _ICON_PATHS.get(name, _ICON_PATHS["dash"])
    return Markup('<svg class="{}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">{}</svg>').format(
        f"icon {cls}".strip(), Markup(paths))


# ---- 格式化 -----------------------------------------------------------------

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def fmt_date(value: Any) -> str:
    """YYYY-MM-DD → 2026年9月30日;其他格式原樣回傳。"""
    if not isinstance(value, str):
        return "" if value is None else str(value)
    m = _ISO_DATE.match(value.strip())
    if not m:
        return value
    y, mo, d = m.groups()
    return f"{int(y)}年{int(mo)}月{int(d)}日"


def fmt_short_date(value: Any) -> str:
    """YYYY-MM-DD → 10月20日(摘要與提醒用;完整年份在「讀到的內容」裡);其他格式回傳空字串。"""
    parts = date_parts(value)
    return f"{parts[0]}{parts[1]}日" if parts else ""


def date_parts(value: Any) -> tuple[str, str] | None:
    """YYYY-MM-DD → ("10月", "20"),給日曆小方塊用;其他格式回傳 None。"""
    m = _ISO_DATE.match(value.strip()) if isinstance(value, str) else None
    if not m:
        return None
    _, mo, d = m.groups()
    return f"{int(mo)}月", str(int(d))


def fmt_when(iso: Any) -> str:
    """處理時間 2026-09-30T14:03:12 → 9月30日 14:03(給清單與頁首用)。"""
    try:
        dt = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return str(iso or "")
    return f"{dt.month}月{dt.day}日 {dt:%H:%M}"


def _is_number(x: Any) -> bool:
    """模型讀到的數字(int 或 float);bool 不算,True 不是 1 元。"""
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def fmt_amount(amount: Any, currency: Any = "NTD") -> str:
    if not _is_number(amount):
        return "" if amount in (None, "") else str(amount)
    text = f"{amount:,.0f}" if float(amount).is_integer() else f"{amount:,.2f}"
    cur = (currency or "NTD").upper()
    return f"{text} 元" if cur in ("NTD", "TWD") else f"{text} {cur}"


def conf_class(conf: Any) -> str:
    if not _is_number(conf):
        return ""
    if conf < 0.5:
        return "low"
    if conf < 0.8:
        return "mid"
    return ""


def fmt_conf(conf: Any) -> str:
    return f"{conf:.2f}" if _is_number(conf) else "—"


def fmt_pct(conf: Any) -> str:
    """驗證信心用百分比,和結果頁「驗證信心 85%」一致。"""
    return f"{conf * 100:.0f}%" if _is_number(conf) else "—"


def _is_empty(value: Any) -> bool:
    """模型用哨兵值(""、0、[])代表「沒有」,畫面上一律當成沒讀到。"""
    return value in (None, "", 0, [], {}) and not isinstance(value, bool)


def _plain(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, dict):
        return ";".join(f"{k}:{_plain(v)}" for k, v in value.items() if not _is_empty(v))
    if isinstance(value, (list, tuple)):
        return "、".join(_plain(v) for v in value if not _is_empty(v))
    return str(value)


# ---- 欄位表 -----------------------------------------------------------------
# 欄位名稱、種類、必填與顯示順序都在 web/fields.py(FieldSpec)


def _field_value(result: dict[str, Any], key: str) -> Any:
    if key.startswith("fields."):
        return (result.get("fields") or {}).get(key.split(".", 1)[1])
    return result.get(key)


def _field_aliases(name: str) -> set[str]:
    """同一個欄位的寫法:類型專屬欄位寫 'due_date' 或 'fields.due_date' 都算(模型與各檢查的寫法不一)。"""
    bare = name.removeprefix("fields.")
    return {name, bare, f"fields.{bare}"}


def _is_unreadable(key: str, unreadable: Any) -> bool:
    """模型自己標成讀不清的欄位;unreadable 是 result["unreadable"],只認字串。"""
    names = {u for u in unreadable or () if isinstance(u, str)}
    return not names.isdisjoint(_field_aliases(key))


def _checks(verification: Any) -> list[tuple[str, dict[str, Any]]]:
    """verification 裡真正的檢查項目 (名稱, 內容);略過 _coverage 這類底線開頭的附註與格式不對的項目。"""
    if not isinstance(verification, dict):
        return []
    return [(name, check) for name, check in verification.items()
            if not name.startswith("_") and isinstance(check, dict)]


_CHECK_RANK = {"pass": 1, "match": 2, "fail": 3}


def _field_checks(verification: dict[str, Any]) -> dict[str, str]:
    """欄位 → 最嚴重的驗證狀態;欄位名同時接受 'due_date' 與 'fields.due_date'。

    fail 優先;pass 再分兩種:match 是與 QR 等獨立證據相符,pass 只通過格式或合理性規則(弱證據)。
    """
    status: dict[str, str] = {}
    for name, check in _checks(verification):
        st = check.get("status")
        if st not in ("pass", "fail"):
            continue
        if st == "pass" and name in INDEPENDENT_CHECKS:
            st = "match"
        for f in check.get("fields") or []:
            if not isinstance(f, str):
                continue
            for k in _field_aliases(f):
                if _CHECK_RANK[st] > _CHECK_RANK.get(status.get(k, ""), 0):
                    status[k] = st
    return status


def field_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    """整理「讀到的內容」表格:有值的欄位 + 必要欄位(沒讀到也要列出,提醒家人補)。"""
    doc_type = result.get("doc_type") or "其他"
    fields = result.get("fields") or {}
    checks = _field_checks(result.get("verification") or {})

    # 這個類型的欄位在前;模型多給的類型專屬欄位(有值才列)接在後面
    specs = list(field_specs(doc_type))
    listed = {spec.key for spec in specs}
    specs += [field_spec(doc_type, f"fields.{name}") for name in fields
              if name != "items" and f"fields.{name}" not in listed]

    rows = []
    for spec in specs:
        key = spec.key
        if key == "fields.items":
            continue  # 藥品清單另外排成藥品表
        value = _field_value(result, key)
        is_unreadable = _is_unreadable(key, result.get("unreadable"))
        if _is_empty(value) and not is_unreadable and not spec.required:
            continue
        row: dict[str, Any] = {"key": key, "label": spec.label,
                               "value": "", "value_list": None, "missing": ""}
        if is_unreadable:
            row["missing"] = "讀不清楚,請看原件"
        elif _is_empty(value):
            row["missing"] = "沒有讀到"
        elif spec.kind == "amount":
            row["value"] = fmt_amount(value, result.get("currency"))
        elif spec.kind == "date":
            row["value"] = fmt_date(value)
        elif isinstance(value, (list, tuple)):
            row["value_list"] = [_plain(v) for v in value if not _is_empty(v)]
        else:
            row["value"] = _plain(value)
        st = checks.get(key)
        row["check"] = "pass" if st == "match" else st
        # 只過規則的欄位寫「已檢查」,不寫成被證實
        row["check_text"] = {"match": "與 QR 相符", "pass": "已檢查", "fail": "不符"}.get(st or "", "")
        rows.append(row)
    return rows


# ---- 藥袋 -------------------------------------------------------------------

TIMINGS = ("早", "中", "晚", "睡前")
# 同 src/actions/medication.py 認得的寫法(更正表單的勾選要和服藥時間表一致),再加上英文
_TIMING_ALIASES = {"早上": "早", "上午": "早", "午": "中", "中午": "中", "晚上": "晚", "睡覺前": "睡前",
                   "morning": "早", "noon": "中", "evening": "晚", "bedtime": "睡前"}
_PRN_WORDS = ("需要時", "必要時", "prn")


def _timing_label(value: Any) -> str:
    text = str(value).strip()
    return _TIMING_ALIASES.get(text, _TIMING_ALIASES.get(text.lower(), text))


def med_item(raw: Any) -> dict[str, Any]:
    """一筆藥品(照藥袋印刷文字轉錄;不補任何藥物知識)。字串也接受,當作藥名。"""
    if not isinstance(raw, dict):
        return {"name": _plain(raw), "dose_text": "", "frequency_text": "",
                "timing": [], "prn": False, "days": ""}
    timing = raw.get("timing") or []
    if isinstance(timing, str):
        timing = [t for t in re.split(r"[、,,/\s]+", timing) if t]
    days = raw.get("days")
    return {
        "name": _plain(raw.get("name") or raw.get("drug") or "(藥名讀不清)"),
        "dose_text": _plain(raw.get("dose_text") or ""),
        "frequency_text": _plain(raw.get("frequency_text") or ""),
        "timing": [_timing_label(t) for t in timing if not _is_empty(t)],
        "prn": bool(raw.get("prn")),
        "days": f"{days} 天" if _is_number(days) and days else "",
    }


def med_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    items = (result.get("fields") or {}).get("items")
    return [med_item(it) for it in items] if isinstance(items, list) else []


def _names(entries: Any) -> list[str]:
    if isinstance(entries, (str, dict)):
        entries = [entries]
    if not isinstance(entries, (list, tuple)):
        return []
    return [med_item(e)["name"] for e in entries if not _is_empty(e)]


# 服藥時間表 payload(W1-C 契約,src/actions):
#   {"title", "hospital", "dispensed_date",
#    "slots": [{"slot": "早"|"中"|"晚"|"睡前", "items": [item…]}],   # 只列有藥的時段
#    "prn": [item…], "unscheduled": [item…],                        # 需要時 / 有頻率但沒時段
#    "items": [item…], "pharmacist_phone", "disclaimer"}
# 缺鍵時容錯:沒有 slots/prn/unscheduled 才改用 items 自己分組;未知鍵原樣(跳脫)列出。
_MED_GROUP_KEYS = ("slots", "schedule", "by_timing", "groups")
_MED_KNOWN_KEYS = {"items", "prn", "prn_items", "as_needed", "unscheduled", "note", "notes", "title",
                   "hospital", "dispensed_date", "pharmacist_phone", "disclaimer", *_MED_GROUP_KEYS}
UNSCHEDULED_NOTE = "時段請依藥袋或詢問藥師"


def medication_view(payload: dict[str, Any]) -> dict[str, Any]:
    """把服藥時間表的 payload 整理成「時段 → 藥名」+「需要時」+「未標時段」。

    只重排 payload 裡已有的藥名,不推論時段:沒有時段的藥一律放「未標時段」,
    畫面提示「時段請依藥袋或詢問藥師」。
    """
    payload = payload if isinstance(payload, dict) else {}
    slots: dict[str, list[str]] = {}
    prn: list[str] = []
    unscheduled: list[str] = []

    def extend(bucket: list[str], names: list[str]) -> None:
        bucket.extend(n for n in names if n not in bucket)

    def add(slot: str, names: list[str]) -> None:
        if not names:
            return
        if slot.lower() in _PRN_WORDS or slot in _PRN_WORDS:
            extend(prn, names)
        else:
            extend(slots.setdefault(_timing_label(slot), []), names)

    structured = False
    for key in _MED_GROUP_KEYS:
        group = payload.get(key)
        if isinstance(group, list):          # W1-C:[{"slot": "早", "items": [...]}]
            for entry in group:
                if isinstance(entry, dict) and entry.get("slot"):
                    add(str(entry["slot"]), _names(entry.get("items")))
                    structured = True
        elif isinstance(group, dict):        # 容錯:{"早": [...], "睡前": [...]}
            for slot, entries in group.items():
                add(str(slot), _names(entries))
                structured = True
    for key in (*TIMINGS, *_TIMING_ALIASES):
        if key in payload:
            add(key, _names(payload[key]))
            structured = True
    for key in ("prn", "prn_items", "as_needed"):
        if isinstance(payload.get(key), (list, tuple, str)):
            extend(prn, _names(payload[key]))
            structured = True
    if isinstance(payload.get("unscheduled"), (list, tuple, str)):
        extend(unscheduled, _names(payload["unscheduled"]))
        structured = True

    items = payload.get("items")
    if not structured and isinstance(items, list):
        for raw in items:
            it = med_item(raw)
            if it["prn"]:
                extend(prn, [it["name"]])
            for t in it["timing"]:
                add(t, [it["name"]])
            if not it["prn"] and not it["timing"]:
                extend(unscheduled, [it["name"]])

    info = []
    for key, label in (("hospital", "醫療院所"), ("dispensed_date", "調劑日期"),
                       ("pharmacist_phone", "藥師電話")):
        if not _is_empty(payload.get(key)):
            value = fmt_date(payload[key]) if key == "dispensed_date" else _plain(payload[key])
            info.append(f"{label}:{value}")
    ordered = [(t, slots.pop(t)) for t in TIMINGS if t in slots] + list(slots.items())
    extra = [
        (str(k), _plain(v)) for k, v in payload.items()
        if k not in _MED_KNOWN_KEYS and k not in TIMINGS and k not in _TIMING_ALIASES and not _is_empty(v)
    ]
    note = _plain(payload.get("note") or payload.get("notes") or "")
    disclaimer = _plain(payload.get("disclaimer") or "")
    return {"slots": ordered, "prn": prn, "unscheduled": unscheduled, "info": info,
            "extra": extra, "note": note, "disclaimer": disclaimer}


# ---- 自我驗證 ---------------------------------------------------------------

# 與獨立證據(電子發票 QR)比對的檢查說「相符/不符」;格式、檢查碼、合理性只是規則,
# 通過不代表讀對,說「通過/沒通過」(弱證據不得呈現成已證實)
_STATUS_TEXT = {"pass": "相符", "fail": "不符", "skip": "無法核對"}
_RULE_STATUS_TEXT = {"pass": "通過", "fail": "沒通過", "skip": "無法核對"}
_STATUS_ICON = {"pass": "check", "fail": "x", "skip": "dash"}


def verification_view(result: dict[str, Any]) -> dict[str, Any]:
    """把 result["verification"] 整理成一行打勾列與明細。沒有資料時 overall 為 "none"。

    conflict:有與獨立證據矛盾的檢查;evidence:有任何與獨立證據比對過的檢查(相符或不符)。
    沒有 evidence 時整份只有弱證據,note 要講清楚「不能證明讀對」。
    """
    checks = []
    for name, check in _checks(result.get("verification")):
        status = check.get("status") if check.get("status") in _STATUS_TEXT else "skip"
        independent = name in INDEPENDENT_CHECKS
        checks.append({
            "name": name,
            "label": name,   # 檢查名稱本身就是中文(src/verify 的 CHECK_* 常數),細節看 detail
            "status": status,
            "status_text": (_STATUS_TEXT if independent else _RULE_STATUS_TEXT)[status],
            "independent": independent,
            "icon": _STATUS_ICON[status],
            "detail": _plain(check.get("detail") or ""),
        })
    counts = {s: sum(1 for c in checks if c["status"] == s) for s in _STATUS_TEXT}
    matched = sum(1 for c in checks if c["independent"] and c["status"] == "pass")
    conflict = any(c["independent"] and c["status"] == "fail" for c in checks)
    evidence = any(c["independent"] and c["status"] != "skip" for c in checks)
    if not checks:
        overall, text = "none", "未核對"
    elif counts["fail"]:
        overall, text = "fail", f"有 {counts['fail']} 項{'不符' if conflict else '沒通過'},請對照原件"
    elif matched:
        overall, text = "pass", f"QR Code 相符 {matched} 項"
    elif counts["pass"]:
        overall, text = "pass", f"檢查通過 {counts['pass']} 項"
    else:
        overall, text = "skip", "這份文件沒有能自動核對的項目"

    if evidence:
        note = "核對由程式比對發票上的 QR Code,並檢查格式與檢查碼,不是 AI 自己打分數。"
    elif counts["pass"] or counts["fail"]:
        note = "這份文件沒有 QR Code 可以比對,程式只檢查格式與合理性,不能證明讀對,請對照原件。"
    else:
        note = ""
    vc = result.get("verified_confidence")
    confidence_text = ""
    if _is_number(vc):
        confidence_text = f"驗證信心 {vc * 100:.0f}%(依核對結果計算,不是 AI 自評)"
    return {"checks": checks, "counts": counts, "overall": overall,
            "overall_text": text, "overall_icon": _STATUS_ICON.get(overall, "dash"),
            "conflict": conflict, "evidence": evidence, "note": note,
            "confidence_text": confidence_text}


# ---- 行動 -------------------------------------------------------------------

_KIND_LABELS = {CALENDAR: "期限提醒", MEDICATION_SCHEDULE: "服藥時間表"}
_KIND_ICONS = {CALENDAR: "calendar", MEDICATION_SCHEDULE: "pill"}

# 取消/恢復提醒(F8)的表單值;按鈕文字與圖示依提醒目前是否生效決定
REMINDER_CANCEL, REMINDER_RESTORE = "cancel", "restore"
BILL_DEADLINE_NOTE = "期限由 AI 讀取,請對照帳單。"   # 自動提醒的期限沒有人看過


def is_auto_reminder(action: dict[str, Any]) -> bool:
    """自動列入的期限提醒:只有它能在結果頁取消與恢復(原則 4);要家人確認的行動在「家人確認」處理。"""
    return action.get("kind") == CALENDAR and action.get("tier") == "auto"


def _action_state(action: dict[str, Any]) -> tuple[str, str, str]:
    """(畫面文字, 樣式, 圖示):依分級與狀態。自動行動建立時就已完成,只看是否被退回。"""
    tier, status, kind = action.get("tier"), action.get("status"), action.get("kind")
    if status == "rejected":
        return ("家人已退回" if tier == "confirm" else "已取消", "off", "x")
    if tier == "auto":
        return ("已列入提醒" if kind == CALENDAR else "已自動處理", "auto", "check")
    if tier == "confirm":
        if status == "done":
            return ("家人已確認", "auto", "check")
        return ("等待家人確認", "wait", "clock")
    if status == "done":
        return ("已處理", "auto", "check")
    return ("需要人工處理", "manual", "hand")


def action_view(action: dict[str, Any]) -> dict[str, Any]:
    """一筆行動的畫面資料;payload 內容一律當資料顯示(跳脫),不影響分級與按鈕。"""
    kind = str(action.get("kind") or "")
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    state_text, state_class, state_icon = _action_state(action)
    view: dict[str, Any] = {
        "id": action.get("id"), "document_id": action.get("document_id"),
        "kind": kind, "kind_label": _KIND_LABELS.get(kind, "其他待辦"),
        "kind_icon": _KIND_ICONS.get(kind, "file"),
        "tier": action.get("tier"), "status": action.get("status"),
        "state_text": state_text, "state_class": state_class, "state_icon": state_icon,
        "title": "", "lines": [], "medication": None,
    }
    if kind == CALENDAR:
        # 提醒只列在網頁上(2026-10-01 拿掉 .ics):沒有東西會提前通知,所以不顯示 remind_days_before
        view["title"] = _plain(payload.get("title") or "期限提醒")
        if payload.get("date"):
            view["lines"].append(f"日期:{fmt_date(payload['date'])}")
        # description 是多行文字(W1-C:最後一行是「只提醒,不會替您付款或回覆」),逐行顯示
        for line in str(payload.get("description") or "").splitlines():
            if line.strip():
                view["lines"].append(line.strip())
        view["date_text"] = fmt_date(payload.get("date") or "")
        view["month"], view["day"] = date_parts(payload.get("date")) or ("", "")
    elif kind == MEDICATION_SCHEDULE:
        view["title"] = _plain(payload.get("title") or "服藥時間表")
        view["medication"] = medication_view(payload)
    else:
        view["title"] = _plain(payload.get("title") or view["kind_label"])
        view["lines"] = [f"{k}:{_plain(v)}" for k, v in payload.items()
                         if k != "title" and not _is_empty(v)]
    return view


# ---- 文件 -------------------------------------------------------------------

_MEASURE = {"發票": "一張發票", "收據": "一張收據", "帳單": "一張帳單", "公文": "一份公文", "藥袋": "一個藥袋"}
# 文件狀態用長輩看得懂的字:「歸檔、人工複核」是系統用語(10/1 稽核)
_DECISION_STATE = {
    "archive": ("已存檔", "auto", "check"),
    "review": ("等待複核", "wait", "clock"),
    "failed": ("讀不出來", "manual", "alert"),
}


def _decision_state(doc: dict[str, Any]) -> tuple[str, str, str]:
    """文件狀態 (畫面文字, 樣式, 圖示);還沒有決策結果時是「處理中」。"""
    return _DECISION_STATE.get(doc.get("action"), ("處理中", "wait", "clock"))
# 轉人工的白話原因:QR 比對項目 → 看得懂的欄位名
_QR_PLAIN = {CHECK_QR_TOTAL: "金額", CHECK_QR_DATE: "日期", CHECK_QR_INVOICE: "發票號碼",
             CHECK_QR_SELLER: "賣方統編", CHECK_QR_BUYER: "買方統編", CHECK_QR_RANDOM: "隨機碼"}


def review_reason(result: dict[str, Any] | None) -> str:
    """為什麼要複核,用白話說;不寫「驗證信心 0.10、門檻 0.80」這類工程用語(數字留在核對區)。

    順序比照 src/decision.py:不支援的類型 → 核對程式出錯 → 與 QR 矛盾 → 規則沒通過 → 必要欄位沒讀到 → AI 沒把握。
    只依讀值與核對結果組句,不推論。
    """
    result = result or {}
    doc_type = result.get("doc_type") or ""
    required = required_specs(doc_type)
    if not required:   # 支援的類型都有必要欄位(REQUIRED_FIELDS),沒有就是不支援
        return "這類文件不在支援範圍(發票、收據、帳單、公文、藥袋),請對照原件確認。"
    verification = result.get("verification")
    if isinstance(verification, dict) and verification.get("_summary") == VERIFY_ERROR_SUMMARY:
        return "這次的核對沒有完成(核對程式出錯),請對照原件確認。"
    failed = [name for name, check in _checks(result.get("verification")) if check.get("status") == "fail"]
    conflicts = [name for name in failed if name in INDEPENDENT_CHECKS]
    if conflicts:
        words = "、".join(dict.fromkeys(_QR_PLAIN.get(n, n.removeprefix("QR ")) for n in conflicts))
        return f"讀到的{words}和發票上的 QR Code 不一樣,請對照原件確認。"
    if failed:
        return f"有 {len(failed)} 項檢查沒通過,請對照原件確認。"
    missing = [spec.label for spec in required
               if _is_empty(_field_value(result, spec.key)) or _is_unreadable(spec.key, result.get("unreadable"))]
    if missing:
        return f"有必要的欄位沒讀到({'、'.join(missing)}),請對照原件補上。"
    return "AI 對這份文件的讀值沒有把握,請對照原件確認。"


def doc_review_reason(doc: dict[str, Any]) -> str:
    """一份文件為什麼要複核:上傳時選了交給家人確認的文件(保單、身分證…)或身分證明類,一律轉人工
    (決策層級的規則,讀值再好也一樣),原因照實寫家人選的名稱;其餘照讀值與核對結果組句(review_reason)。"""
    label = doc_label(doc)
    if label and DOC_LABELS[label] is None:
        return f"「{label}」請家人對照原件確認。"
    if doc.get("category") == IDENTITY_CATEGORY:
        return "身分證明類文件請家人對照原件確認。"
    return review_reason(doc.get("result"))


def fallback_summary(result: dict[str, Any]) -> str:
    """AI 沒給白話解說時,只用讀到的欄位拼出一句話(不推論、不補知識)。"""
    doc_type = result.get("doc_type") or "其他"
    fields = result.get("fields") or {}
    parts = [f"這是{_MEASURE.get(doc_type, '一份文件')}。"]
    if doc_type == "藥袋":
        names = [it["name"] for it in med_items(result)]
        if names:
            parts.append(f"上面有 {len(names)} 種藥:{'、'.join(names)}。")
        return "".join(parts)
    def label(key: str) -> str:
        return field_spec(doc_type, key).label

    if doc_type == "公文" and fields.get("subject"):
        parts.append(f"{label('fields.subject')}:{_plain(fields['subject'])}。")
    if result.get("vendor"):
        parts.append(f"{label('vendor')}:{result['vendor']}。")
    if _amount_text(result):
        parts.append(f"{label('amount')} {_amount_text(result)}。")
    if fields.get("due_date"):
        parts.append(f"{label('fields.due_date')} {fmt_date(fields['due_date'])}。")
    if fields.get("deadline"):
        # 句子裡說「期限」就好;欄位表的「期限(推算)」是要和公文上的期限原文分開
        parts.append(f"期限 {fmt_date(fields['deadline'])}。")
    elif result.get("date"):
        parts.append(f"{label('date')} {fmt_date(result['date'])}。")
    return "".join(parts)


def doc_label(doc: dict[str, Any]) -> str | None:
    """家人上傳時選的文件名稱(保單、稅單、醫療收據…);只認固定清單上的名稱,資料庫裡的其他值當作沒有。"""
    label = doc.get("doc_label")
    return label if isinstance(label, str) and label in DOC_LABELS else None


def doc_title(doc: dict[str, Any]) -> str:
    """文件標題(清單、家人確認、待複核都用):家人上傳時選了清單上的名稱就用它,否則用讀到的文件類型。"""
    result = doc.get("result") or {}
    return doc_label(doc) or result.get("doc_type") or doc.get("doc_type") or (
        "無法辨識的文件" if doc.get("action") == "failed" else "文件")


def doc_icon(doc: dict[str, Any]) -> str:
    """清單前面的小圖示跟著標題走:家人選了名稱就用它對應的類型(保單這類不能自動判讀的用一般文件圖示),
    否則用讀到的類型。"""
    label = doc_label(doc)
    if label:
        return type_icon(DOC_LABELS[label])
    return type_icon((doc.get("result") or {}).get("doc_type") or doc.get("doc_type"))


def doc_row(doc: dict[str, Any]) -> dict[str, Any]:
    """首頁「最近看過的文件」一列。"""
    result = doc.get("result") or {}
    fields = result.get("fields") or {}
    state_text, state_class, state_icon = _decision_state(doc)
    subtitle = _plain(fields.get("subject") or result.get("vendor") or "")
    return {"id": doc["id"], "title": doc_title(doc), "subtitle": subtitle,
            "when": fmt_when(doc.get("created_at")), "icon": doc_icon(doc),
            "state_text": state_text, "state_class": state_class, "state_icon": state_icon}


def doc_heading(doc: dict[str, Any]) -> str:
    """結果頁標題:帳單種類在固定對照表內才加上(電費 → 電費帳單),其他沿用 doc_title(有家人選的名稱就是它)。"""
    result = doc.get("result") or {}
    kind = str((result.get("fields") or {}).get("bill_kind") or "").strip()
    if result.get("doc_type") == "帳單" and kind in BILL_TITLES:
        return f"{kind}帳單"
    return doc_title(doc)


def _amount_text(result: dict[str, Any]) -> str:
    """讀到的金額(含單位);沒讀到、不是數字或是 0 就是空字串。"""
    amount = result.get("amount")
    if not _is_number(amount) or not amount:
        return ""
    return fmt_amount(amount, result.get("currency"))


def _primary_action(actions: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    """同種類的第一個沒被退回的行動;都被退回就回傳第一個(畫面要說「已退回」)。"""
    same = [a for a in actions if a.get("kind") == kind]
    return next((a for a in same if a.get("state_class") != "off"), same[0] if same else None)


def _fact(label: str, value: str, tone: str = "", wide: bool = False) -> dict[str, Any]:
    """重點格子的一格:tone=due(期限,琥珀色)、amount(金額,大字);wide 佔一整行。"""
    return {"kind": "fact", "label": label, "value": value, "tone": tone, "wide": wide}


def _fact_rows(result: dict[str, Any], doc_type: str) -> list[dict[str, Any]]:
    """沒有要做的事時的摘要:商家/院所(佔一行)、日期、金額(只列有讀到的)。"""
    rows = []
    if result.get("vendor"):
        rows.append(_fact(field_spec(doc_type, "vendor").label, _plain(result["vendor"]), wide=True))
    if result.get("date"):
        rows.append(_fact(field_spec(doc_type, "date").label, fmt_date(result["date"])))
    if _amount_text(result):
        rows.append(_fact(field_spec(doc_type, "amount").label, _amount_text(result), tone="amount"))
    return rows


def _pair_halves(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """半寬的格子兩兩一排;落單的最後一格改成整行,格線才不會留下空洞。"""
    halves = [r for r in rows if r["kind"] == "fact" and not r["wide"]]
    if len(halves) % 2:
        halves[-1]["wide"] = True
    return rows


def answer_view(result: dict[str, Any], actions: list[dict[str, Any]],
                verification: dict[str, Any]) -> dict[str, Any]:
    """結果頁最上面的重點格子:要做什麼 / 什麼時候前 / 多少錢。

    只重排已讀到的欄位與已產生的行動,不推論新內容。rows 的 kind:
    task(事項,佔一整行)、fact(標籤 + 值,見 _fact)、todos(應辦事項清單)、text(長文字,例如公文主旨)。
    tone:todo(還有事要做)、done(沒有要做的事或家人已確認);alert 是核對不符的警示。
    actions 是 action_view 的結果。
    """
    doc_type = result.get("doc_type") or "其他"
    fields = result.get("fields") or {}
    amount = _amount_text(result)
    ans: dict[str, Any] = {
        "tone": "done", "header": "文件摘要", "rows": [], "medication": None,
        "status": None, "footer": "",
        "alert": verification.get("overall_text") if verification.get("overall") == "fail" else "",
    }
    rows: list[dict[str, Any]] = ans["rows"]
    primary = None
    if doc_type == "帳單":
        due = fmt_short_date(fields.get("due_date"))
        kind = str(fields.get("bill_kind") or "").strip()
        if due or amount:
            ans.update(tone="todo", header="要做的事")
            rows.append({"kind": "task", "label": "事項", "value": BILL_TITLES.get(kind, "繳費")})
            if due:
                rows.append(_fact("繳費期限", f"{due}前", tone="due"))
                ans["footer"] = BILL_DEADLINE_NOTE
            if amount:
                rows.append(_fact("應繳金額", amount, tone="amount"))
        else:
            rows.extend(_fact_rows(result, doc_type))
        primary = _primary_action(actions, CALENDAR)
    elif doc_type == "公文":
        deadline = fmt_short_date(fields.get("deadline"))
        todos = [_plain(x) for x in fields.get("required_actions") or [] if not _is_empty(x)]
        subject = _plain(fields.get("subject") or "")
        if deadline or todos or subject:
            ans["tone"], ans["header"] = "todo", "要做的事"
        if deadline:
            rows.append(_fact("辦理期限", f"{deadline}前", tone="due"))
            if fields.get("deadline_text"):
                ans["footer"] = f"依公文上的「{_plain(fields['deadline_text'])}」推算,期限以公文原文為準。"
        if result.get("vendor") and rows:
            rows.append(_fact("發文機關", _plain(result["vendor"])))
        if todos:
            rows.append({"kind": "todos", "label": "應辦事項", "entries": todos[:5]})
        elif subject:
            rows.append({"kind": "text", "label": "主旨", "text": subject})
        if amount:
            rows.append(_fact("金額", amount, tone="amount"))
        if not rows:
            rows.extend(_fact_rows(result, doc_type))
        primary = _primary_action(actions, CALENDAR)
    elif doc_type == "藥袋" or med_items(result):
        primary = _primary_action(actions, MEDICATION_SCHEDULE)
        medication = (primary or {}).get("medication") or medication_view({"items": fields.get("items") or []})
        if medication["slots"] or medication["prn"] or medication["unscheduled"]:
            ans.update(tone="todo", header="服藥時間表", medication=medication,
                       footer="照藥袋上印的字整理,請對照藥袋。")
        else:
            rows.extend(_fact_rows(result, "藥袋"))
    else:
        rows.extend(_fact_rows(result, doc_type))
    _pair_halves(rows)

    if primary:
        # 生效中的期限提醒在到期前都列在首頁(reminder_rows 會略過已過期的),所以只說「到期前」
        listed = primary.get("kind") == CALENDAR and primary["state_class"] == "auto"
        ans["status"] = {"text": primary["state_text"], "icon": primary["state_icon"],
                         "class": primary["state_class"],
                         "sub": "到期前會列在首頁「要記得的事」" if listed else "", "toggle": None}
        if is_auto_reminder(primary):
            # 自動提醒在這裡取消;取消後同一個位置可以恢復(不跳確認框,按錯能馬上救回)
            cancelled = primary.get("status") == "rejected"
            if cancelled:
                ans["status"].update(text="已取消提醒", sub="首頁「要記得的事」不會再列出")
            ans["status"]["toggle"] = {
                "id": primary["id"],
                "decision": REMINDER_RESTORE if cancelled else REMINDER_CANCEL,
                "label": "恢復提醒" if cancelled else "取消提醒",
                "icon": "undo" if cancelled else "x",
            }
        if primary["state_class"] == "wait":
            ans["tone"] = "todo"
        elif primary.get("kind") == MEDICATION_SCHEDULE and primary.get("status") == "done":
            ans["tone"] = "done"
    return ans


def trust_notes(provider: str, local_only: tuple[str, ...] | list[str]) -> list[tuple[str, str]]:
    """每頁固定的安心說明(圖示, 文字)。隱私那一句依實際設定寫,不承諾系統做不到的事。"""
    sensitive = "、".join(local_only) or "敏感文件"
    if provider == "workers_ai":
        # 敏感大類(src.models.SENSITIVE_CATEGORIES)不論類型都在本機,和首頁上傳區的說明同一句話
        privacy = f"醫療與保險、身分證明兩類,以及{sensitive}和沒選類型的文件只在這台電腦上辨識;其他文件會交給雲端模型讀取。"
    elif provider == "mock":
        privacy = "展示模式:使用模擬讀值,文件不會送出這台電腦。"
    else:
        privacy = "文件都在這台電腦上辨識,不會送到雲端。"
    return [
        ("lock", f"{privacy}照片與結果存在這台電腦。"),
        ("shield", "只協助閱讀:只提醒,不會替您付款、回覆或做醫療判斷。"),
        ("check", "核對結果由程式依規則檢查,不是 AI 自己打分數。"),
    ]


def reminder_rows(docs: list[dict[str, Any]], actions: list[dict[str, Any]], today: date,
                  limit: int = 6) -> list[dict[str, Any]]:
    """首頁「要記得的事」:家人待確認 → 期限未過的期限提醒(近的在前)→ 核對不符 → 已確認的服藥表。

    docs 是 Store.list_documents 的結果,actions 是 Store.list_actions 的原始列;
    只拿已有的欄位排版,文件文字一律當資料顯示。有日期的列帶 month/day 給日曆小方塊。
    """
    by_id = {d["id"]: d for d in docs}
    waiting, upcoming, alerts, done = [], [], [], []
    for raw in actions:
        # 被家人更正取代的舊行動不列(和結果頁同一條規則),不管它現在的狀態:只看狀態的話,舊期限可能和新的一起冒出來
        if raw.get("superseded"):
            continue
        v = action_view(raw)
        if v["state_class"] == "off":
            continue
        doc = by_id.get(v["document_id"]) or {}
        result = doc.get("result") or {}
        icon_name = type_icon(result.get("doc_type") or ("藥袋" if v["kind"] == MEDICATION_SCHEDULE else ""))
        vendor = _plain(result.get("vendor") or (raw.get("payload") or {}).get("hospital") or "")
        row = {"href": f"/doc/{v['document_id']}", "title": v["title"], "sub": vendor, "amount": "",
               "month": "", "day": "", "icon": icon_name, "alert": False, "state": v["state_text"],
               "state_icon": v["state_icon"], "state_class": v["state_class"]}
        if v["kind"] == CALENDAR:
            day = (raw.get("payload") or {}).get("date")
            try:
                due = date.fromisoformat(str(day))
            except ValueError:
                continue
            if due < today:
                continue
            parts = date_parts(str(day)) or ("", "")
            row.update(month=parts[0], day=parts[1], sort=due.isoformat(),
                       sub=",".join(x for x in (vendor, f"{parts[0]}{parts[1]}日前") if x),
                       amount=_amount_text(result) if result.get("doc_type") == "帳單" else "")
        elif v["kind"] == MEDICATION_SCHEDULE and v["status"] == "done":
            m = v["medication"] or {}
            slots = [s for s, _ in m.get("slots", [])] + (["需要時"] if m.get("prn") else [])
            row["sub"] = ",".join(x for x in (vendor, "、".join(slots)) if x)
            done.append(row)
            continue
        if v["state_class"] == "wait":
            row["href"] = "/confirm"
            waiting.append(row)
        elif v["kind"] == CALENDAR:
            upcoming.append(row)
    for doc in docs:
        vv = verification_view(doc.get("result") or {})
        if vv["overall"] == "fail":
            alerts.append({"href": f"/doc/{doc['id']}", "title": doc_title(doc),
                           "sub": _plain((doc.get("result") or {}).get("vendor") or ""),
                           "amount": _amount_text(doc.get("result") or {}),
                           "month": "", "day": "", "icon": "alert", "alert": True,
                           # 清單欄位窄,只放短狀態;完整說明在結果頁
                           "state": "核對不符" if vv["conflict"] else "檢查沒通過",
                           "state_icon": "alert", "state_class": "manual"})
    upcoming.sort(key=lambda n: n["sort"])
    return (waiting + upcoming + alerts + done)[:limit]


def _summary_note(summary_is_ai: bool, corrected: bool) -> str:
    """白話解說下面那句說明:寫清楚這段是誰整理的(原則 7)。家人更正後不留 AI 的舊解說,改由系統依欄位整理。"""
    if summary_is_ai:
        return "這段是 AI 讀完整理的,重要的日期與金額請對照原件。"
    if corrected:
        return "家人更正過讀值,這段是系統依更正後的欄位整理的。"
    return "AI 沒有提供解說,這段是系統依讀到的欄位整理的。"


def doc_view(doc: dict[str, Any], *, file_url: str | None, hint: str | None = None,
             actions: list[dict[str, Any]] | None = None,
             file_size: tuple[int, int] | None = None,
             correct_url: str | None = None, corrected: bool = False) -> dict[str, Any]:
    """結果頁的畫面資料。hint 是上傳時使用者選的類型(只用來決定是否顯示藥袋聲明);
    actions 是這份文件的 action_view 清單,用來組摘要上的提醒/家人確認狀態。
    correct_url:「更正讀值」的連結(不能更正的文件是 None);corrected:家人更正過這份文件的讀值。"""
    result = doc.get("result") or {}
    state_text, state_class, state_icon = _decision_state(doc)
    summary = (result.get("plain_summary") or "").strip()
    items = med_items(result)
    verification = verification_view(result)
    category = doc_category(doc)
    # 不放 doc 的原始 reason:那是「驗證信心 0.10 低於門檻 0.80」這類工程用語,放進畫面資料容易被誤用;
    # 轉人工的原因一律用 review_reason 的白話句子
    return {
        "title": doc_title(doc),
        "heading": doc_heading(doc),
        "category": category,                       # 麵包屑「首頁 / 醫療與保險 / 藥袋」,連到文件櫃的這一類
        "category_href": cabinet_href(category),
        "answer": answer_view(result, actions or [], verification) if result else None,
        "when": fmt_when(doc.get("created_at")),
        "review_reason": doc_review_reason(doc) if doc.get("action") == "review" else "",
        "failed": doc.get("action") == "failed" or not result,
        "state_text": state_text, "state_class": state_class, "state_icon": state_icon,
        "summary": summary or (fallback_summary(result) if result else ""),
        "summary_is_ai": bool(summary),
        "summary_note": _summary_note(bool(summary), corrected),
        "correct_url": correct_url,
        "fields": field_rows(result) if result else [],
        "med_items": items,
        "is_medication": result.get("doc_type") == "藥袋" or hint == "藥袋" or bool(items),
        "verification": verification,
        "file_url": file_url,
        "file_size": file_size,   # (寬, 高):<img> 先佔位,延遲載入時版面不跳
        "is_pdf": is_pdf(doc.get("target_path")),
        "source_model": result.get("source_model") or "",
    }


def review_row(doc: dict[str, Any]) -> dict[str, Any]:
    """待複核清單的一列(讀 SQLite,每份連到更正頁):原因用白話;可信度只列依核對算的驗證信心(原則 1)。"""
    result = doc.get("result") or {}
    vc = result.get("verified_confidence")
    return {
        "href": f"/doc/{doc['id']}/correct",
        "title": doc_title(doc),
        "icon": doc_icon(doc),
        "reason": doc_review_reason(doc) if result else (doc.get("reason") or "(沒有說明)"),
        "when": fmt_when(doc.get("created_at")),
        "confidence": vc if _is_number(vc) else None,
    }


# ---- 文件四大類:上傳先選大類、文件櫃、家人指定類別 ---------------------------------------
# 大類只用來整理、瀏覽與隱私分流;擷取、核對與行動仍只看文件類型。大類的值一律來自封閉列舉
# (src.models.CATEGORIES 與「未分類」),表單送來的其他值當作沒選。

# 大類的圖示(上傳第一列、文件櫃卡片、更正頁的類別選項)
_ICON_PATHS.update({
    "id": '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="11" r="2.2"/>'
          '<path d="M5.8 16a3.4 3.4 0 0 1 6.4 0"/><path d="M14 10h4"/><path d="M14 13.5h3"/>',
    "asset": '<path d="M3.5 10 12 4l8.5 6"/><path d="M5.5 9v10h13V9"/><path d="M10 19v-5h4v5"/>',
    "health": '<path d="M20.4 5.6a5 5 0 0 0-7.1 0L12 6.9l-1.3-1.3a5 5 0 1 0-7.1 7.1L12 21l8.4-8.3a5 5 0 0 0 0-7.1z"/>'
              '<path d="M8 12h2.5l1-2 2 4 1-2H16"/>',
    "contract": '<path d="M6 2.5h8l4.5 4.5v14.5h-12.5z"/><path d="M14 2.5V7h4.5"/><path d="M9 12h6"/>'
                '<path d="M9 15.5h3.5"/><path d="M14.5 18.5l1.5 1 2.5-3"/>',
})
CATEGORY_ICONS: dict[str, str] = {"身分證明": "id", "財產資產": "asset", "醫療與保險": "health",
                                  "生活契約": "contract", UNCATEGORIZED: "file"}
ASSIGNABLE_CATEGORIES: tuple[str, ...] = (*CATEGORIES, UNCATEGORIZED)   # 家人在更正頁能指定的類別
# 文件櫃卡片上的例子:說明這一類放什麼,不代表都能自動判讀(身分證明類交給家人確認)
CATEGORY_EXAMPLES: dict[str, str] = {"身分證明": "身分證、健保卡、戶口名簿", "財產資產": "發票、收據、稅單",
                                     "醫療與保險": "藥袋、保單、醫療收據", "生活契約": "水電瓦斯、電信、租約"}
KINDS_LEGEND = "是哪一種文件?"   # 上傳第二列沒選大類(或沒有 JS)時的標題


def id_note(cloud: bool, examples: bool = False) -> str:
    """身分證明類的說明(文件櫃;上傳區不放任何說明,使用者 10/3):這一類交給家人確認。

    「只在這台電腦處理」只在雲端模式寫:本機模式所有文件都在這台電腦,特別寫出來反而像別的文件會送出去
    (同首頁藥袋提示的規則,10/2 決定)。examples:名稱後面加上例子(文件櫃用)。
    """
    name = f"身分證明類({CATEGORY_EXAMPLES['身分證明']})" if examples else "身分證明類"
    return f"{name}拍了會交給家人複核{',而且只在這台電腦處理' if cloud else ''}。"


def kind_choices() -> list[dict[str, Any]]:
    """上傳第二列的選項:五種能自動判讀的類型 + 四大類清單上的常見文件(CATEGORY_DOCS),「不確定」放最後(照樣稿)。

    沒有 JS 時只看得到五種類型,各類的常見文件先藏著(hidden);app.js 依第一列只留那一類的名稱。
    合成一串時,幾個大類都有的名稱(公文)排在後面,依大類篩出來的順序才會和清單一樣。
    """
    named = [name for docs in CATEGORY_DOCS.values() for name, _ in docs]
    shared = {name for name in named if named.count(name) > 1}
    values = sorted(dict.fromkeys((*TYPE_VALUES, *named)), key=lambda v: v in shared)
    return [{"value": v, "label": v, "unsure": False, "hidden": v not in TYPE_VALUES} for v in values] + [
        {"value": UNSURE, "label": UNSURE, "unsure": True, "hidden": False}]


def category_choices() -> list[dict[str, Any]]:
    """上傳第一列「這是哪一類?」:不確定 + 四大類。

    kinds 是選了這一類後第二列留下的選項(這一類清單上的名稱 + 「不確定」,空白分隔;app.js 照這個篩),
    legend 是第二列的標題;沒選大類(不確定)留下五種類型。伺服器各欄只收白名單值(kind_choice)。
    """
    shown = " ".join(c["value"] for c in kind_choices() if not c["hidden"])   # 五種類型 + 不確定,照畫面順序
    rows = [{"value": UNSURE, "icon": TYPE_ICONS[UNSURE], "legend": KINDS_LEGEND, "kinds": shown}]
    for category in CATEGORIES:
        names = [name for name, _ in CATEGORY_DOCS[category]]
        rows.append({"value": category, "icon": CATEGORY_ICONS[category], "legend": f"{category}裡的哪一種?",
                     "kinds": " ".join((*names, UNSURE))})
    return rows


def kind_choice(category: str | None, value: Any) -> str | None:
    """上傳第二列送來的值 → 白名單上的名稱;不在白名單(含「不確定」)回 None,當作沒選。

    選了大類(四大類之一)只收那一類清單上的名稱(稅單、保單…),沒選大類只收五種類型;
    回傳的是清單裡的字串,不是表單原文。沒有 JS 時第二列是五種類型,和大類對不上的(例如生活契約 + 帳單)也當作沒選。
    """
    if category in CATEGORIES:
        entry = catalog_entry(category, value)
        return entry[0] if entry else None
    return next((v for v in TYPE_VALUES if v == value), None)


def doc_category(doc: dict[str, Any]) -> str:
    """文件現在歸哪一類;舊資料沒有類別(None)或值不認得,一律當「未分類」。"""
    category = doc.get("category")
    return category if category in CATEGORIES else UNCATEGORIZED


def cabinet_href(category: str) -> str:
    """文件櫃只看某一類的網址(結果頁麵包屑、文件櫃卡片)。"""
    return f"/cabinet?cat={quote(category)}"


def _cabinet_empty(category: str) -> str:
    if category == UNCATEGORIZED:
        return "沒有未分類的文件。"
    return f"這一類還沒有文件。上傳時選「{category}」,文件就會放在這裡。"


def cabinet_view(docs: list[dict[str, Any]], selected: str | None = None, *, cloud: bool = False) -> dict[str, Any]:
    """文件櫃的畫面資料。docs 是 Store.list_documents(limit=None)(新的在前);selected 是網址的 ?cat=。

    cards:四大類卡片(圖示、名稱、例子、份數;選中的 current)。sections:選了一類(四大類或未分類)只列那一類;
    沒選或不認得的值,依大類分組列出全部,「未分類」組放最後(舊資料沒有類別也算),空的組不列。
    每一列沿用首頁「最近看過的文件」(doc_row)。身分證明還沒有文件時附上說明(id_note)。
    """
    groups: dict[str, list[dict[str, Any]]] = {name: [] for name in (*CATEGORIES, UNCATEGORIZED)}
    for doc in docs:
        groups[doc_category(doc)].append(doc)
    selected = selected if selected in groups else None
    cards = [{"name": name, "icon": CATEGORY_ICONS[name], "examples": CATEGORY_EXAMPLES[name],
              "count": len(groups[name]), "href": cabinet_href(name), "current": name == selected}
             for name in CATEGORIES]
    names = [selected] if selected else [name for name, rows in groups.items() if rows]
    sections = [{"name": name, "count": len(groups[name]), "rows": [doc_row(d) for d in groups[name]],
                 "empty": _cabinet_empty(name)} for name in names]
    return {"cards": cards, "sections": sections,
            "id_note": "" if groups["身分證明"] else id_note(cloud, examples=True)}


# ---- 更正讀值(F7) ---------------------------------------------------------------
# 欄位依類型(順序照樣稿);標籤、種類、必填沿用 web/fields.py,這裡只補表單才需要的:輸入方式、提示、
# 表單上不同的標籤。欄位名(name)就是 FieldSpec 的鍵("amount"、"fields.due_date");藥品每一種是
# items-<序號>-<name|usage|timing|days>。表單值和模型讀值一樣只是資料,只讀這裡列的欄位(原則 3)。

BILL_KINDS: tuple[str, ...] = ("電費", "水費", "瓦斯", "電信", "其他")   # 同提示詞的帳單種類(封閉選項)
PRN_CHOICE = "需要時"
MED_TIMING_CHOICES: tuple[str, ...] = (*TIMINGS, PRN_CHOICE)   # 時段可多選;「需要時」存成 prn
MAX_FORM_TEXT = 500      # 一欄最多幾個字:只是儲存的上限,內容合不合理交給核對
MAX_MED_ITEMS = 30       # 同 src/actions/medication.py:一張藥袋不會有這麼多種藥

_FORM_KEYS: dict[str, tuple[str, ...]] = {
    "帳單": ("fields.bill_kind", "fields.due_date", "amount", "vendor", "date"),
    "公文": ("vendor", "date", "fields.subject", "fields.deadline_text", "fields.doc_number",
             "fields.required_actions"),
    "藥袋": ("vendor", "date", "fields.items"),
    "發票": ("date", "vendor", "amount", "currency", "invoice_number"),
    "收據": ("date", "vendor", "amount", "currency", "invoice_number"),
}
_FORM_DEFAULT_KEYS = ("date", "vendor", "amount", "currency", "invoice_number")   # 不支援的類型:共通欄位
# 表單上和結果頁不同的標籤:帳單金額寫明單位;幣別在 web/fields.py 沒有中文名
_FORM_LABELS = {("帳單", "amount"): "應繳金額(元)", ("", "currency"): "幣別"}
_FORM_HINTS = {
    "amount": "只填數字",
    "currency": "台幣填 NTD",
    "fields.deadline_text": "照公文上的寫法,例如「收到本函後15日內」;日期由系統計算",
    "fields.required_actions": "一行一項",
}
# 輸入方式:choice 選項膠囊、lines 多行(一行一項)、currency 幣別代碼;其餘照 FieldSpec.kind(date/amount/text)
_FORM_KINDS = {"fields.bill_kind": "choice", "fields.required_actions": "lines", "currency": "currency"}
_NO_SPELLCHECK = {"invoice_number", "currency", "fields.doc_number"}   # 代碼類欄位不做拼字檢查
# 存檔後會重新計算什麼(頁首說明只寫系統真的會做的事,原則 7)
_RECOMPUTED = {"帳單": "並重新計算提醒", "公文": "並重新計算提醒", "藥袋": "並重新整理服藥時間表"}

MSG_REQUIRED = "這一欄必填,請對照原件填上。"
MSG_DATE = "請填正確的日期,例如 2026-10-20。"
MSG_AMOUNT = "只填數字,例如 1286。"
MSG_AMOUNT_POSITIVE = "金額要大於 0。"
MSG_DAYS = "只填天數的數字,例如 7。"
MSG_CURRENCY = "請填 3 個英文字母,台幣填 NTD。"
MSG_TOO_LONG = f"太長了,最多 {MAX_FORM_TEXT} 個字。"

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_ITEM_INPUT = re.compile(r"items-(\d{1,3})-(?:name|usage|timing|days)")


def _form_keys(doc_type: str) -> tuple[str, ...]:
    return _FORM_KEYS.get(doc_type, _FORM_DEFAULT_KEYS)


def _form_kind(doc_type: str, key: str) -> str:
    return _FORM_KINDS.get(key) or field_spec(doc_type, key).kind


def _form_label(doc_type: str, key: str) -> str:
    return _FORM_LABELS.get((doc_type, key)) or _FORM_LABELS.get(("", key)) or field_spec(doc_type, key).label


def _form_text(value: Any) -> str:
    """讀值 → 表單上的字:金額去掉 .0(1286.0 → 1286,長輩不會以為要填小數)、清單一行一項。"""
    if _is_empty(value) or isinstance(value, bool):
        return ""
    if _is_number(value):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, (list, tuple)):
        return "\n".join(_plain(v) for v in value if not _is_empty(v))
    return _plain(value)


def _usage_text(raw: dict[str, Any]) -> str:
    """一種藥的「用法」:劑量與用法原文接成一行,和結果頁「用法:」顯示的一樣。"""
    return ",".join(_plain(raw.get(k)) for k in ("dose_text", "frequency_text") if not _is_empty(raw.get(k)))


def _is_prn(value: Any) -> bool:
    """「需要時」服用;判斷同 src/actions/medication.py(字串 "false" 不算)。"""
    return value is True or str(value).strip().lower() in ("true", "1", "是", "yes")


def _blank_item() -> dict[str, Any]:
    return {"name": "", "usage": "", "timing": [], "days": ""}


def _item_values(raw: Any) -> dict[str, Any]:
    """一種藥的表單值。沒讀到藥名的也列出來(空白),家人才能對照藥袋補上。"""
    if not isinstance(raw, dict):
        return {**_blank_item(), "name": _form_text(raw)}
    timing = raw.get("timing") or []
    if isinstance(timing, str):
        timing = [t for t in re.split(r"[、,,/\s]+", timing) if t]
    slots = {_timing_label(t) for t in timing if not _is_empty(t)} if isinstance(timing, list) else set()
    days = raw.get("days")
    return {
        "name": _form_text(raw.get("name") or raw.get("drug")),
        "usage": _usage_text(raw),
        "timing": [t for t in TIMINGS if t in slots] + ([PRN_CHOICE] if _is_prn(raw.get("prn")) else []),
        "days": str(int(days)) if _is_number(days) and days > 0 else "",
    }


def correction_values(result: dict[str, Any]) -> dict[str, Any]:
    """更正表單的初始值(都是字串,和表單送回來的一樣):從讀值取,日期 YYYY-MM-DD、清單一行一項。"""
    doc_type = result.get("doc_type") or "其他"
    values: dict[str, Any] = {}
    for key in _form_keys(doc_type):
        value = _field_value(result, key)
        if key == "fields.items":
            items = value if isinstance(value, list) else []
            values["items"] = [_item_values(it) for it in items[:MAX_MED_ITEMS]] or [_blank_item()]
        elif key == "currency":
            values[key] = _form_text(value) or "NTD"
        else:
            values[key] = _form_text(value)
    return values


def _form_input(form: Any, name: str, multiline: bool = False) -> str:
    """表單送來的一欄:只收字串,去掉控制字元(多行欄位保留換行)與頭尾空白。"""
    value = form.get(name)
    if not isinstance(value, str):
        return ""
    keep = "\n" if multiline else ""
    return "".join(ch for ch in value if unicodedata.category(ch) != "Cc" or ch in keep).strip()


def _parse_amount(text: str) -> tuple[float | None, str]:
    plain = unicodedata.normalize("NFKC", text).replace(",", "").replace(" ", "")   # 1,286、全形數字都收
    if not _NUMBER.fullmatch(plain):
        return None, MSG_AMOUNT
    value = float(plain)
    return (value, "") if value > 0 else (None, MSG_AMOUNT_POSITIVE)


def _parse_value(kind: str, text: str) -> tuple[Any, str]:
    """一欄非空白的輸入 → (轉好型別的值, 錯誤說明)。日期也收民國年寫法,由程式換算(原則 2)。"""
    if len(text) > MAX_FORM_TEXT:
        return None, MSG_TOO_LONG
    if kind == "date":
        parsed = parse_date(text)
        return (parsed.isoformat(), "") if parsed else (None, MSG_DATE)
    if kind == "amount":
        return _parse_amount(text)
    if kind == "currency":
        code = clean_currency(text)
        return (code, "") if re.fullmatch(r"[A-Z]{3}", code) else (None, MSG_CURRENCY)
    if kind == "lines":
        return [line.strip() for line in text.splitlines() if line.strip()], ""
    return text, ""


def _parse_items(form: Any, originals: list[Any], required: bool,
                 errors: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """藥品 → (藥品清單, 表單值)。全部空白的那一種當作沒有(多加了沒填);錯誤寫進 errors。

    用法沒改就保留原本分開的劑量與用法原文;改了就整行存成用法原文(家人照藥袋抄的一行字)。
    """
    indexes = sorted({int(m.group(1)) for name in form.keys() if (m := _ITEM_INPUT.fullmatch(name))})
    values: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for i in indexes[:MAX_MED_ITEMS]:
        prefix = f"items-{i}-"
        chosen = form.getlist(prefix + "timing")
        entry = {"name": _form_input(form, prefix + "name"), "usage": _form_input(form, prefix + "usage"),
                 "timing": [t for t in MED_TIMING_CHOICES if t in chosen], "days": _form_input(form, prefix + "days")}
        at = f"items-{len(values)}-"   # 重畫表單時的序號(錯誤也標在這裡)
        values.append(entry)
        if not any(entry.values()):
            continue
        days = unicodedata.normalize("NFKC", entry["days"])
        if not entry["name"]:
            errors[at + "name"] = MSG_REQUIRED
        if days and not (days.isdigit() and int(days) <= 365):
            errors[at + "days"] = MSG_DAYS
        for name in ("name", "usage"):
            if len(entry[name]) > MAX_FORM_TEXT:
                errors[at + name] = MSG_TOO_LONG
        original = originals[i] if i < len(originals) and isinstance(originals[i], dict) else {}
        same_usage = entry["usage"] == _usage_text(original)
        items.append({
            "name": entry["name"],
            "dose_text": _form_text(original.get("dose_text")) if same_usage else "",
            "frequency_text": _form_text(original.get("frequency_text")) if same_usage else entry["usage"],
            "timing": [t for t in entry["timing"] if t in TIMINGS],
            "prn": PRN_CHOICE in entry["timing"],
            "days": int(days) if days.isdigit() and int(days) <= 365 else 0,
        })
    if not values:
        values.append(_blank_item())
    if required and not items:
        errors["items-0-name"] = MSG_REQUIRED   # 一種藥都沒有:第一種的藥名必填
    return items, values


def parse_correction(result: dict[str, Any], form: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """把更正表單轉成 (changes, values, errors)。

    changes:給 src.review.correct_document 的值(已轉好型別,空白 = 清掉這一欄);values:原樣保留的輸入,
    驗證失敗時重畫表單用,已填的不必重填;errors:欄位名 → 中文說明(必填、日期格式、金額只填數字…)。
    result 是原讀值(藥品的用法沒改時保留原本的劑量與用法原文)。只讀這個類型的表單欄位,
    其他鍵(例如有人塞進來的 tier、kind、doc_type)一律不看。form 要有 get、getlist、keys(Starlette 的 FormData)。
    """
    doc_type = result.get("doc_type") or "其他"
    changes: dict[str, Any] = {}
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key in _form_keys(doc_type):
        spec = field_spec(doc_type, key)
        if key == "fields.items":
            originals = _field_value(result, key)
            changes[key], values["items"] = _parse_items(
                form, originals if isinstance(originals, list) else [], spec.required, errors)
            continue
        kind = _form_kind(doc_type, key)
        text = _form_input(form, key, multiline=kind == "lines")
        values[key] = text
        if kind == "choice":
            if text in BILL_KINDS:   # 沒選、或不在選項內:維持原讀值
                changes[key] = text
        elif not text:
            if spec.required:
                errors[key] = MSG_REQUIRED
            else:
                changes[key] = "NTD" if kind == "currency" else None
        else:
            value, error = _parse_value(kind, text)
            if error:
                errors[key] = error
            else:
                changes[key] = value
    return changes, values, errors


def _input_view(name: str, input_id: str, label: str, value: Any, *, kind: str = "text", required: bool = False,
                hint: str = "", error: str = "", spellcheck: bool = True) -> dict[str, Any]:
    """表單的一個輸入欄(樣板 correct.html 的 field 巨集用)。"""
    return {"name": name, "id": input_id, "label": label, "value": value, "kind": kind, "required": required,
            "hint": hint, "error": error, "spellcheck": spellcheck and kind not in ("date", "amount")}


def _item_view(j: int, entry: dict[str, Any], errors: dict[str, str]) -> dict[str, Any]:
    prefix, base = f"items-{j}-", f"m{j}-"
    return {
        "legend": f"第 {j + 1} 種藥",
        "name": _input_view(prefix + "name", base + "name", "藥名", entry.get("name", ""), required=True,
                            error=errors.get(prefix + "name", "")),
        "usage": _input_view(prefix + "usage", base + "usage", "用法(照藥袋上的字)", entry.get("usage", ""),
                             error=errors.get(prefix + "usage", "")),
        "timing_name": prefix + "timing",
        "timing": entry.get("timing") or [],
        "days": _input_view(prefix + "days", base + "days", "天數", entry.get("days", ""), kind="number",
                            error=errors.get(prefix + "days", ""), spellcheck=False),
    }


def correct_view(doc: dict[str, Any], values: dict[str, Any] | None = None,
                 errors: dict[str, str] | None = None, *, file_url: str | None,
                 file_size: tuple[int, int] | None = None, add_item: bool = False) -> dict[str, Any]:
    """更正頁的畫面資料。values 是表單值(沒給就從讀值取);errors 是欄位 → 說明;
    add_item:剛按了「再加一種藥」,藥品多一種空白的。

    驗證失敗時第一個要改的欄位、或新加的那一種藥的藥名,自動取得焦點(focus 是輸入欄的 id)。
    「這份文件屬於哪一類」:四大類 + 未分類,預設目前的類別;values["category"] 是家人剛選的(重畫表單時保留,
    不在選項內就回到目前的類別)。
    """
    result = doc.get("result") or {}
    doc_type = result.get("doc_type") or "其他"
    values = values if values is not None else correction_values(result)
    errors = errors or {}
    chosen = values.get("category")
    category = chosen if chosen in ASSIGNABLE_CATEGORIES else doc_category(doc)
    fields: list[dict[str, Any]] = []
    items: list[dict[str, Any]] | None = None
    for key in _form_keys(doc_type):
        if key == "fields.items":
            entries = list(values.get("items") or [_blank_item()])
            if add_item and len(entries) < MAX_MED_ITEMS:
                entries.append(_blank_item())
            items = [_item_view(j, entry, errors) for j, entry in enumerate(entries)]
            continue
        kind = _form_kind(doc_type, key)
        fields.append({
            **_input_view(key, "f-" + key.replace(".", "-"), _form_label(doc_type, key), values.get(key, ""),
                          kind=kind, required=field_spec(doc_type, key).required, hint=_FORM_HINTS.get(key, ""),
                          error=errors.get(key, ""), spellcheck=key not in _NO_SPELLCHECK),
            "options": BILL_KINDS if kind == "choice" else (),
        })
    inputs = fields + [f for it in items or [] for f in (it["name"], it["usage"], it["days"])]
    focus = next((f["id"] for f in inputs if f["error"]), None)
    if focus is None and add_item and items:
        focus = items[-1]["name"]["id"]
    review = doc.get("action") == "review"
    recomputed = _RECOMPUTED.get(doc_type)
    return {
        "id": doc["id"],
        "title": doc_title(doc),
        "heading": doc_heading(doc),
        "lede": f"對照原件,只改讀錯的地方。存檔後會重新核對{',' + recomputed if recomputed else ''}。",
        "review_reason": doc_review_reason(doc) if review else "",   # 和待複核清單、結果頁同一句
        "after_note": f"存檔後程式會再檢查一次;還有問題的話,文件會{'留在' if review else '改放到'}「待複核」並說明原因。",
        "fields": fields,
        "meds": items,   # 藥品(只有藥袋有;鍵不叫 items,Jinja 的 v.items 會先抓到 dict 的方法)
        "category": category,
        "category_choices": [{"value": c, "icon": CATEGORY_ICONS[c]} for c in ASSIGNABLE_CATEGORIES],
        "timing_choices": MED_TIMING_CHOICES,
        "can_add_item": items is not None and len(items) < MAX_MED_ITEMS,
        "error_count": len(errors),
        "focus": focus,
        "can_reject": review,
        "reject_ask": REJECT_DOC_ASK,   # 退回前先問的話(確認框與沒有 JS 時的確認頁同一份)
        "file_url": file_url,
        "file_size": file_size,
        "is_pdf": is_pdf(doc.get("target_path")),
    }


# ---- 設定頁(SET) -------------------------------------------------------------------
# 四塊:系統狀態(唯讀)、這台裝置的偏好(存在瀏覽器,app.js 讀寫)、會影響全家的系統設定、資料管理。
# 規則(門檻範圍、雲端要金鑰、藥袋一律本機)在 src/settings.py;這裡只把目前的設定排成畫面文字。

PROVIDER_LABELS = {"ollama": "只用這台電腦", "workers_ai": "可以用雲端備援"}   # 選項與變更紀錄共用
_MODE_TEXT = {"ollama": "本機(這台電腦)", "workers_ai": "雲端備援(敏感文件仍在這台電腦)",
              "mock": "展示模式(使用模擬讀值)"}
_MODEL_TEXT = {DEFAULT_OLLAMA_MODEL: "Gemma 4 12B", DEFAULT_WORKERS_AI_MODEL: "Gemma 4 26B"}   # 其他照設定原樣
# 這台裝置的偏好:(name, 標題, ((值, 文字)…), 預設值)。值要和 app.js 的 PREFS、app.css 的 html[data-font] 一致
DEVICE_PREFS: tuple[tuple[str, str, tuple[tuple[str, str], ...], str], ...] = (
    ("font", "字級", (("standard", "標準"), ("large", "大"), ("xlarge", "特大")), "standard"),
    ("rate", "朗讀速度", (("slow", "慢"), ("standard", "標準")), "standard"),
)
CLOUD_CONFIRM_TITLE, CLOUD_CONFIRM_OK = "確定要開啟雲端備援嗎?", "確定開啟"
PURGE_CONFIRM_TITLE, PURGE_CONFIRM_OK = "確定要刪除全部資料嗎?", "確定刪除"
PURGE_CONFIRM = "所有文件的照片、讀值、提醒與更正紀錄都會刪除,無法復原;設定與設定變更紀錄會保留。"

# ---- 要先確認的動作 ----
# 切到雲端備援、刪除全部資料、兩處「退回」,按下前先問一次:標題、說明、確定鍵,加上確認欄位的名稱。
# 有 JS 時用 base.html 的確認框,按了「確定…」app.js 才把確認欄位(值是 "1")補進表單;伺服器沒收到這個欄位
# 就不做(沒有 JS、或 app.js 沒跑起來),改回一頁確認頁(ask.html)問同樣的字
CONFIRM_CLOUD_FIELD, CONFIRM_PURGE_FIELD, CONFIRM_REJECT_FIELD = "confirm_cloud", "understood", "confirm_reject"
PURGE_ASK = {"title": PURGE_CONFIRM_TITLE, "message": PURGE_CONFIRM, "ok": PURGE_CONFIRM_OK,
             "field": CONFIRM_PURGE_FIELD}
REJECT_DOC_ASK = {"title": "確定要退回這份文件嗎?", "message": "這份文件會改成「讀不出來」,請長輩重新拍一張。",
                  "ok": "確定退回", "field": CONFIRM_REJECT_FIELD}


def reject_action_ask(kind_label: str) -> dict[str, str]:
    """家人確認頁退回一個事項前要問的話(kind_label:服藥時間表、期限提醒…)。"""
    return {"title": f"確定要退回「{kind_label}」嗎?", "message": "退回後就不會生效。", "ok": "確定退回",
            "field": CONFIRM_REJECT_FIELD}


def local_only_scope(local_only: tuple[str, ...] | list[str]) -> list[str]:
    """一律只在本機處理的文件類型與大類(畫面文字用):本機限定的類型(藥袋…)在前,再接敏感大類,
    含這些類型的大類排前面(藥袋 → 醫療與保險 → 身分證明)。"""
    types = list(dict.fromkeys(local_only))
    categories = sorted(SENSITIVE_CATEGORIES,
                        key=lambda c: (not set(CATEGORY_TYPES.get(c, ())) & set(types), CATEGORIES.index(c)))
    return types + categories


def cloud_ask(local_only: tuple[str, ...] | list[str]) -> dict[str, str]:
    """切到雲端備援前要問的話;哪些文件仍只在這台電腦處理,依目前的設定寫。"""
    scope = "、".join(local_only_scope(local_only))
    return {"title": CLOUD_CONFIRM_TITLE, "ok": CLOUD_CONFIRM_OK, "field": CONFIRM_CLOUD_FIELD,
            "message": f"開啟後,{scope},以及沒選類型的文件仍只在這台電腦處理;其他文件會交給雲端模型讀取。"}


def _model_pill(provider: str, ollama: str) -> dict[str, str] | None:
    """辨識模式旁的狀態膠囊:本機模型(Ollama)連不連得上;展示模式不連模型,不放。"""
    where = "" if provider == "ollama" else "本機"
    if ollama == "reachable":
        return {"text": f"{where}模型連得上", "class": "auto", "icon": "check"}
    if ollama == "unreachable":
        return {"text": f"{where}模型連不上", "class": "manual", "icon": "alert"}
    return None


def _models_text(models: dict[str, str]) -> str:
    """使用的模型(雲端在前);展示模式沒有模型。"""
    names = [f"{_MODEL_TEXT.get(models[key], models[key])}({where})"
             for key, where in (("cloud", "雲端"), ("local", "本機")) if models.get(key)]
    return "、".join(names) or "不使用模型(展示模式用模擬讀值)"


def setting_change_text(change: dict[str, Any]) -> str:
    """一筆設定變更的白話(「辨識模式改成『只用這台電腦』」);值一律當資料顯示。"""
    key, value = change.get("key"), str(change.get("new_value") or "")
    if key == PROVIDER:
        return f"辨識模式改成「{PROVIDER_LABELS.get(value, value)}」"
    if key == THRESHOLD:
        try:
            return f"自動存檔門檻改成 {fmt_pct(float(value))}"
        except ValueError:
            return f"自動存檔門檻改成 {value}"
    if key == PURGED:
        return "刪除全部資料"
    return f"{key} 改成 {value}"


def _threshold_options(current: float, default: float, chosen: Any) -> list[dict[str, Any]]:
    """門檻的選項(80%、85%、90%、95%;目前的值不在裡面也列出);config.yaml 的值標「預設」。"""
    values = set(THRESHOLD_CHOICES)
    if THRESHOLD_MIN <= current <= THRESHOLD_MAX:
        values.add(round(current, 2))
    try:
        selected = round(float(chosen), 2) if chosen is not None else round(current, 2)
    except ValueError:
        selected = round(current, 2)
    return [{"value": f"{t:.2f}", "label": f"{fmt_pct(t)}{'(預設)' if t == round(default, 2) else ''}",
             "selected": t == selected} for t in sorted(values)]


def settings_view(current: AppConfig, *, default_threshold: float, models: dict[str, str], ollama: str,
                  version: str, changes: list[dict[str, Any]], cloud_ready: bool,
                  values: dict[str, Any] | None = None, errors: dict[str, str] | None = None) -> dict[str, Any]:
    """設定頁的畫面資料。current 是目前生效的設定;default_threshold 是 config.yaml 的門檻(選項標「預設」);
    models、ollama 是 /healthz 同一套的模型名稱與 Ollama 連線狀態;changes 是最近的設定變更;
    cloud_ready:雲端帳號與金鑰設好了沒。values、errors:存檔失敗時重畫用(留住剛剛的選擇與中文說明)。
    """
    values, errors = values or {}, errors or {}
    demo = current.provider == "mock"
    scope = "、".join(local_only_scope(current.local_only_doc_types))
    status = [
        {"label": "辨識模式", "value": _MODE_TEXT.get(current.provider, current.provider),
         "pill": _model_pill(current.provider, ollama)},
        {"label": "使用的模型", "value": _models_text(models)},
        {"label": "只在本機處理",
         "value": "全部文件(展示模式不會把文件送出這台電腦)" if demo else f"{scope},以及沒選類型的文件"},
        {"label": "自動存檔門檻",
         "value": f"驗證信心 {fmt_pct(current.auto_threshold)} 以上才自動存檔,其餘交給家人複核"},
        {"label": "版本", "value": version},
    ]
    # 重畫時留住剛剛的選擇;被拒絕的那一欄回到目前的值(例如沒有金鑰時選了雲端)
    keep = values.get(PROVIDER) in PROVIDER_LABELS and PROVIDER not in errors
    picked = values[PROVIDER] if keep else current.provider
    providers = [{"value": value, "label": label, "checked": value == picked,
                  # 沒有金鑰不能選雲端;從本機切到雲端要先確認(app.js 換成帶確認框的「儲存設定」)
                  "disabled": value == "workers_ai" and not cloud_ready,
                  "confirm": value == "workers_ai" and current.provider != "workers_ai"}
                 for value, label in PROVIDER_LABELS.items()]
    return {
        "status": status,
        "prefs": [{"name": name, "label": label, "options": options, "default": default}
                  for name, label, options, default in DEVICE_PREFS],
        "demo": demo,
        "providers": providers,
        "cloud_hint": "" if cloud_ready or demo else "這台電腦還沒有設定雲端模型的帳號與金鑰,所以不能選(設定方法見部署指南)。",
        "lockline": f"{scope}一律只在這台電腦處理,這一項不能關。",
        "thresholds": _threshold_options(current.auto_threshold, default_threshold,
                                         None if THRESHOLD in errors else values.get(THRESHOLD)),
        "threshold_error": THRESHOLD in errors,
        "errors": list(errors.values()),
        "cloud_confirm": cloud_ask(current.local_only_doc_types),
        "purge_confirm": PURGE_ASK,
        "changes": [{"when": fmt_when(c.get("created_at")), "text": setting_change_text(c),
                     "actor": c.get("actor") or ""} for c in changes],
    }


# ---- Jinja2 ----------------------------------------------------------------

env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
env.globals.update(
    conf_class=conf_class, fmt_conf=fmt_conf, fmt_pct=fmt_pct, icon=icon, type_icon=type_icon,
    review_reason=review_reason,
    BRAND_NAME=BRAND_NAME, BRAND_SUB=BRAND_SUB, BRAND_FULL=BRAND_FULL,
    MEDICATION_DISCLAIMER=MEDICATION_DISCLAIMER, UNSCHEDULED_NOTE=UNSCHEDULED_NOTE,
)


def render(template: str, **context: Any) -> str:
    return env.get_template(template).render(**context)
