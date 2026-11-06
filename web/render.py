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
from starlette.datastructures import FormData

from src.actions import BILL_DEFAULT_TITLE, BILL_TITLES, CALENDAR, MEDICATION_SCHEDULE
from src.config import DEFAULT_OLLAMA_MODEL, DEFAULT_WORKERS_AI_MODEL, AppConfig
from src.dates import deadline_uses_base, parse_date
from src.duplicates import SAME_FILE, SAME_INVOICE, verified_invoice_number
from src.lottery import SETTING_PREFIX as LOTTERY_PREFIX
from src.lottery import Numbers, claim_hint, match, period_label, period_of, prize_text, qr_buyer_has_tax_id
from src.models import (
    CATEGORIES,
    DOC_LABELS,
    DOC_TYPES,
    IDENTITY_CATEGORY,
    UNCATEGORIZED,
    catalog_entry,
)
from src.parsing import clean_currency
from src.store import CONSENT_PREFIX, DOCUMENT_DELETED
from src.settings import PROVIDER, PURGED, THRESHOLD, THRESHOLD_CHOICES, THRESHOLD_MAX, THRESHOLD_MIN
from src.pipeline import PROCESSING, model_unavailable
from src.verify import INDEPENDENT_CHECKS, VERIFY_ERROR_SUMMARY
from src.verify.billcode import CHECK_BARCODE, CHECK_BARCODE_AMOUNT
from src.verify.checks import check_invoice_number_format
from src.verify.einvoice import (
    CHECK_QR,
    CHECK_QR_BUYER,
    CHECK_QR_DATE,
    CHECK_QR_INVOICE,
    CHECK_QR_RANDOM,
    CHECK_QR_SELLER,
    CHECK_QR_TOTAL,
)
from web.fields import field_spec, field_specs, required_specs

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def is_pdf(path: Any) -> bool:
    """原件是不是 PDF(看副檔名,上傳時已比對過檔頭);Path 或字串都可以,沒有就是 False。

    PDF 不能當 <img> 顯示,也讀不到寬高:結果頁、待複核與家人確認的縮圖都用這個判斷。
    """
    return str(path or "").lower().endswith(".pdf")


# 上傳時的類型選項(使用者 11/4:上傳只選類型,四大類只留在文件櫃):(送出值, 畫面文字),照畫面順序,「不確定」在最後。
# 送出值必須是 src.models.DOC_TYPES 裡的原字串:provider 的隱私分流只認這些值,其他任何字串都當作沒選而留在本機。
# 發票和收據合成一個選項、送「收據」:收據可能是醫療收據,一律只在本機辨識(src.settings.ALWAYS_LOCAL);
# 兩者共用發票版提示詞(src.prompts),讀出來是發票還是收據由模型看內容決定。「不確定」送出後轉成 None,交給模型判斷。
UNSURE = "不確定"
UPLOAD_TYPES: tuple[tuple[str, str], ...] = (
    ("帳單", "帳單"),
    ("公文", "公文"),
    ("藥袋", "藥袋"),
    ("收據", "發票／收據"),
    (UNSURE, "不確定"),
)
TYPE_VALUES: tuple[str, ...] = tuple(v for v, _ in UPLOAD_TYPES if v != UNSURE and v in DOC_TYPES)
# 舊版首頁的「發票」選項併進「發票／收據」:還開著舊頁面送來的、舊文件重新辨識時記著的,都當作選了這一項
_MERGED_TYPES = {"發票": "收據"}

MEDICATION_DISCLAIMER = "本系統只協助閱讀，不提供醫療建議；用藥請依醫師與藥師指示。"

# 產品名稱集中在這裡(10/1 定案;舊的英文名稱撞名,已棄用)。短名稱「看有」= 台語 khuànn-ū「看得懂」,
# 副標說明用途;正式題目「結合視覺語言模型與分層核對機制之高齡家庭文書輔助系統」只用在文件
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
    "refresh": '<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v6h-6"/>',
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
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.4-4.4"/>',
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

    fail 優先;pass 再分兩種:match 是與 QR、帳單條碼等獨立證據相符,pass 只通過格式或合理性規則(弱證據)。
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


# 程式算的、家人填的:AI 沒有讀這些,不標「家人改過」
_NOT_READ_BY_AI = frozenset({"fields.deadline", "fields.deadline_basis", "fields.received_date"})


def _comparable(value: Any) -> Any:
    """比較兩個讀值用:空的都算 None,數字換成浮點,清單換成文字的 tuple,其他去掉前後空白。"""
    if _is_empty(value):
        return None
    if _is_number(value):
        return float(value)
    if isinstance(value, (list, tuple)):
        return tuple(_plain(v) for v in value if not _is_empty(v))
    return _plain(value)


def _shown(kind: str, value: Any, currency: Any) -> str:
    """一個讀值在畫面上的寫法(「家人改過,原本讀成…」用);空的回空字串。"""
    if _is_empty(value):
        return ""
    if kind == "amount":
        return fmt_amount(value, currency)
    if kind == "date":
        return fmt_date(value)
    if isinstance(value, (list, tuple)):
        return "、".join(_plain(v) for v in value if not _is_empty(v))
    return _plain(value)


def field_rows(result: dict[str, Any], original: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """整理「讀到的內容」表格:有值的欄位 + 必要欄位(沒讀到也要列出,提醒家人補)。

    original 是家人更正之前 AI 讀到的值(ai_reading):和現在不一樣的欄位帶 was(原本讀成什麼),
    參考 Rossum 把 AI 讀的和人改過的分開;只寫誰改過,不寫成被證實(原則 1)。
    """
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
                               "value": "", "value_list": None, "missing": "", "was": None}
        if original is not None and key not in _NOT_READ_BY_AI:
            before = _field_value(original, key)
            if _comparable(before) != _comparable(value):
                row["was"] = _shown(spec.kind, before, original.get("currency")) or "沒有讀到"
        if is_unreadable:
            row["missing"] = "讀不清楚，請看原件"
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
        row["check_text"] = {"match": _match_text(result), "pass": "已檢查", "fail": "不符"}.get(st or "", "")
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
        "name": _plain(raw.get("name") or raw.get("drug") or "（藥名讀不清）"),
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

    # 院所、日期、電話:(欄名, 內容) 兩個值,樣板排成兩欄的 <dl>,內容對齊(使用者 11/6)
    info: list[tuple[str, str]] = []
    for key, label in (("hospital", "醫療院所"), ("dispensed_date", "調劑日期"),
                       ("pharmacist_phone", "藥師電話")):
        if not _is_empty(payload.get(key)):
            value = fmt_date(payload[key]) if key == "dispensed_date" else _plain(payload[key])
            info.append((label, value))
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

# 與獨立證據(電子發票 QR、帳單條碼的應繳金額)比對的檢查說「相符/不符」;格式、檢查碼、合理性只是規則,
# 通過不代表讀對,說「通過/沒通過」(弱證據不得呈現成已證實)
_BILL_EVIDENCE_NOTE = ("核對由程式比對帳單上的超商條碼，並檢查格式與合理性，不是 AI 自己打分數。"
                      "條碼只能證明金額和條碼上的一樣，不能證明帳單屬實。")
_BILL_RULES_NOTE = "這張帳單沒有讀到超商條碼，程式只檢查格式與合理性，不能證明讀對，請對照原件。"


def _by_barcode(result: dict[str, Any]) -> bool:
    """獨立證據是帳單的超商條碼(條碼應繳金額有比對出結果),不是電子發票的 QR。"""
    check = dict(_checks(result.get("verification"))).get(CHECK_BARCODE_AMOUNT) or {}
    return check.get("status") in ("pass", "fail")


def _match_text(result: dict[str, Any]) -> str:
    """欄位旁「與獨立證據相符」的寫法:比的是帳單條碼就說條碼,否則是電子發票的 QR。"""
    return "與條碼相符" if _by_barcode(result) else "與 QR 相符"


_STATUS_TEXT = {"pass": "相符", "fail": "不符", "skip": "無法核對"}
_RULE_STATUS_TEXT = {"pass": "通過", "fail": "沒通過", "skip": "無法核對"}
_STATUS_ICON = {"pass": "check", "fail": "x", "skip": "dash"}


def verification_view(result: dict[str, Any]) -> dict[str, Any]:
    """把 result["verification"] 整理成一行打勾列與明細。沒有資料時 overall 為 "none"。

    conflict:有與獨立證據矛盾的檢查;evidence:有任何與獨立證據比對過的檢查(相符或不符)。
    沒有 evidence 時整份只有弱證據,note 要講清楚「不能證明讀對」。
    """
    is_bill = result.get("doc_type") == "帳單"
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
        overall, text = "fail", f"有 {counts['fail']} 項{'不符' if conflict else '沒通過'}，請對照原件"
    elif matched:
        overall, text = "pass", f"{'條碼相符' if _by_barcode(result) else 'QR Code 相符'} {matched} 項"
    elif counts["pass"]:
        overall, text = "pass", f"檢查通過 {counts['pass']} 項"
    else:
        overall, text = "skip", "這份文件沒有能自動核對的項目"

    if evidence:
        note = _BILL_EVIDENCE_NOTE if _by_barcode(result) else "核對由程式比對發票上的 QR Code，並檢查格式與檢查碼，不是 AI 自己打分數。"
    elif counts["pass"] or counts["fail"]:
        note = _BILL_RULES_NOTE if is_bill else "這份文件沒有 QR Code 可以比對，程式只檢查格式與合理性，不能證明讀對，請對照原件。"
    else:
        note = ""
    vc = result.get("verified_confidence")
    confidence_text = ""
    if _is_number(vc):
        confidence_text = f"驗證信心 {vc * 100:.0f}%（依核對結果計算，不是 AI 自評）"
    return {"checks": checks, "counts": counts, "overall": overall,
            "overall_text": text, "overall_icon": _STATUS_ICON.get(overall, "dash"),
            "conflict": conflict, "evidence": evidence, "note": note,
            "confidence_text": confidence_text}


# ---- 行動 -------------------------------------------------------------------

_KIND_LABELS = {CALENDAR: "期限提醒", MEDICATION_SCHEDULE: "服藥時間表"}
_KIND_ICONS = {CALENDAR: "calendar", MEDICATION_SCHEDULE: "pill"}

# 取消/恢復提醒(F8)的表單值;按鈕文字與圖示依提醒目前是否生效決定
REMINDER_CANCEL, REMINDER_RESTORE = "cancel", "restore"
BILL_DEADLINE_NOTE = "期限由 AI 讀取，請對照帳單。"   # 自動提醒的期限沒有人看過


def is_auto_reminder(action: dict[str, Any]) -> bool:
    """自動列入的期限提醒:只有它能在結果頁取消與恢復(原則 4);要家人確認的行動在「家人確認」處理。"""
    return action.get("kind") == CALENDAR and action.get("tier") == "auto"


def _action_state(action: dict[str, Any]) -> tuple[str, str, str]:
    """(畫面文字, 樣式, 圖示):依分級與狀態。自動行動建立時就已完成,只看是否被退回。"""
    tier, status, kind = action.get("tier"), action.get("status"), action.get("kind")
    if status == "rejected":
        return ("已退回" if tier == "confirm" else "已取消", "off", "x")
    if status == "completed":
        return ("已完成", "auto", "check")
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
            view["lines"].append(f"日期：{fmt_date(payload['date'])}")
        # description 是多行文字(W1-C:最後一行是「只提醒,不會替您付款或回覆」),逐行顯示
        for line in str(payload.get("description") or "").splitlines():
            if line.strip():
                view["lines"].append(line.strip())
        view["date_text"] = fmt_date(payload.get("date") or "")
        view["basis"] = _plain(payload.get("basis") or "")
        # 確認頁的翻牌(_flap.html)要數字:月、日是整數,補零由翻牌做。和 date_text 同一個格式判斷(YYYY-MM-DD),
        # 牌和旁邊的日期全文一定對得上;沒有日期或格式不對是空字串,不畫牌
        m = _ISO_DATE.match(str(payload.get("date") or "").strip())
        view["month"], view["day"] = (int(m.group(2)), int(m.group(3))) if m else ("", "")
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
    PROCESSING: ("處理中", "wait", "clock"),     # 收件後還沒讀完(排隊或正在讀)
}


def _decision_state(doc: dict[str, Any]) -> tuple[str, str, str]:
    """文件狀態 (畫面文字, 樣式, 圖示);還沒有決策結果時是「處理中」。模型沒回應的不是照片讀不出來:「還沒讀」。"""
    if doc_busy(doc):
        return ("還沒讀", "wait", "clock")
    return _DECISION_STATE.get(doc.get("action"), ("處理中", "wait", "clock"))


def doc_busy(doc: dict[str, Any]) -> bool:
    """沒讀成是因為模型沒回應(服務沒開、逾時、忙碌):照片沒問題,可以重新辨識(src.pipeline.model_unavailable)。"""
    return doc.get("action") == "failed" and model_unavailable(doc.get("error"))
# 轉人工的白話原因:QR 比對項目 → 看得懂的欄位名
_QR_PLAIN = {CHECK_QR_TOTAL: "金額", CHECK_QR_DATE: "日期", CHECK_QR_INVOICE: "發票號碼",
             CHECK_QR_SELLER: "賣方統編", CHECK_QR_BUYER: "買方統編", CHECK_QR_RANDOM: "隨機碼"}


def review_reason(result: dict[str, Any] | None) -> str:
    """為什麼要複核,用白話說;不寫「驗證信心 0.10、門檻 0.80」這類工程用語(數字留在核對區)。

    順序比照 src/decision.py:不支援的類型 → 核對程式出錯 → 與 QR/條碼矛盾 → 規則沒通過 → 必要欄位沒讀到 → AI 沒把握。
    只依讀值與核對結果組句,不推論。
    """
    result = result or {}
    doc_type = result.get("doc_type") or ""
    required = required_specs(doc_type)
    if not required:   # 支援的類型都有必要欄位(REQUIRED_FIELDS),沒有就是不支援
        return "這類文件不在支援範圍（發票、收據、帳單、公文、藥袋），請對照原件確認。"
    verification = result.get("verification")
    if isinstance(verification, dict) and verification.get("_summary") == VERIFY_ERROR_SUMMARY:
        return "這次的核對沒有完成（核對程式出錯），請對照原件確認。"
    failed = [name for name, check in _checks(result.get("verification")) if check.get("status") == "fail"]
    conflicts = [name for name in failed if name in INDEPENDENT_CHECKS]
    if CHECK_BARCODE_AMOUNT in conflicts:
        return "讀到的金額和帳單上的超商條碼不一樣，請對照原件確認。"
    if conflicts:
        words = "、".join(dict.fromkeys(_QR_PLAIN.get(n, n.removeprefix("QR ")) for n in conflicts))
        return f"讀到的{words}和發票上的 QR Code 不一樣，請對照原件確認。"
    if failed:
        return f"有 {len(failed)} 項檢查沒通過，請對照原件確認。"
    missing = [spec.label for spec in required
               if _is_empty(_field_value(result, spec.key)) or _is_unreadable(spec.key, result.get("unreadable"))]
    if missing:
        return f"有必要的欄位沒讀到（{'、'.join(missing)}），請對照原件補上。"
    return "AI 對這份文件的讀值沒有把握，請對照原件確認。"


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
            parts.append(f"上面有 {len(names)} 種藥：{'、'.join(names)}。")
        return "".join(parts)
    def label(key: str) -> str:
        return field_spec(doc_type, key).label

    if doc_type == "公文" and fields.get("subject"):
        parts.append(f"{label('fields.subject')}：{_plain(fields['subject'])}。")
    if result.get("vendor"):
        parts.append(f"{label('vendor')}：{result['vendor']}。")
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
    if doc_busy(doc) and not doc_label(doc):
        return "還沒讀的文件"   # 模型沒回應:不是讀不出來
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


# ---- 結果頁第一屏:先回答三個問題(使用者 11/4) -------------------------------------
# 「這是什麼 → 多少錢或要做什麼 → 什麼時候前」,接著一句核對結果;處理狀態與操作放在下面(status_rows),
# 核對細項、辨識來源與完整欄位收起來。只重排已讀到的欄位與已產生的行動,文件文字一律當資料(原則 3)。

def _iso(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _line(result: dict[str, Any], key: str, value: str, name: str, *, label: str = "",
          big: bool = False) -> dict[str, Any]:
    """第一屏的一行重點。value 是空的就照實寫「沒有讀到…」,模型自己標成讀不清的寫「…讀不清楚，請看原件」(原則 5)。

    label 空白表示值自己就說得清楚(「1,286 元」「11月20日前繳費」);name 是沒讀到時說的欄位名;big 放大(金額、期限)。
    note 是值後面的狀態牌(還有幾天、已繳費),note_class 同清單的樣式(manual 紅牌、wait 黃牌、空白是外框牌)。
    flap 是期限那一行的翻牌(_due_line),其他行是 None。
    """
    row = {"key": key, "label": label, "value": value, "big": big, "missing": "", "note": "", "note_class": "",
           "flap": None}
    if _is_unreadable(key, result.get("unreadable")):
        row.update(value="", missing=f"{name}讀不清楚，請看原件")
    elif not value:
        row["missing"] = f"沒有讀到{name}"
    return row


def _before(value: Any, verb: str) -> str:
    """期限那一行:2026-11-20 →「11月20日前繳費」;不是 YYYY-MM-DD 的照讀到的字接上去;沒有值回空字串。"""
    if _is_empty(value):
        return ""
    return f"{fmt_short_date(value) or _plain(value)}前{verb}"


def _due_line(result: dict[str, Any], key: str, value: Any, verb: str, name: str) -> dict[str, Any]:
    """期限那一行(帳單的繳費期限、公文的辦理期限):字照 _before。值是 YYYY-MM-DD 的另外給翻牌的月、日與「前繳費」
    (v5 看板:牌只給眼睛看,畫面上原本的字藏起來給讀屏與朗讀);不是日期、沒讀到或讀不清楚就不畫牌,照實寫字。"""
    row = _line(result, key, _before(value, verb), name, big=True)
    m = _ISO_DATE.match(value.strip()) if isinstance(value, str) and row["value"] else None
    if m:
        row["flap"] = {"month": int(m.group(2)), "day": int(m.group(3)), "verb": f"前{verb}"}
    return row


def _basic_lines(result: dict[str, Any], doc_type: str, *, every: bool) -> list[dict[str, Any]]:
    """商家(院所)、日期、金額三行,金額放大。every:發票與收據三行都列、沒讀到的照實寫;其他類型只列讀到的。"""
    lines = []
    for key, value, big in (("vendor", _plain(result.get("vendor") or ""), False),
                            ("date", fmt_date(result.get("date")), False),
                            ("amount", _amount_text(result), True)):
        name = field_spec(doc_type, key).label
        if every or value:
            lines.append(_line(result, key, value, name, label=name, big=big))
    return lines


def _todo_list(value: Any) -> list[str]:
    """公文的應辦事項:清單照順序取有字的;模型給成一個字串時當成一項(不能一個字一個字拆開)。"""
    items = value if isinstance(value, list) else [value] if isinstance(value, str) else []
    return [_plain(x) for x in items if not _is_empty(x) and _plain(x).strip()]


def answer_view(result: dict[str, Any], actions: list[dict[str, Any]],
                verification: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """結果頁第一屏:先回答「這是什麼、多少錢或要做什麼、什麼時候前」,再用一句話說核對到哪裡(使用者 11/4)。

    各類型的重點:帳單是應繳金額與繳費期限;公文是發文機關、辦理期限與要做的事(應辦事項,沒有就是主旨);
    藥袋是服藥時間表;發票、收據與其他類型是商家、日期、金額。
    lines 見 _line;todos 是公文的應辦事項;medication 是服藥時間表,med_state 是它等不等家人確認;
    check 是核對那一句(check_line);footer 是「說明」裡的話(帳單的期限由 AI 讀取、公文的期限怎麼算的)。
    status 是主要行動(期限提醒或服藥時間表)的狀態與按鈕,畫在下面的「處理狀態」(status_rows)。
    actions 是 action_view 的結果;等家人確認的可以帶 waiting(等了幾天,見 waiting_text)。
    期限提醒還生效時,期限那一行寫還有幾天(due_text,程式算的);標示已繳費或已辦理後改寫「已繳費」「已辦理」,
    取消提醒後不寫,免得已經繳的帳單寫「逾期」。
    """
    today = today or date.today()
    doc_type = result.get("doc_type") or "其他"
    fields = result.get("fields") or {}
    amount = _amount_text(result)
    ans: dict[str, Any] = {"lines": [], "todos": [], "medication": None, "med_state": None, "check": None,
                           "status": None, "footer": "", "set_received": False}
    lines: list[dict[str, Any]] = ans["lines"]
    due_line: dict[str, Any] | None = None
    due_day: date | None = None
    primary = None
    if doc_type == "帳單":
        lines.append(_line(result, "amount", amount, "應繳金額", big=True))
        due_line = _due_line(result, "fields.due_date", fields.get("due_date"), "繳費", "繳費期限")
        due_day = _iso(fields.get("due_date"))
        lines.append(due_line)
        if due_line["value"]:
            ans["footer"] = BILL_DEADLINE_NOTE
        primary = _primary_action(actions, CALENDAR)
    elif doc_type == "公文":
        lines.append(_line(result, "vendor", _plain(result.get("vendor") or ""), "發文機關", label="發文機關"))
        if fields.get("deadline") or _is_empty(fields.get("deadline_text")):
            due_line = _due_line(result, "fields.deadline", fields.get("deadline"), "辦理", "辦理期限")
            due_day = _iso(fields.get("deadline"))
        else:   # 期限原文算不出日期(例如「儘速辦理」):照抄公文上的字,不猜日期
            due_line = _line(result, "fields.deadline_text", _plain(fields["deadline_text"]), "辦理期限", label="期限")
        lines.append(due_line)
        if fields.get("deadline") and not _is_empty(fields.get("deadline_text")):
            basis = _plain(fields.get("deadline_basis") or "期限以公文原文為準。")
            ans["footer"] = f"依公文上的「{_plain(fields['deadline_text'])}」推算，{basis}"
            ans["set_received"] = deadline_uses_base(fields["deadline_text"])
        todos = _todo_list(fields.get("required_actions"))
        if todos:
            ans["todos"] = todos[:5]
        else:
            lines.append(_line(result, "fields.subject", _plain(fields.get("subject") or ""), "主旨", label="主旨"))
        if amount:
            lines.append(_line(result, "amount", amount, "金額", label="金額"))
        primary = _primary_action(actions, CALENDAR)
    elif doc_type == "藥袋" or med_items(result):
        primary = _primary_action(actions, MEDICATION_SCHEDULE)
        medication = (primary or {}).get("medication") or medication_view({"items": fields.get("items") or []})
        if medication["slots"] or medication["prn"] or medication["unscheduled"]:
            ans.update(medication=medication, footer="照藥袋上印的字整理，請對照藥袋。")
        else:
            lines.extend(_basic_lines(result, "藥袋", every=False))
            lines.append(_line(result, "fields.items", "", "藥品清單"))
    else:
        lines.extend(_basic_lines(result, doc_type, every=doc_type in ("發票", "收據")))
        if doc_type == "發票":
            primary = _primary_action(actions, CALENDAR)   # 對獎頁加的領獎提醒:在下面的處理狀態取消/恢復

    keys = [line["key"] for line in lines]
    keys += ["fields.required_actions"] if ans["todos"] else []
    keys += ["fields.items"] if ans["medication"] else []
    ans["check"] = check_line(result, verification, keys)

    if primary:
        status = primary.get("status")
        st = ans["status"] = {
            "id": primary["id"], "kind": primary.get("kind"), "tier": primary.get("tier"), "status": status,
            "text": primary["state_text"], "icon": primary["state_icon"], "class": primary["state_class"],
            "waiting": primary.get("waiting") or "", "toggle": None, "undo": None,
        }
        if primary.get("kind") == CALENDAR:
            button, undo = done_words(result)
            if status == "completed":
                # 首頁「已完成」只留幾天,這裡不限時間;能不能按由 can_remind 決定
                st["undo"] = {"id": primary["id"], "label": undo}
                if due_line is not None and due_line["value"]:
                    due_line["note"] = button
            elif status != "rejected" and due_line is not None and due_line["value"]:
                # 生效中的期限提醒會一直列在首頁,過期了也一樣(reminder_rows),直到按了完成或取消
                due_line["note"], due_line["note_class"] = due_text(due_day, today)
            if is_auto_reminder(primary) and status != "completed":
                # 自動提醒在下面取消;取消後同一個位置可以恢復(不跳確認框,按錯能馬上救回)
                cancelled = status == "rejected"
                if cancelled:
                    st["text"] = "已取消提醒"
                st["toggle"] = {
                    "id": primary["id"],
                    "decision": REMINDER_RESTORE if cancelled else REMINDER_CANCEL,
                    "label": "恢復提醒" if cancelled else "取消提醒",
                    "icon": "undo" if cancelled else "x",
                }
        elif primary.get("kind") == MEDICATION_SCHEDULE and ans["medication"]:
            # 服藥時間表一律等家人確認才算數:第一屏就寫它現在的狀態
            waiting = primary["state_class"] == "wait"
            ans["med_state"] = {"text": st["waiting"] or primary["state_text"], "class": "wait" if waiting else ""}
    for line in lines:
        # 有問題的值當場標出來(寫成字,不只靠紅色);不符的期限也不必再算還有幾天
        problem = _failed_note(result, line["key"]) if line["value"] else ""
        if problem:
            line["note"], line["note_class"] = problem, "manual"
    return ans


# 「核對那一句」裡的欄位短名(使用者 11/4 的例子:「金額與條碼相符；期限請對照原件。」)
_SHORT_NAMES = {"amount": "金額", "date": "日期", "fields.due_date": "期限", "fields.deadline": "期限",
                "fields.deadline_text": "期限", "fields.required_actions": "要做的事", "fields.subject": "主旨",
                "fields.items": "藥名與服藥時段"}
_VENDOR_NAMES = {"公文": "機關", "藥袋": "醫療院所", "帳單": "開單單位"}   # 其他類型的 vendor 叫「商家」
_TOP_LEVEL_FIELDS = frozenset({"date", "amount", "vendor", "invoice_number", "currency"})


def _field_key(name: str) -> str:
    """核對項目寫的欄位名 → FieldSpec 的鍵:'due_date' 與 'fields.due_date' 都是 'fields.due_date'。"""
    return name if name in _TOP_LEVEL_FIELDS or name.startswith("fields.") else f"fields.{name}"


def _short_name(doc_type: str, key: str) -> str:
    if key == "vendor":
        return _VENDOR_NAMES.get(doc_type, "商家")
    return _SHORT_NAMES.get(key) or field_spec(doc_type, key).label


def _evidence(result: dict[str, Any]) -> str:
    """句子裡的獨立證據:「金額與條碼相符」「日期與 QR Code 相符」。"""
    return "條碼" if _by_barcode(result) else " QR Code "


def _failed_note(result: dict[str, Any], key: str) -> str:
    """第一屏的值旁邊標問題:和獨立證據矛盾寫「和條碼不符」,規則沒通過寫「沒通過檢查」;沒問題回空字串。"""
    note = ""
    for name, check in _checks(result.get("verification")):
        fields = {_field_key(f) for f in check.get("fields") or [] if isinstance(f, str)}
        if check.get("status") == "fail" and key in fields:
            if name in INDEPENDENT_CHECKS:
                return f"和{_evidence(result)}不符"
            note = "沒通過檢查"
    return note


def _no_evidence(result: dict[str, Any]) -> str:
    """一個欄位都沒和機讀碼比對到時,先說為什麼:帳單讀不到超商條碼、電子發票讀不到 QR Code(其他類型本來就沒有)。"""
    checks = dict(_checks(result.get("verification")))
    doc_type = result.get("doc_type")
    if doc_type == "帳單" and (checks.get(CHECK_BARCODE) or {}).get("status") == "skip":
        return "沒有讀到條碼，"
    if doc_type == "發票" and (checks.get(CHECK_QR) or {}).get("status") == "skip":
        return "沒有讀到 QR Code，"
    return ""


def check_line(result: dict[str, Any], verification: dict[str, Any], keys: list[str]) -> dict[str, str]:
    """第一屏「核對那一句」:依核對結果由程式組句,不看模型怎麼說(使用者 11/4)。回傳 {text, tone}。

    keys 是第一屏列出的欄位(依畫面順序)。和獨立證據(電子發票 QR、帳單超商條碼)相符的欄位才說「相符」;
    只過格式與合理性規則、或根本沒有檢查的,一律寫「請對照原件」,不說成相符(原則 1)。
    與獨立證據矛盾、規則沒通過、核對程式出錯時 tone 是 manual(紅字):這種問題直接攤在第一屏,不收起來。
    藥袋請對照的是藥袋。
    """
    doc_type = result.get("doc_type") or ""
    where = "藥袋" if doc_type == "藥袋" else "原件"
    raw = result.get("verification")
    if isinstance(raw, dict) and raw.get("_summary") == VERIFY_ERROR_SUMMARY:
        return {"text": f"這次的核對沒有完成，請對照{where}。", "tone": "manual"}
    if not verification["checks"]:
        return {"text": f"未核對：沒有能自動核對的項目，請對照{where}。", "tone": ""}
    if verification["conflict"]:
        return {"text": review_reason(result), "tone": "manual"}   # 「讀到的金額和帳單上的超商條碼不一樣…」
    status = _field_checks(raw)
    names = list(dict.fromkeys(_short_name(doc_type, k) for k in keys))
    matched = list(dict.fromkeys(_short_name(doc_type, k) for k in keys if status.get(k) == "match"))
    parts = [f"{'、'.join(matched)}與{_evidence(result)}相符"] if matched else []
    failed: list[str] = []
    for name, check in _checks(raw):
        if check.get("status") != "fail":
            continue   # 走到這裡的 fail 都只是規則(和獨立證據矛盾的在上面已經處理)
        on = [_field_key(f) for f in check.get("fields") or [] if isinstance(f, str)]
        failed += [_short_name(doc_type, k) if k in keys else field_spec(doc_type, k).label for k in on] or [name]
    if failed:
        parts.append(f"{'、'.join(dict.fromkeys(failed))}沒通過檢查，請對照{where}")
        return {"text": "；".join(parts) + "。", "tone": "manual"}
    rest = [n for n in names if n not in matched]
    if rest:
        parts.append(("" if matched else _no_evidence(result)) + f"{'、'.join(rest)}請對照{where}")
    return {"text": "；".join(parts or [f"請對照{where}"]) + "。", "tone": ""}


def trust_notes(provider: str, local_only: tuple[str, ...] | list[str]) -> list[tuple[str, str]]:
    """每頁固定的安心說明(圖示, 文字)。隱私那一句依實際設定寫,不承諾系統做不到的事。"""
    if provider == "workers_ai":
        privacy = cloud_privacy(local_only)   # 和雲端模式首頁上傳區的說明同一句話
    elif provider == "mock":
        privacy = "展示模式：使用模擬讀值，文件不會送出這台電腦。"
    else:
        privacy = "文件都在這台電腦上辨識，不會送到雲端。"
    return [
        ("lock", f"{privacy}照片與結果存在這台電腦。"),
        ("shield", "只協助閱讀：只提醒，不會替您付款、回覆或做醫療判斷。"),
        ("check", "核對結果由程式依規則檢查，不是 AI 自己打分數。"),
    ]


def _due_date(action: dict[str, Any]) -> date | None:
    """期限提醒的日期;沒有日期或日期讀不懂回 None。"""
    try:
        return date.fromisoformat(str((action.get("payload") or {}).get("date")))
    except ValueError:
        return None


def overdue_days(action: dict[str, Any], today: date) -> int:
    """期限提醒過期幾天;還沒過期或沒有可用的日期回 0。只看日期,狀態由呼叫端判斷。"""
    due = _due_date(action)
    return max((today - due).days, 0) if due else 0


def overdue_text(days: int) -> str:
    return f"逾期 {days} 天"


SOON_DAYS = 3      # 期限前幾天開始在清單上寫「還有 N 天」(系統不推播,長輩打開時看到的字就是全部的提醒)


def due_text(due: date | None, today: date) -> tuple[str, str]:
    """期限離今天多遠:(文字, 樣式)。逾期是 manual、三天內(含今天)是 wait、更遠的樣式空白;沒有日期回 ("", "")。
    寫「還有 N 天」,不寫「提前 N 天提醒」:系統不會提前通知(Q3 修訂)。"""
    if due is None:
        return "", ""
    days = (due - today).days
    if days < 0:
        return overdue_text(-days), "manual"
    if days == 0:
        return "今天到期", "wait"
    if days == 1:
        return "明天到期", "wait"
    return f"還有 {days} 天", "wait" if days <= SOON_DAYS else ""


def waited_days(action: dict[str, Any], today: date) -> int:
    """要家人確認的事從建立到今天等了幾天;看不出來回 0。"""
    try:
        return max((today - datetime.fromisoformat(str(action.get("created_at"))).date()).days, 0)
    except ValueError:
        return 0


def waiting_text(action: dict[str, Any], today: date) -> str:
    """等家人確認的說法:等了一天以上寫「等家人確認 N 天」,長輩才知道要不要打電話請家人看;當天寫「等待家人確認」。"""
    days = waited_days(action, today)
    return f"等家人確認 {days} 天" if days else "等待家人確認"


def _who(actor: Any, names: dict[str, str]) -> str:
    """操作紀錄的操作者 → 畫面上的稱呼(家庭成員的稱呼;這台電腦直接開的寫「這台電腦」)。"""
    return "這台電腦" if actor == "local" else names.get(actor or "") or actor or "未記錄身分"


REJECTED_DAYS = 30     # 退回通知留幾天:沒按「已讀」也不會一直掛著
REJECT_EVENTS = ("reject_document", "reject", "seen_reject")   # rejected_rows 要的操作紀錄種類


def rejected_rows(docs: list[dict[str, Any]], actions: list[dict[str, Any]], events: list[dict[str, Any]],
                  today: date, names: dict[str, str], viewer: str | None = None) -> list[dict[str, Any]]:
    """被退回的文件與事項(參考 Rossum:退回時通知對方):別人退回的才列給這個人看,按「已讀」才收掉。

    events 是 Store.list_audit_events(event=REJECT_EVENTS)(新的在前):reject_document 是待複核的文件退回重拍,
    reject 是在家人確認退回某件事;seen_reject 記收掉的是哪一筆(detail 的 event_id)。viewer 是現在看的人(自己退回的不列)。
    文件刪掉、後來又讀好或更正(不再是讀不出來),或事項被取代、恢復,就不再列。不寫金額與期限:可能就是讀錯才退回。
    """
    by_doc = {d["id"]: d for d in docs}
    by_action = {a["id"]: a for a in actions}
    seen = {(e.get("detail") or {}).get("event_id") for e in events if e.get("event") == "seen_reject"}
    rows = []
    for event in events:
        kind = event.get("event")
        if kind not in ("reject_document", "reject") or event.get("id") in seen:
            continue
        if viewer is not None and event.get("actor") == viewer:
            continue
        try:
            when = datetime.fromisoformat(str(event.get("created_at")))
        except ValueError:
            continue
        if (today - when.date()).days > REJECTED_DAYS:
            continue
        row = {"sub": f"{_who(event.get('actor'), names)} {when.month}/{when.day} 退回", "when": "", "amount": "",
               "note_class": "wait", "seen": {"id": event["id"]}}
        if kind == "reject_document":
            doc = by_doc.get(event.get("document_id"))
            if doc is None or doc.get("action") != "failed":
                continue
            row.update(href=f"/doc/{doc['id']}", title=f"請重新拍：{doc_title(doc)}", note="要重拍")
        else:
            action = by_action.get(event.get("action_id"))
            if action is None or action.get("superseded") or action.get("status") != "rejected":
                continue
            row.update(href=f"/doc/{action['document_id']}", title=action_view(action)["title"], note="已退回")
        rows.append(row)
    return rows


HOME_REMINDERS = 6     # 首頁「要記得的事」列幾筆,其他在 /reminders


SOON_GROUP_DAYS = 7     # 「快到期」:7 天內(使用者 11/5 看樣稿選的);寫「還有 N 天」也到這裡為止
DOC_LINK = "看文件"
# 「要記得的事」的分組,照這個順序(使用者 11/5,六點的第 1 點:已逾期、快到期、需要家人協助)。
# 被退回的是別人退回才有;其他是更遠的期限與已確認的服藥表,事情不能因為不在前三組就消失
REMINDER_GROUPS: tuple[tuple[str, str], ...] = (
    ("overdue", "已逾期"), ("rejected", "被退回的"), ("soon", "快到期"), ("family", "需要家人協助"), ("other", "其他"))


def _row_label(title: str, doc: dict[str, Any]) -> str:
    """清單這一列叫什麼(「電費 1,286 元」的「電費」):帳單寫種類,種類不在對照表的寫文件名稱,公文寫主旨。"""
    if title == BILL_DEFAULT_TITLE:
        return doc_title(doc) if doc else "帳單"
    if title in BILL_TITLES.values():
        return title.removeprefix("繳")
    return title.replace("公文期限：", "公文：", 1)


def _due_short(due: date, today: date) -> str:
    """清單上的期限(讀屏念的字;畫面上是翻牌):「11/20 前」;不是今年的寫年份,才不會把去年的看成今年的。"""
    return f"{due.month}/{due.day} 前" if due.year == today.year else f"{due.year}/{due.month}/{due.day} 前"


def reminder_rows(docs: list[dict[str, Any]], actions: list[dict[str, Any]], today: date,
                  notices: list[dict[str, Any]] | None = None, *, can_confirm: bool = True) -> list[dict[str, Any]]:
    """「要記得的事」的全部列,依 REMINDER_GROUPS 排好(group 是這一列屬於哪一組,見 group_rows):
    已逾期(最近才過期的在前)→ 被退回的(notices,見 rejected_rows)→ 快到期(近的在前)
    → 需要家人協助(等家人確認、核對不符)→ 其他(更遠的期限、已確認的服藥表)。

    docs 是 Store.list_documents 的結果,actions 是 Store.list_actions 的原始列;只拿已有的欄位排版,文件文字一律當資料顯示。
    v5 車站時刻表(11/6):每列的 headline 只寫事項與金額(「電費 1,286 元」),期限另外給 due_month、due_day 畫成翻牌,
    不是今年的另外給 due_year;沒有日期三個都是空字串(空白的暗牌)。讀屏念 when(「11/20 前」)。狀態(note)是一樣寬的狀態牌。
    沒事的列不寫狀態(note 空白),要注意的才有字:逾期與核對不符是 manual、三天內到期(due_text)與等家人確認是 wait、
    七天內是 plain、已確認的服藥表是 auto。等家人確認的寫等了幾天(waiting_text)。
    逾期的提醒留到完成或取消,等家人確認的也一樣(到 /confirm 確認或退回);自動列入的才能在清單上一鍵收掉(dismiss)。
    can_confirm:看的人能不能確認。能的人(家人)等確認的列按鈕是「去確認」,還多一列待複核的份數;不能的人(長輩)
    是「看文件」,不帶到他按不了的家人確認頁(使用者 11/4)。
    """
    by_id = {d["id"]: d for d in docs}
    overdue, soon, waiting, later, alerts, done = [], [], [], [], [], []
    for raw in actions:
        # 被家人更正取代的舊行動不列(和結果頁同一條規則),不管它現在的狀態:只看狀態的話,舊期限可能和新的一起冒出來
        if raw.get("superseded") or raw.get("status") == "completed":
            continue      # 按了「已繳費/已辦理」的改列在「已完成」(completed_rows)
        v = action_view(raw)
        if v["state_class"] == "off":
            continue
        doc = by_id.get(v["document_id"]) or {}
        result = doc.get("result") or {}
        vendor = _plain(result.get("vendor") or (raw.get("payload") or {}).get("hospital") or "")
        is_waiting = v["state_class"] == "wait"
        wait_note = waiting_text(raw, today) if is_waiting else ""
        to_confirm = is_waiting and can_confirm
        row = {"group": "family" if is_waiting else "other",
               "href": f"/confirm?item={v['id']}" if to_confirm else f"/doc/{v['document_id']}",
               "link": "去確認" if to_confirm else DOC_LINK, "title": v["title"], "label": v["title"], "sub": vendor,
               "when": "", "amount": "", "note": wait_note, "note_class": "wait" if is_waiting else ""}
        if v["kind"] == CALENDAR:
            due = _due_date(raw)        # 日期壞了也不能靜默消失:不寫日期,排在其他期限後面
            late = overdue_days(raw, today)
            near = due is not None and (due - today).days <= SOON_GROUP_DAYS
            row.update(label=_row_label(v["title"], doc), due=due, sort=due.isoformat() if due else "9999-12-31",
                       amount=_amount_text(result) if result.get("doc_type") == "帳單" else "")
            if not is_waiting:          # 等家人確認的還沒生效,不能標成完成(POST 會回 409)
                row["done"] = {"id": v["id"], "label": done_words(result)[0]}
            text, cls = due_text(due, today)
            if near:                    # 逾期與七天內才寫;更遠的不說話
                if is_waiting:          # 期限的狀態佔了狀態欄,「等家人確認」移到開單單位後面
                    row["sub"] = "，".join(x for x in (vendor, wait_note) if x)
                row.update(note=text, note_class=cls or "plain")
            if late:
                if is_auto_reminder(raw):
                    row["dismiss"] = {"id": v["id"], "decision": REMINDER_CANCEL, "label": "取消提醒"}
                row["group"] = "overdue"
                overdue.append(row)
                continue
            if not is_waiting:
                row["group"] = "soon" if near else "other"
                (soon if near else later).append(row)
                continue
        elif v["kind"] == MEDICATION_SCHEDULE and v["status"] == "done":
            m = v["medication"] or {}
            slots = [s for s, _ in m.get("slots", [])] + (["需要時"] if m.get("prn") else [])
            row.update(sub="，".join(x for x in (vendor, "、".join(slots)) if x), note=v["state_text"], note_class="auto")
            done.append(row)
            continue
        if is_waiting:
            waiting.append(row)
    flagged = set()
    for doc in docs:
        vv = verification_view(doc.get("result") or {})
        # 家人已退回重拍(action 是 failed,讀值留著當紀錄)就是處理過了,不再掛在首頁
        if vv["overall"] == "fail" and doc.get("action") != "failed":
            flagged.add(doc["id"])
            alerts.append({"group": "family", "href": f"/doc/{doc['id']}", "link": DOC_LINK, "title": doc_title(doc),
                           "label": doc_title(doc), "sub": _plain((doc.get("result") or {}).get("vendor") or ""),
                           "when": "", "amount": _amount_text(doc.get("result") or {}),
                           # 清單欄位窄,只放短狀態;完整說明在結果頁
                           "note": "核對不符" if vv["conflict"] else "檢查沒通過", "note_class": "manual"})
    # 其他待複核的文件(AI 沒把握、少了欄位…)合成一列,只給能處理的人;核對不符的上面已經各有一列
    to_review = sum(1 for doc in docs if doc.get("action") == "review" and doc["id"] not in flagged)
    review_row = [{"group": "family", "href": "/review", "link": "去複核", "title": f"{to_review} 份文件等您複核",
                   "label": f"{to_review} 份文件等您複核", "sub": "", "when": "", "amount": "", "note": "",
                   "note_class": ""}] if to_review and can_confirm else []
    overdue.sort(key=lambda n: n["sort"], reverse=True)
    soon.sort(key=lambda n: n["sort"])
    later.sort(key=lambda n: n["sort"])
    rejected = [{**n, "group": "rejected", "link": DOC_LINK, "label": n["title"]} for n in notices or []]
    rows = overdue + rejected + soon + review_row + waiting + alerts + later + done
    for row in rows:
        # 日期格(v5 時刻表,11/6):月、日畫成翻牌,不是今年的另外寫年份;沒有日期是空白的暗牌。
        # 牌只給眼睛看,讀屏念 when;標題只寫事項與金額,不再接「｜11/20 前」
        due = row.pop("due", None)
        row.update(when=_due_short(due, today) if due else "", due_month=due.month if due else "",
                   due_day=due.day if due else "", due_year=due.year if due and due.year != today.year else "")
        row["headline"] = f"{row['label']} {row['amount']}" if row["amount"] else row["label"]
    return rows


def group_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 reminder_rows 的列分成畫面上的組(照 REMINDER_GROUPS 的順序,沒有列的組不出現)。"""
    return [{"key": key, "title": title, "rows": [r for r in rows if r["group"] == key]}
            for key, title in REMINDER_GROUPS if any(r["group"] == key for r in rows)]


DONE_COMPLETE, DONE_UNDO = "complete", "undo"     # 「已繳費/已辦理」與改回(POST /reminder/{id}/done 的 decision)
COMPLETED_DAYS = 7                                 # 「已完成」列幾天內按的


def done_words(result: dict[str, Any]) -> tuple[str, str]:
    """(按鈕, 改回) 的說法:帳單說「已繳費/改回未繳」,其他期限(公文)說「已辦理/改回未辦」(使用者 11/4:用正式用語)。"""
    return ("已繳費", "改回未繳") if (result or {}).get("doc_type") == "帳單" else ("已辦理", "改回未辦")


def completed_rows(docs: list[dict[str, Any]], actions: list[dict[str, Any]], events: list[dict[str, Any]],
                   today: date, names: dict[str, str], days: int | None = COMPLETED_DAYS) -> list[dict[str, Any]]:
    """「已完成」:days 天內按了「已繳費/已辦理」的期限提醒(首頁 7 天;/reminders 給 None,全部),寫出誰、哪天按的與「改回」。

    events 是 Store.list_audit_events(新的在前),取每個行動最近一次 complete_reminder;
    names 是登入 email → 家庭成員的稱呼。只是記下來,不代表系統查過或付過款。
    """
    by_id = {d["id"]: d for d in docs}
    pressed: dict[int, dict[str, Any]] = {}
    for event in events:
        if event.get("event") == "complete_reminder" and event.get("action_id") not in pressed:
            pressed[event["action_id"]] = event
    rows = []
    for raw in actions:
        event = pressed.get(raw.get("id"))
        if raw.get("status") != "completed" or raw.get("superseded") or event is None:
            continue
        try:
            when = datetime.fromisoformat(str(event.get("created_at")))
        except ValueError:
            continue
        if days is not None and (today - when.date()).days > days:
            continue
        result = (by_id.get(raw.get("document_id")) or {}).get("result") or {}
        button, undo = done_words(result)
        rows.append({"id": raw["id"], "title": action_view(raw)["title"],
                     "amount": _amount_text(result) if result.get("doc_type") == "帳單" else "",
                     "who": _who(event.get("actor"), names),
                     "when": f"{when.month}/{when.day}", "button": button, "undo": undo, "sort": when.isoformat()})
    rows.sort(key=lambda r: r["sort"], reverse=True)
    return rows


def cancelled_rows(docs: list[dict[str, Any]], actions: list[dict[str, Any]], events: list[dict[str, Any]],
                   names: dict[str, str]) -> list[dict[str, Any]]:
    """「已取消的提醒」:自動列入、後來取消的期限提醒(沒被更正取代的),連到結果頁,那裡可以恢復。

    events 是 Store.list_audit_events(新的在前),取每個行動最近一次 cancel_reminder,寫出誰、哪天取消的;
    舊資料沒有紀錄就只寫期限。最近取消的在前。
    """
    by_id = {d["id"]: d for d in docs}
    pressed: dict[int, dict[str, Any]] = {}
    for event in events:
        if event.get("event") == "cancel_reminder" and event.get("action_id") not in pressed:
            pressed[event["action_id"]] = event
    rows = []
    for raw in actions:
        if not is_auto_reminder(raw) or raw.get("status") != "rejected" or raw.get("superseded"):
            continue
        event = pressed.get(raw.get("id")) or {}
        try:
            when: datetime | None = datetime.fromisoformat(str(event.get("created_at")))
        except ValueError:
            when = None
        due = _due_date(raw)
        vendor = _plain(((by_id.get(raw.get("document_id")) or {}).get("result") or {}).get("vendor") or "")
        who = f"{_who(event.get('actor'), names)} {when.month}/{when.day} 取消" if when else ""
        rows.append({"href": f"/doc/{raw['document_id']}", "title": action_view(raw)["title"],
                     "sub": "，".join(x for x in (vendor, who) if x),
                     "when": f"{due.month}月{due.day}日前" if due else "", "sort": when.isoformat() if when else ""})
    rows.sort(key=lambda r: r["sort"], reverse=True)
    return rows


def ai_reading(doc: dict[str, Any], corrections: list[dict[str, Any]],
               retypes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """家人第一次更正之前 AI 讀到的值(更正紀錄最早那筆的 before),拿來標「家人改過,原本讀成…」。

    corrections 是 Store.list_corrections(新的在前);retypes 是這份文件的 retype 操作紀錄(新的在前):改過類型,
    之前的更正是另一個類型的讀值,只從最後一次改類型之後算,類型對不上的也不算。沒有更正過回 None。
    """
    doc_type = (doc.get("result") or {}).get("doc_type")
    since = str(retypes[0].get("created_at") or "") if retypes else ""
    for correction in reversed(corrections):
        before = correction.get("before") or {}
        if str(correction.get("created_at") or "") > since and before.get("doc_type") == doc_type:
            return before
    return None


def _summary_note(summary_is_ai: bool, corrected: bool) -> str:
    """白話解說下面那句說明:寫清楚這段是誰整理的(原則 7)。家人更正後不留 AI 的舊解說,改由系統依欄位整理。"""
    if summary_is_ai:
        return "這段是 AI 讀完整理的，重要的日期與金額請對照原件。"
    if corrected:
        return "家人更正過讀值，這段是系統依更正後的欄位整理的。"
    return "AI 沒有提供解說，這段是系統依讀到的欄位整理的。"


def _lottery_url(result: dict[str, Any]) -> str | None:
    """發票結果頁的「統一發票對獎」連到這張發票的那一期;讀不到日期就連到對獎頁預設的那一期。

    不是發票,或 QR 記載有買方統編(打統編的發票不能兌獎)回 None。
    """
    if result.get("doc_type") != "發票" or qr_buyer_has_tax_id(result):
        return None
    try:
        return f"/lottery?period={period_of(date.fromisoformat(str(result.get('date') or '')))}"
    except ValueError:
        return "/lottery"


_DUPLICATE_REASONS = {SAME_FILE: "上傳的檔案一模一樣", SAME_INVOICE: "發票號碼一樣（兩份都經過 QR Code 核對）"}


def duplicate_view(dup: dict[str, Any] | None) -> dict[str, str] | None:
    """「這張可能已經上傳過」提示框的文字(src.duplicates.find_duplicate 的結果);另一份比較早寫「之前」,比較晚寫「之後」。"""
    if not dup:
        return None
    day = fmt_short_date(str(dup.get("created_at") or "")[:10])
    reason = _DUPLICATE_REASONS[dup["reason"]]
    if dup["earlier"]:
        text, link = f"{day}上傳的那一份，{reason}。", "看之前那一份"
    else:
        text, link = f"{day}又上傳了一份，{reason}。", "看之後那一份"
    return {"text": text, "link": link, "href": f"/doc/{int(dup['id'])}"}


LISTED = "列在首頁「要記得的事」"       # 「處理狀態」的提醒列:生效中的提醒在首頁一直列到完成或取消(reminder_rows)
UNLISTED = "首頁「要記得的事」不再列出"


def doc_lead(doc: dict[str, Any]) -> str:
    """結果頁第一屏的標題,回答「這是什麼」:「這是電費帳單」(doc_heading);讀成不支援的類型說「這是其他類型的文件」。"""
    heading = doc_heading(doc)
    return "這是其他類型的文件" if heading == "其他" else f"這是{heading}"


def _marked_by(event: dict[str, Any] | None, names: dict[str, str]) -> str:
    """「媽媽 11/4 標示」:誰、哪天按了「已繳費/已辦理」;舊資料沒有操作紀錄就不寫。"""
    try:
        when = datetime.fromisoformat(str((event or {}).get("created_at")))
    except ValueError:
        return ""
    return f"{_who((event or {}).get('actor'), names)} {when.month}/{when.day} 標示"


def _family_row(key: str, label: str, status: dict[str, Any]) -> dict[str, Any]:
    """要家人確認的事(期限提醒、服藥時間表)那一列:等的時候寫等了幾天,只有家人有連到「家人確認」那一件的入口(confirm 是行動編號);
    確認後列在「要記得的事」。退回的不寫「不再列出」:另一個人首頁還會有一列退回通知(rejected_rows)。"""
    row: dict[str, Any] = {"key": key, "label": label, "text": "已退回", "class": "", "sub": ""}
    if status["status"] == "pending":
        row.update({"text": status["waiting"] or "等待家人確認", "class": "wait", "sub": "", "confirm": status["id"]})
    elif status["status"] == "done":
        row.update(text="家人已確認", sub=LISTED, remind=True)
    return row


def status_rows(doc: dict[str, Any], answer: dict[str, Any] | None, *, events: list[dict[str, Any]],
                names: dict[str, str]) -> list[dict[str, Any]]:
    """結果頁「處理狀態」(使用者 11/4):文件收好了沒(已存檔、等待複核)和事情辦了沒(已繳費、已辦理)、提醒還在不在,
    分成不同列,免得以為存檔就代表事情辦好了。

    answer 是 answer_view 的結果(主要行動在 answer["status"]);events 是這份文件「標示已繳費/已辦理」的操作紀錄
    (新的在前),names 是登入 email → 稱呼。每列 {key, label, text, class, sub},再加上這一列的按鈕:done(已繳費)、
    undo(改回未繳)、toggle(取消/恢復提醒)、remind(連到「要記得的事」)、confirm(等家人確認;只有家人看得到入口)。
    要不要畫按鈕由樣板依權限決定。只寫系統真的做到的:提醒只列在首頁,不會傳訊息,所以沒有「已通知」(原則 7)。
    """
    text, cls, _ = _decision_state(doc)
    filed = "收在文件櫃，" if doc.get("action") == "archive" else ""   # 畫面上就說清楚已存檔是什麼意思
    rows: list[dict[str, Any]] = [{"key": "doc", "label": "文件", "text": text, "class": "wait" if cls == "wait" else "",
                                   "sub": f"{filed}{fmt_when(doc.get('created_at'))} 上傳"}]
    st = (answer or {}).get("status")
    if not st:
        return rows
    if st["kind"] == MEDICATION_SCHEDULE:
        rows.append(_family_row("medication", "服藥時間表", st))
        return rows
    if st["kind"] != CALENDAR:
        return rows
    result = doc.get("result") or {}
    button = done_words(result)[0]
    noun = {"帳單": "繳費", "發票": "領獎"}.get(result.get("doc_type") or "", "辦理")
    if st["status"] == "completed":
        event = next((e for e in events if e.get("action_id") == st["id"]), None)
        rows.append({"key": "done", "label": noun, "text": button, "class": "", "sub": _marked_by(event, names),
                     "undo": st["undo"]})
        return rows
    if st["status"] in (("pending", "done") if st["tier"] == "auto" else ("done",)):
        # 生效中的才能標示(同 POST /reminder/{id}/done 的規則):等家人確認、已取消的按了會被擋,就不放按鈕
        rows.append({"key": "done", "label": noun, "text": f"還沒標示{button}", "class": "", "sub": "",
                     "done": {"id": st["id"], "label": button}})
    if st["tier"] == "auto":
        cancelled = st["status"] == "rejected"
        rows.append({"key": "remind", "label": "提醒", "text": "已取消提醒" if cancelled else "已列入提醒", "class": "",
                     "sub": UNLISTED if cancelled else LISTED, "remind": not cancelled, "toggle": st["toggle"]})
    else:
        rows.append(_family_row("remind", "提醒", st))
    return rows


def _status_note(rows: list[dict[str, Any]], result: dict[str, Any]) -> str:
    """「處理狀態」標題旁的「說明」:存檔不等於事情辦好了、按「已繳費」只是記錄、提醒只在網頁上(都是系統真的做的)。"""
    keys = {r["key"] for r in rows}
    button = done_words(result)[0]
    parts = ["「等待複核」是讀到的內容還要家人對照原件確認。"] if rows and rows[0]["class"] == "wait" else []
    if "done" in keys:
        parts.append(f"「已存檔」只代表文件收進文件櫃，不代表{button}。按「{button}」只是記錄，讓家人知道；"
                     f"系統不會{'付款' if result.get('doc_type') == '帳單' else '替您辦理'}，也不會查核。")
    else:
        parts.append("「已存檔」只代表文件收進文件櫃，不代表事情已經辦好。")
    if keys & {"remind", "medication"}:
        parts.append("提醒只列在首頁「要記得的事」，不會傳訊息或響鈴。")
    return "".join(parts)


def doc_view(doc: dict[str, Any], *, file_url: str | None, hint: str | None = None,
             actions: list[dict[str, Any]] | None = None,
             file_size: tuple[int, int] | None = None,
             correct_url: str | None = None, corrected: bool = False,
             retry_url: str | None = None, duplicate: dict[str, Any] | None = None,
             prepared_url: str | None = None, retype_url: str | None = None,
             original: dict[str, Any] | None = None, records: list[dict[str, Any]] | None = None,
             events: list[dict[str, Any]] | None = None, names: dict[str, str] | None = None,
             today: date | None = None) -> dict[str, Any]:
    """結果頁的畫面資料。hint 是上傳時使用者選的類型(只用來決定是否顯示藥袋聲明);
    actions 是這份文件的 action_view 清單,用來組第一屏與「處理狀態」上的提醒/家人確認狀態。
    correct_url:「更正讀值」的連結(不能更正的文件是 None);corrected:家人更正過這份文件的讀值。
    retype_url:「類型不對?」的連結(只有能更正的人看得到;原件不在就不能重讀,是 None)。
    retry_url:模型沒回應而沒讀成時「重新辨識」送去哪裡(看的人不能上傳就是 None,只寫說明)。
    duplicate:可能是同一張的另一份(src.duplicates.find_duplicate;沒有是 None),第一屏下面提示,不自動刪。
    prepared_url:「系統處理後送給 AI 的影像」(原件不在就是 None)。
    original:家人更正之前 AI 讀到的值(ai_reading;沒更正過是 None),「讀到的內容」標出家人改過的欄位。
    records 是這份文件的原始行動(Store.list_actions,看等家人確認等了幾天);events 是這份文件「標示已繳費/已辦理」的
    操作紀錄、names 是登入 email → 稱呼(「處理狀態」寫誰、哪天標示的);today 由呼叫端給,測試才能固定今天。"""
    result = doc.get("result") or {}
    today = today or date.today()
    state_text, state_class, state_icon = _decision_state(doc)
    summary = (result.get("plain_summary") or "").strip()
    items = med_items(result)
    verification = verification_view(result)
    category = doc_category(doc)
    # 處理中(排隊或正在讀):和「還沒讀」一樣說照片收好了、不用重拍,但還不能按「重新辨識」(讀完就會更新)
    processing = doc.get("action") == PROCESSING
    failed = doc.get("action") == "failed" or not result
    # 等家人確認的事寫等了幾天(waiting_text 要原始行動的建立時間);只加在畫面資料上,不改 action_view
    raw = {r.get("id"): r for r in records or []}
    views = [dict(a, waiting=waiting_text(raw[a["id"]], today)) if a.get("state_class") == "wait" and a.get("id") in raw
             else a for a in actions or []]
    answer = answer_view(result, views, verification, today) if not failed else None
    status = status_rows(doc, answer, events=events or [], names=names or {}) if not failed else []
    # 不放 doc 的原始 reason:那是「驗證信心 0.10 低於門檻 0.80」這類工程用語,放進畫面資料容易被誤用;
    # 轉人工的原因一律用 review_reason 的白話句子
    return {
        "title": doc_title(doc),
        "heading": doc_heading(doc),
        "lead": doc_lead(doc),                      # 第一屏的標題「這是電費帳單」
        "category": category,                       # 麵包屑「首頁 / 醫療與保險 / 藥袋」,連到文件櫃的這一類
        "category_href": cabinet_href(category),
        "answer": answer,
        "status": status,                           # 「處理狀態」(status_rows)
        "status_note": _status_note(status, result) if status else "",
        "when": fmt_when(doc.get("created_at")),
        "review_reason": doc_review_reason(doc) if doc.get("action") == "review" else "",
        "failed": failed,
        "busy": doc_busy(doc) or processing,
        "processing": processing,
        "retry_url": None if processing else retry_url,
        "duplicate": duplicate_view(duplicate),
        "lottery_url": _lottery_url(result) if doc.get("action") != "failed" else None,
        "state_text": state_text, "state_class": state_class, "state_icon": state_icon,
        "summary": summary or (fallback_summary(result) if result else ""),
        "summary_is_ai": bool(summary),
        "summary_note": _summary_note(bool(summary), corrected),
        "correct_url": correct_url, "retype_url": retype_url,
        "fields": field_rows(result, original) if result else [],
        "med_items": items,
        "is_medication": result.get("doc_type") == "藥袋" or hint == "藥袋" or bool(items),
        "verification": verification,
        "file_url": file_url,
        "file_size": file_size,   # (寬, 高):<img> 先佔位,延遲載入時版面不跳
        "is_pdf": is_pdf(doc.get("target_path")),
        "prepared_url": prepared_url,
        "source_model": result.get("source_model") or "",
    }


_REVIEW_TILES = {"low": "manual", "mid": "wait"}   # 驗證信心的狀態牌:低於五成紅牌、五到八成黃牌,其他外框牌


def review_row(doc: dict[str, Any]) -> dict[str, Any]:
    """待複核清單的一列(讀 SQLite,每份連到更正頁):原因用白話;可信度只列依核對算的驗證信心(原則 1)。
    tile 是列尾狀態牌的樣式(.tile--*,v5):顏色和原本的 .conf 一樣看 conf_class,沒有數字是灰的外框牌。"""
    result = doc.get("result") or {}
    vc = result.get("verified_confidence")
    confidence = vc if _is_number(vc) else None
    return {
        "href": f"/doc/{doc['id']}/correct",
        "title": doc_title(doc),
        "icon": doc_icon(doc),
        "reason": doc_review_reason(doc) if result else (doc.get("reason") or "（沒有說明）"),
        "when": fmt_when(doc.get("created_at")),
        "confidence": confidence,
        "tile": "auto" if confidence is None else _REVIEW_TILES.get(conf_class(confidence), "plain"),
    }


# ---- 文件四大類:文件櫃、家人指定類別 ---------------------------------------------------
# 大類只用來整理與瀏覽;擷取、核對與行動仍只看文件類型。上傳 11/4 起只選類型(UPLOAD_TYPES),
# 文件依讀到的類型歸類,家人可在更正頁改。大類的值一律來自封閉列舉(src.models.CATEGORIES 與「未分類」),
# 表單送來的其他值當作沒選。

# 大類的圖示(文件櫃卡片、更正頁的類別選項)
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
# 文件櫃卡片上的例子:說明這一類放什麼,不代表都能自動判讀
CATEGORY_EXAMPLES: dict[str, str] = {"身分證明": "身分證、健保卡、戶口名簿", "財產資產": "發票、收據、稅單",
                                     "醫療與保險": "藥袋、保單、醫療收據", "生活契約": "水電瓦斯、電信、租約"}


def type_choices() -> list[dict[str, str]]:
    """首頁上傳的類型選項,照畫面順序(UPLOAD_TYPES);伺服器只收這些值(kind_choice)。"""
    return [{"value": value, "label": label} for value, label in UPLOAD_TYPES]


def upload_label(name: str | None) -> str:
    """上傳時選的類型或文件名稱 → 畫面上的名稱(進度頁「收到照片：發票／收據」);沒選是空字串。"""
    return dict(UPLOAD_TYPES).get(name, name) if name else ""


def kind_choice(category: str | None, value: Any) -> str | None:
    """上傳送來的類型 → 白名單上的值;不在白名單(含「不確定」)回 None,當作沒選。

    沒選大類只收類型選項的值,舊版的「發票」當作「發票／收據」。大類欄位是舊版首頁(先選大類)的表單才會送的,
    還開著舊頁面的手機照舊收得下:選了四大類之一只收那一類清單上的名稱(稅單、保單…)。回傳的是清單裡的字串,不是表單原文。
    """
    if category in CATEGORIES:
        entry = catalog_entry(category, value)
        return entry[0] if entry else None
    if isinstance(value, str):
        value = _MERGED_TYPES.get(value, value)
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
    return "這一類還沒有文件。"     # 上傳不選大類(使用者 11/4):依讀到的類型歸類,家人可在更正頁指定


SEARCH_MAX = 50      # 搜尋最多幾個字
SEARCH_WORDS = 5     # 最多比幾個詞


def _fold(text: str) -> str:
    """比對用:全形轉半形(NFKC)、不分大小寫。"""
    return unicodedata.normalize("NFKC", text).casefold()


def search_text(doc: dict[str, Any]) -> str:
    """一份文件找得到的字:標題、單位、號碼、日期、金額、白話解說、讀到的欄位與家人選的名稱(已正規化)。"""
    result = doc.get("result") or {}
    parts: list[Any] = [doc_title(doc), doc_heading(doc), doc_label(doc), result.get("vendor"), result.get("date"),
                        result.get("invoice_number"), result.get("amount"), result.get("plain_summary")]

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif not _is_empty(value) and not isinstance(value, bool):
            parts.append(value)

    walk(result.get("fields") or {})
    return _fold(" ".join(str(p) for p in parts if not _is_empty(p)))


def search_documents(docs: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """每個詞都要出現、順序不拘(參考 Paperless);只在已讀到的資料裡找,不呼叫模型。沒有詞回空清單。"""
    words = _fold(query[:SEARCH_MAX]).split()[:SEARCH_WORDS]
    return [doc for doc in docs if words and all(word in search_text(doc) for word in words)]


def cabinet_view(docs: list[dict[str, Any]], selected: str | None = None, *, cloud: bool = False,
                 query: str = "") -> dict[str, Any]:
    """文件櫃的畫面資料。docs 是 Store.list_documents(limit=None)(新的在前);selected 是網址的 ?cat=;
    query 是 ?q= 的搜尋字(選了一類就只在那一類裡找),有就只回 found(找到的文件列)。

    cards:四大類卡片(圖示、名稱、例子、份數;選中的 current)。sections:選了一類(四大類或未分類)只列那一類;
    沒選或不認得的值,依大類分組列出全部,「未分類」組放最後(舊資料沒有類別也算),空的組不列。
    每一列沿用首頁「最近看過的文件」(doc_row)。
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
    query = query[:SEARCH_MAX].strip()
    found = [doc_row(d) for d in search_documents(groups[selected] if selected else docs, query)] if query else []
    return {"cards": cards, "sections": sections, "query": query, "found": found, "selected": selected,
            "query_max": SEARCH_MAX, "clear_href": cabinet_href(selected) if selected else "/cabinet"}


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
    "公文": ("vendor", "date", "fields.received_date", "fields.subject", "fields.deadline_text", "fields.doc_number",
             "fields.required_actions"),
    "藥袋": ("vendor", "date", "fields.items"),
    "發票": ("date", "vendor", "amount", "currency", "invoice_number"),
    "收據": ("date", "vendor", "amount", "currency", "invoice_number"),
}
_FORM_DEFAULT_KEYS = ("date", "vendor", "amount", "currency", "invoice_number")   # 不支援的類型:共通欄位
# 表單上和結果頁不同的標籤:帳單金額寫明單位;幣別在 web/fields.py 沒有中文名
_FORM_LABELS = {("帳單", "amount"): "應繳金額（元）", ("", "currency"): "幣別"}
# 輸入方式:choice 下拉、lines 多行(一行一項)、currency 幣別代碼;其餘照 FieldSpec.kind(date/amount/text),
# 統一發票的號碼另外是 invoice(_form_kind)
_FORM_KINDS = {"fields.bill_kind": "choice", "fields.required_actions": "lines", "currency": "currency"}
_NO_SPELLCHECK = {"invoice_number", "currency", "fields.doc_number"}   # 代碼類欄位不做拼字檢查

MSG_REQUIRED = "這一欄必填，請對照原件填上。"
MSG_DATE = "請填正確的日期，例如 2026-10-20。"
MSG_AMOUNT = "請輸入數字，例如 1286。"
MSG_AMOUNT_POSITIVE = "金額要大於 0。"
MSG_DAYS = "請輸入天數的數字，例如 7。"
MSG_CURRENCY = "請填 3 個英文字母，台幣填 NTD。"
MSG_INVOICE = "請輸入 2 個英文字母加 8 位數字，例如 AB12345678；不是台灣的統一發票就空著。"
MSG_TOO_LONG = f"太長了，最多 {MAX_FORM_TEXT} 個字。"
MSG_RECEIVED_FUTURE = "收到日期不能晚於今天。"
MSG_RECEIVED_EARLY = "收到日期不能早於發文日期。"
SAVE_CORRECTION = "儲存更正並重新核對"   # 更正頁唯一的主要按鈕;最後一個問題的「下一步」也寫這幾個字
RETYPE_CHOOSE = "請選一個類型。"          # 改類型頁離開欄位時的說明;同 web/app.py 的 MSG_RETYPE_CHOOSE(伺服器回的那一句)

# 填寫要求(使用者 11/4 的表單規範):help 是框下常駐的灰字,沒有格式要求的欄位不寫;pattern 與 check 給 app.js
# 離開欄位時檢查(data-error,寫清楚怎麼改)。pattern 要和 _parse_value 收的寫法一致(全形數字、千分位逗號都算對),
# 只是早一點告訴家人,伺服器照樣再檢查一次
_FORM_RULES: dict[str, dict[str, str]] = {
    "amount": {"help": "只寫數字", "pattern": r"\s*[0-9０-９][0-9０-９,，\s]*([.．][0-9０-９]+)?\s*", "check": MSG_AMOUNT},
    "number": {"help": "只寫數字", "pattern": r"\s*[0-9０-９]+\s*", "check": MSG_DAYS},
    "currency": {"help": "3 個英文字母，台幣填 NTD", "pattern": r"\s*[A-Za-z]{3}\s*", "check": MSG_CURRENCY},
    "invoice": {"help": "2 個英文字母加 8 位數字",
                "pattern": r"\s*[A-Za-zＡ-Ｚａ-ｚ]{2}[\s\-－]*[0-9０-９]{8}\s*", "check": MSG_INVOICE},
    "lines": {"help": "一行寫一項"},
}

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_ITEM_INPUT = re.compile(r"items-(\d{1,3})-(?:name|usage|timing|days)")


def _form_keys(doc_type: str) -> tuple[str, ...]:
    return _FORM_KEYS.get(doc_type, _FORM_DEFAULT_KEYS)


def _form_kind(doc_type: str, key: str) -> str:
    if doc_type == "發票" and key == "invoice_number":
        return "invoice"       # 只有統一發票有固定格式;收據的號碼各家寫法不同
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
    return "，".join(_plain(raw.get(k)) for k in ("dose_text", "frequency_text") if not _is_empty(raw.get(k)))


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
    if kind == "invoice":      # 格式同核對的「字軌格式」;全形字母與數字先轉半形,連字號與空白由核對處理
        plain = unicodedata.normalize("NFKC", text)
        return (plain, "") if check_invoice_number_format(plain)["status"] == "pass" else (None, MSG_INVOICE)
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
    received = changes.get("fields.received_date")
    if received:        # 都是表單轉好的 YYYY-MM-DD,可以直接比
        if received > date.today().isoformat():
            errors["fields.received_date"] = MSG_RECEIVED_FUTURE
        elif changes.get("date") and received < changes["date"]:
            errors["fields.received_date"] = MSG_RECEIVED_EARLY
    return changes, values, errors


def _input_view(name: str, input_id: str, label: str, value: Any, *, kind: str = "text", required: bool = False,
                error: str = "", spellcheck: bool = True) -> dict[str, Any]:
    """表單的一個輸入欄(樣板 correct.html 的 field 巨集用)。issues、next 只有讀值欄位會填(correct_view)。

    help、pattern、check 是填寫要求(_FORM_RULES);有格式的 check 寫格式,必填的寫必填,都沒有就不檢查。
    """
    rule = _FORM_RULES.get(kind, {})
    return {"name": name, "id": input_id, "label": label, "value": value, "kind": kind, "required": required,
            "error": error, "spellcheck": spellcheck and kind not in ("date", "amount"), "issues": [], "next": None,
            "help": rule.get("help", ""), "pattern": rule.get("pattern", ""),
            "check": rule.get("check") or (MSG_REQUIRED if required else "")}


def _item_view(j: int, entry: dict[str, Any], errors: dict[str, str]) -> dict[str, Any]:
    prefix, base = f"items-{j}-", f"m{j}-"
    return {
        "legend": f"第 {j + 1} 種藥",
        "name": _input_view(prefix + "name", base + "name", "藥名", entry.get("name", ""), required=True,
                            error=errors.get(prefix + "name", "")),
        "usage": _input_view(prefix + "usage", base + "usage", "用法（照藥袋上的字）", entry.get("usage", ""),
                             error=errors.get(prefix + "usage", "")),
        "timing_name": prefix + "timing",
        "timing": entry.get("timing") or [],
        "days": _input_view(prefix + "days", base + "days", "天數", entry.get("days", ""), kind="number",
                            error=errors.get(prefix + "days", ""), spellcheck=False),
    }


# 「改成機讀碼上的值」那一句的欄位說法:只列更正表單上有的欄位(發票的統編、隨機碼不在表單上);
# 帳單條碼只有應繳金額
_QR_FIX_LABELS = {"amount": "總計", "date": "開立日期", "invoice_number": "發票號碼"}
_BARCODE_FIX_LABELS = {"amount": "應繳金額"}


def evidence_fixes(result: dict[str, Any]) -> dict[str, dict[str, str]]:
    """與 QR 或帳單條碼矛盾的欄位 → 機讀碼上的值,給更正頁「改成這個」:{欄位: {value, shown, lead}}。

    value 是填進表單的字、shown 是畫面上的寫法、lead 是那一句的開頭。只取獨立證據的檢查附上的
    expected(src/verify);值一律當資料(截短,畫面上跳脫)。按了只填進欄位,存檔後一樣重新核對。
    """
    fixes: dict[str, dict[str, str]] = {}
    for name, check in _checks(result.get("verification")):
        expected = check.get("expected")
        if check.get("status") != "fail" or name not in INDEPENDENT_CHECKS or not isinstance(expected, dict):
            continue
        barcode = name == CHECK_BARCODE_AMOUNT
        labels = _BARCODE_FIX_LABELS if barcode else _QR_FIX_LABELS
        for key, raw in expected.items():
            if key in fixes or key not in labels or _is_empty(raw) or (key == "amount" and not _is_number(raw)):
                continue
            value = _form_text(raw)[:40]
            shown = fmt_amount(raw) if key == "amount" else fmt_date(value) if key == "date" else value
            source = "帳單上的超商條碼記的" if barcode else "發票上的 QR Code 記的"
            fixes[key] = {"value": value, "shown": shown, "lead": f"{source}{labels[key]}是"}
    return fixes


def _fix_for(fix: dict[str, str] | None, current: Any, key: str) -> dict[str, str] | None:
    """欄位現在填的已經是機讀碼上的值(家人改好了、表單重畫)就不再提;current 是表單上的字。"""
    if fix is None:
        return None
    text = str(current).strip()
    same = text == fix["value"]
    if key == "amount" and text and not same:
        amount, _ = _parse_amount(text)
        same = amount is not None and abs(amount - float(fix["value"])) < 0.005
    if same:
        return None
    if not text:
        now = "空白"
    else:
        now = f"{text[:40]} 元" if key == "amount" else fmt_date(text[:40]) if key == "date" else text[:40]
    return dict(fix, current=now)


# 更正頁每一欄「為什麼要看」(參考 Rossum 先看沒打勾的欄位,11/4):fail 是紅字(和機讀碼不符、檢查沒通過),
# wait 是琥珀(讀不清楚、沒有讀到)。顏色只是第二重提示,原因一律寫成字
ISSUE_CONFLICT_QR = "和發票上的 QR Code 不符"
ISSUE_CONFLICT_BARCODE = "和帳單上的超商條碼不符"
ISSUE_UNREADABLE = "AI 讀不清楚"
ISSUE_MISSING = "AI 沒有讀到"
ISSUE_CHECK = "檢查沒通過"


def correction_issues(result: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """更正表單上的欄位 → 為什麼要看:[{text, tone}],沒有問題的欄位不列。

    原因只取讀值的核對結果(verification)與讀不清的欄位(unreadable),不看 AI 自評的信心(原則 1)。
    順序:和 QR Code 或超商條碼不符 → 讀不清楚 → 沒有讀到(必填欄位) → 檢查沒通過(附程式寫的說明)。
    都是存好的讀值的狀態:家人在表單上改了、還沒存檔前不會變,存檔重新核對後才換
    (例外在 correct_view:表單重畫時已經改成機讀碼上的值,就不再寫不符)。
    只對到這張表單上的欄位;核對到表單以外的欄位(例如發票的統編)由頁首的待複核原因說明。
    """
    doc_type = result.get("doc_type") or "其他"
    keys = _form_keys(doc_type)
    failed: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for name, check in _checks(result.get("verification")):
        if check.get("status") != "fail":
            continue
        named = {f for f in check.get("fields") or [] if isinstance(f, str)}
        for key in keys:
            if not named.isdisjoint(_field_aliases(key)):
                failed.setdefault(key, []).append((name, check))
    found: dict[str, list[dict[str, str]]] = {}
    for key in keys:
        issues: list[dict[str, str]] = []
        checks = failed.get(key, [])
        for name, _ in checks:
            if name in INDEPENDENT_CHECKS:
                issues.append({"text": ISSUE_CONFLICT_BARCODE if name == CHECK_BARCODE_AMOUNT else ISSUE_CONFLICT_QR,
                               "tone": "fail"})
        if _is_unreadable(key, result.get("unreadable")):
            issues.append({"text": ISSUE_UNREADABLE, "tone": "wait"})
        elif field_spec(doc_type, key).required and _is_empty(_field_value(result, key)):
            issues.append({"text": ISSUE_MISSING, "tone": "wait"})
        for name, check in checks:
            if name not in INDEPENDENT_CHECKS:
                detail = _plain(check.get("detail") or "")
                issues.append({"text": f"{ISSUE_CHECK}：{detail}" if detail else f"{ISSUE_CHECK}：{name}",
                               "tone": "fail"})
        # 同一句只寫一次(例如 QR 的總計與金額兩項都不符)
        unique = list({issue["text"]: issue for issue in issues}.values())
        if unique:
            found[key] = unique
    return found


def correct_view(doc: dict[str, Any], values: dict[str, Any] | None = None,
                 errors: dict[str, str] | None = None, *, file_url: str | None,
                 file_size: tuple[int, int] | None = None, add_item: bool = False) -> dict[str, Any]:
    """更正頁的畫面資料。values 是表單值(沒給就從讀值取);errors 是欄位 → 說明;
    add_item:剛按了「再加一種藥」,藥品多一種空白的。

    有問題的欄位排前面(problems,每一欄寫出原因,見 correction_issues),沒問題的排在後面(others);
    藥品是一整塊(unit 是 "meds")。有 JS 時有問題的欄位一次只展開一個,其他收成一行摘要(short 加原因,使用者 11/5);
    每個有問題的欄位有「下一個問題」連到下一欄,最後一欄連到儲存按鈕。
    焦點:驗證失敗時第一個要改的欄位(依畫面順序);剛加的那一種藥的藥名;都沒有就是第一個有問題的欄位
    (focus 是輸入欄的 id)。
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
    units: list[dict[str, Any]] = []      # 依表單順序:讀值欄位,藥袋另有一整塊藥品
    fixes = evidence_fixes(result)
    issues = correction_issues(result)
    for key in _form_keys(doc_type):
        if key == "fields.items":
            entries = list(values.get("items") or [_blank_item()])
            if add_item and len(entries) < MAX_MED_ITEMS:
                entries.append(_blank_item())
            items = [_item_view(j, entry, errors) for j, entry in enumerate(entries)]
            units.append({"unit": "meds", "id": "items", "label": "藥品", "short": "藥品", "anchor": "p-items",
                          "issues": issues.get(key, []), "focus_id": items[0]["name"]["id"], "next": None})
            continue
        kind = _form_kind(doc_type, key)
        input_id = "f-" + key.replace(".", "-")
        value = values.get(key, "")
        fix = _fix_for(fixes.get(key), value, key)
        field_issues = issues.get(key, [])
        if key in fixes and fix is None:   # 表單重畫時已經是機讀碼上的值:再寫「不符」就不是事實,其他原因照舊(原則 7)
            field_issues = [i for i in field_issues if i["text"] not in (ISSUE_CONFLICT_QR, ISSUE_CONFLICT_BARCODE)]
        field = {
            **_input_view(key, input_id, _form_label(doc_type, key), value,
                          kind=kind, required=field_spec(doc_type, key).required,
                          error=errors.get(key, ""), spellcheck=key not in _NO_SPELLCHECK),
            "unit": "field",
            "short": _change_label(doc_type, key),
            "options": BILL_KINDS if kind == "choice" else (),
            "fix": fix,
            "issues": field_issues,
            "anchor": "p-" + key.replace(".", "-"),
            "focus_id": input_id,
        }
        fields.append(field)
        units.append(field)
    problems = [u for u in units if u["issues"]]
    for at, unit in enumerate(problems):
        after = problems[at + 1] if at + 1 < len(problems) else None
        unit["next"] = ({"href": f"#{after['anchor']}", "focus": after["focus_id"],
                         "text": f"下一個問題：{after['short']}"} if after else
                        {"href": "#save", "focus": "save", "text": f"下一步：{SAVE_CORRECTION}"})
    others = [u for u in units if not u["issues"]]
    medicines = [f for it in items or [] for f in (it["name"], it["usage"], it["days"])]
    inputs = [f for u in problems + others for f in (medicines if u["unit"] == "meds" else [u])]   # 依畫面順序
    focus = next((f["id"] for f in inputs if f["error"]), None)
    if focus is None and add_item and items:
        focus = items[-1]["name"]["id"]
    if focus is None and problems:
        focus = problems[0]["focus_id"]
    review = doc.get("action") == "review"
    return {
        "id": doc["id"],
        "title": doc_title(doc),
        "heading": doc_heading(doc),
        "review_reason": doc_review_reason(doc) if review else "",   # 和待複核清單、結果頁同一句
        "fields": fields,
        "meds": items,   # 藥品(只有藥袋有;鍵不叫 items,Jinja 的 v.items 會先抓到 dict 的方法)
        "problems": problems,
        "others": others,
        "category": category,
        "category_choices": ASSIGNABLE_CATEGORIES,
        "timing_choices": MED_TIMING_CHOICES,
        "can_add_item": items is not None and len(items) < MAX_MED_ITEMS,
        "error_count": len(errors),
        "focus": focus,
        "save_label": SAVE_CORRECTION,
        "can_reject": review,
        "reject_ask": REJECT_DOC_ASK,   # 退回前先問的話(確認框與沒有 JS 時的確認頁同一份)
        "file_url": file_url,
        "file_size": file_size,
        "is_pdf": is_pdf(doc.get("target_path")),
    }


# ---- 更正存好之後:結果頁上方「改了什麼」 ----------------------------------------------------
# 轉回結果頁的網址帶 ?changed=,值是逗號分開的鍵:這個類型的表單欄位(_form_keys)、類別,以及舊的提醒或
# 服藥時間表換掉了沒有。結果頁只認這份固定清單、換成固定的中文,網址上的其他字一律不顯示(同 ?msg= 的白名單)
CHANGED_CATEGORY = "category"
CHANGED_REPLACED = "replaced"    # 舊的提醒(或服藥時間表)被取代,更正後有新的
CHANGED_DROPPED = "dropped"      # 舊的被取代,更正後沒有新的(例如發票上家人從對獎頁加的領獎提醒)
_CHANGED_MAX = 300               # ?changed= 最多看幾個字:鍵都是短的英文,更長的是亂塞的


def _as_form(values: dict[str, Any]) -> FormData:
    """表單的初始值 → 什麼都不改、原樣送出時的表單(藥品照 items-<序號>-… 的欄名,時段可以多選)。"""
    pairs = [(key, value) for key, value in values.items() if isinstance(value, str)]
    for j, item in enumerate(values.get("items") or []):
        pairs += [(f"items-{j}-name", item["name"]), (f"items-{j}-usage", item["usage"]),
                  (f"items-{j}-days", item["days"])] + [(f"items-{j}-timing", t) for t in item["timing"]]
    return FormData(pairs)


def changed_fields(result: dict[str, Any], changes: dict[str, Any]) -> list[str]:
    """這次更正真的改了的表單欄位(依表單順序)。changes 是 parse_correction 轉好的值。

    和「什麼都不改、原樣送出」時的值比:表單本身的整理(1286.0 寫成 1286、時段的寫法、沒有幣別補 NTD)不算改;
    沒送出的欄位(例如帳單種類一個都沒選)維持原讀值,也不算。
    """
    untouched, _, _ = parse_correction(result, _as_form(correction_values(result)))
    return [key for key in _form_keys(result.get("doc_type") or "其他") if key in changes
            and (key not in untouched or _comparable(changes[key]) != _comparable(untouched[key]))]


def change_param(doc: dict[str, Any], changes: dict[str, Any], category: Any, *, before: int, after: int) -> str:
    """更正存好之後,轉回結果頁的 ?changed= 要帶的鍵(逗號分開;什麼都沒改是空字串)。

    doc 是更正之前的文件;category 是表單送來的類別(不在選項內就是沒改);before、after 是更正前後還有效的
    提醒與服藥時間表各幾筆:更正會把舊的全部換掉(Store.replace_actions),之前有才寫「舊提醒已取代」。
    """
    keys = changed_fields(doc.get("result") or {}, changes)
    if category in ASSIGNABLE_CATEGORIES and category != doc_category(doc):
        keys.append(CHANGED_CATEGORY)
    if before:
        keys.append(CHANGED_REPLACED if after else CHANGED_DROPPED)
    return ",".join(keys)


def _change_label(doc_type: str, key: str) -> str:
    """「改了什麼」裡的欄位名:和更正表單上一樣,只拿掉單位(「應繳金額（元）」寫成「應繳金額」)。"""
    return re.sub(r"（[^）]*）$", "", _form_label(doc_type, key))


def change_note(result: dict[str, Any] | None, raw: Any) -> str:
    """結果頁上方「改了什麼」,例如「應繳金額、繳費期限已更新，舊提醒已取代」;沒有認得的鍵回空字串。

    raw 是網址上的 ?changed=,只當成固定清單裡的鍵:這個類型的表單欄位、類別、舊的提醒換掉了沒有
    (change_param 產生的);其他字一律略過,網址上的文字不會出現在畫面上。藥袋換掉的是服藥時間表。
    """
    if not isinstance(raw, str) or not isinstance(result, dict):
        return ""
    doc_type = result.get("doc_type") or "其他"
    asked = set(raw[:_CHANGED_MAX].split(","))
    labels = [_change_label(doc_type, key) for key in _form_keys(doc_type) if key in asked]
    if CHANGED_CATEGORY in asked:
        labels.append("類別")
    parts = [f"{'、'.join(labels)}已更新"] if labels else []
    old = "舊的服藥時間表" if doc_type == "藥袋" else "舊提醒"
    if CHANGED_REPLACED in asked:
        parts.append(f"{old}已取代")
    elif CHANGED_DROPPED in asked:
        parts.append(f"{old}不再生效")
    return "，".join(parts)


def retype_view(doc: dict[str, Any], *, file_url: str | None, file_size: tuple[int, int] | None = None,
                error: str = "") -> dict[str, Any]:
    """「改文件類型」頁的畫面資料。選項是固定清單(DOC_TYPES)裡現在以外的類型,文件文字影響不了。"""
    current = (doc.get("result") or {}).get("doc_type") or "其他"
    return {"id": doc["id"], "title": doc_title(doc), "heading": doc_heading(doc), "current": current,
            "choices": [t for t in DOC_TYPES if t != current], "error": error, "choose_error": RETYPE_CHOOSE,
            "file_url": file_url, "file_size": file_size, "is_pdf": is_pdf(doc.get("target_path"))}


# ---- 上傳後的進度頁(UX-3) ------------------------------------------------------------
# 四步和 web/jobs.py 的 STAGES 同順序;伺服器真的做完的那一步才是 done,正在做的是 now,其餘 todo。
# 「AI 照抄文字」寫誰在讀(依上傳當下的辨識模式);排隊時寫前面還有幾張

_PROGRESS_STEPS = (
    ("received", "收到照片", "檢查過格式，也拿掉了拍攝地點"),
    ("read", "AI 照抄文字", "AI 讀字，只照抄、不換算"),
    ("checked", "程式核對", "比對 QR Code 或帳單條碼，檢查日期與金額的格式"),
    ("decided", "決定怎麼處理", "照固定規則：收進文件櫃、列入提醒，或交給家人確認"),
)
_READ_HINTS = {"ollama": "Gemma 4 在這台電腦讀字，只照抄、不換算",
               "workers_ai": "Gemma 4 讀字，只照抄、不換算",
               "mock": "展示模式：使用模擬讀值"}
_STEP_STATE_TEXT = {"done": "（做完了）", "now": "（正在做）", "todo": "（還沒開始）"}
PROGRESS_NOW_SECONDS = 2   # 正在做的那一步超過這麼多秒才寫「已 N 秒」,不會一開頭就閃「已 0 秒」


def _step_seconds(seconds: float) -> str:
    if seconds < 0.05:
        return "不到 0.1 秒"   # 寫「0.0 秒」看起來像沒做
    return f"{seconds:.1f} 秒" if seconds < 10 else f"{seconds:.0f} 秒"


def progress_view(snap: dict[str, Any], *, token: str, provider: str, is_pdf: bool, label: str,
                  has_photo: bool) -> dict[str, Any]:
    """進度頁(與每秒一次的狀態查詢)的畫面資料;snap 是 JobBoard.snapshot。label 是家人選的類型或文件名稱。"""
    seconds = snap.get("seconds") or {}
    current = next((key for key, _, _ in _PROGRESS_STEPS if key not in seconds), None)
    steps = []
    for key, title, hint in _PROGRESS_STEPS:
        if key == "received" and is_pdf:
            title, hint = "收到檔案", "檢查過格式"
        elif key == "read":
            hint = _READ_HINTS.get(provider, hint)
        time_text = ""
        if key in seconds:
            state, time_text = "done", _step_seconds(seconds[key])
        elif key == current:
            state = "now"
            if snap.get("queued"):
                ahead = snap.get("ahead") or 0
                hint = f"前面還有 {ahead} 份，輪到這份就開始" if ahead else "馬上開始"
            elif snap.get("elapsed", 0) >= PROGRESS_NOW_SECONDS:
                time_text = f"已 {int(snap['elapsed'])} 秒"
        else:
            state = "todo"
        steps.append({"id": key, "title": title, "hint": hint, "state": state, "time": time_text,
                      "state_text": _STEP_STATE_TEXT[state]})
    now = next((s for s in steps if s["state"] == "now"), None)
    if now is None:
        say = "快好了"
    elif snap.get("queued") and snap.get("ahead"):
        say = f"排隊中，前面還有 {snap['ahead']} 份"
    else:
        say = f"現在：{now['title']}"
    uploaded = "剛上傳的 PDF 檔" if is_pdf else "剛上傳的照片"
    return {
        "token": token,
        "title": "正在讀這份文件" if is_pdf else "正在讀這張文件",
        "photo_text": f"{uploaded}：{label}" if label else uploaded,
        "has_photo": has_photo,
        "steps": steps,
        "say": say,
    }


# ---- 設定頁(SET) -------------------------------------------------------------------
# 四塊:系統狀態(唯讀)、這台裝置的偏好(存在瀏覽器,app.js 讀寫)、會影響全家的系統設定、資料管理。
# 規則(門檻範圍、雲端要金鑰、藥袋一律本機)在 src/settings.py;這裡只把目前的設定排成畫面文字。

PROVIDER_LABELS = {"ollama": "只用這台電腦", "workers_ai": "可以用雲端備援"}   # 選項與變更紀錄共用
_MODE_TEXT = {"ollama": "本機（這台電腦）", "workers_ai": "雲端備援（敏感文件仍在這台電腦）",
              "mock": "展示模式（使用模擬讀值）"}
_MODEL_TEXT = {DEFAULT_OLLAMA_MODEL: "Gemma 4 12B", DEFAULT_WORKERS_AI_MODEL: "Gemma 4 26B"}   # 其他照設定原樣
# 這台裝置的偏好:(name, 標題, ((值, 文字)…), 預設值)。值要和 app.js 的 PREFS、app.css 的 html[data-font] 一致
DEVICE_PREFS: tuple[tuple[str, str, tuple[tuple[str, str], ...], str], ...] = (
    ("font", "字級", (("standard", "標準"), ("large", "大"), ("xlarge", "特大")), "standard"),
    ("rate", "朗讀速度", (("slow", "慢"), ("standard", "標準")), "standard"),
)
CLOUD_CONFIRM_TITLE, CLOUD_CONFIRM_OK = "確定要開啟雲端備援嗎？", "確定開啟"
PURGE_CONFIRM_TITLE, PURGE_CONFIRM_OK = "確定要刪除全部資料嗎？", "確定刪除"
PURGE_CONFIRM = "所有文件的照片、讀值、提醒與更正紀錄都會刪除，無法復原；設定與設定變更紀錄會保留。"

# ---- 要先確認的動作 ----
# 切到雲端備援、刪除全部資料、兩處「退回」,按下前先問一次:標題、說明、確定鍵,加上確認欄位的名稱。
# 有 JS 時用 base.html 的確認框,按了「確定…」app.js 才把確認欄位(值是 "1")補進表單;伺服器沒收到這個欄位
# 就不做(沒有 JS、或 app.js 沒跑起來),改回一頁確認頁(ask.html)問同樣的字
CONFIRM_CLOUD_FIELD, CONFIRM_PURGE_FIELD, CONFIRM_REJECT_FIELD = "confirm_cloud", "understood", "confirm_reject"
CONFIRM_DELETE_FIELD = "confirm_delete"
PURGE_ASK = {"title": PURGE_CONFIRM_TITLE, "message": PURGE_CONFIRM, "ok": PURGE_CONFIRM_OK,
             "field": CONFIRM_PURGE_FIELD}
DELETE_DOC_ASK = {"title": "確定要刪除這份文件嗎？", "ok": "確定刪除", "field": CONFIRM_DELETE_FIELD,
                  "message": "照片、讀到的內容、提醒和這份文件的操作紀錄都會刪掉，不能復原。"}
REJECT_DOC_ASK = {"title": "確定要退回這份文件嗎？", "message": "這份文件會改成「讀不出來」，請長輩重新拍一張。",
                  "ok": "確定退回", "field": CONFIRM_REJECT_FIELD}


def reject_action_ask(kind_label: str) -> dict[str, str]:
    """家人確認頁退回一個事項前要問的話(kind_label:服藥時間表、期限提醒…)。"""
    return {"title": f"確定要退回「{kind_label}」嗎？", "message": "退回後就不會生效。", "ok": "確定退回",
            "field": CONFIRM_REJECT_FIELD}


def upload_routes(local_only: tuple[str, ...] | list[str]) -> tuple[list[str], list[str]]:
    """雲端模式下,上傳的類型選項各在哪裡辨識(畫面文字用):(只在本機的, 會交給雲端的),照選項順序、寫畫面上的名稱。
    分流只看上傳時選的類型(src.providers.routing);「不確定」沒有類型提示,一律在本機,句子另外寫。"""
    named = [(value, label) for value, label in UPLOAD_TYPES if value != UNSURE]
    return ([label for value, label in named if value in local_only],
            [label for value, label in named if value not in local_only])


def local_scope(local_only: tuple[str, ...] | list[str]) -> str:
    """一律只在本機辨識的文件:「藥袋、發票／收據，以及選「不確定」的文件」(設定頁、切到雲端前的確認)。"""
    here = "、".join(upload_routes(local_only)[0])
    return f"{here}，以及選「不確定」的文件" if here else "選「不確定」的文件"


def cloud_privacy(local_only: tuple[str, ...] | list[str]) -> str:
    """雲端模式的隱私說明(每頁的安心說明、首頁上傳區):哪些只在本機、哪些會交給雲端,照實際設定寫。"""
    away = "、".join(upload_routes(local_only)[1])
    if not away:
        return "文件都在這台電腦上辨識，不會送到雲端。"
    return f"{local_scope(local_only)}只在這台電腦上辨識；{away}會交給雲端模型讀取。"


def cloud_ask(local_only: tuple[str, ...] | list[str]) -> dict[str, str]:
    """切到雲端備援前要問的話;哪些文件仍只在這台電腦處理,依目前的設定寫。"""
    away = "、".join(upload_routes(local_only)[1])
    message = f"開啟後，{local_scope(local_only)}仍只在這台電腦處理"
    return {"title": CLOUD_CONFIRM_TITLE, "ok": CLOUD_CONFIRM_OK, "field": CONFIRM_CLOUD_FIELD,
            "message": f"{message}；{away}會交給雲端模型讀取。" if away else f"{message}。"}


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
    names = [f"{_MODEL_TEXT.get(models[key], models[key])}（{where}）"
             for key, where in (("cloud", "雲端"), ("local", "本機")) if models.get(key)]
    return "、".join(names) or "不使用模型（展示模式用模擬讀值）"


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
    if isinstance(key, str) and key.startswith(CONSENT_PREFIX):
        return f"{key.removeprefix(CONSENT_PREFIX)} 看過資料使用說明"
    if key == DOCUMENT_DELETED:
        return f"刪除一份文件（第 {change.get('old_value')} 號）"
    if isinstance(key, str) and key.startswith(LOTTERY_PREFIX):
        return f"更新統一發票對獎號碼（{period_label(key.removeprefix(LOTTERY_PREFIX))}）"
    if isinstance(key, str) and key.startswith("member:"):
        email = key.removeprefix("member:")
        if value == "removed":
            return f"移除家庭成員 {email}"
        return f"更新家庭成員 {email}（{'家人' if value == 'family' else '長輩' if value == 'elder' else value}）"
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
    return [{"value": f"{t:.2f}", "label": f"{fmt_pct(t)}{'（預設）' if t == round(default, 2) else ''}",
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
    scope = local_scope(current.local_only_doc_types)
    status = [
        {"label": "辨識模式", "value": _MODE_TEXT.get(current.provider, current.provider),
         "pill": _model_pill(current.provider, ollama)},
        {"label": "使用的模型", "value": _models_text(models)},
        {"label": "只在本機處理",
         "value": "全部文件（展示模式不會把文件送出這台電腦）" if demo else scope},
        {"label": "自動存檔門檻",
         "value": f"驗證信心 {fmt_pct(current.auto_threshold)} 以上才自動存檔，其餘交給家人複核"},
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
        "cloud_hint": "" if cloud_ready or demo else "這台電腦還沒有設定雲端模型的帳號與金鑰，所以不能選（設定方法見部署指南）。",
        "lockline": f"{scope}一律只在這台電腦處理，這一項不能關。",
        "thresholds": _threshold_options(current.auto_threshold, default_threshold,
                                         None if THRESHOLD in errors else values.get(THRESHOLD)),
        "threshold_error": errors.get(THRESHOLD, ""),       # 寫在門檻的選項正下方
        "errors": [text for key, text in errors.items() if key != THRESHOLD],
        "cloud_confirm": cloud_ask(current.local_only_doc_types),
        "purge_confirm": PURGE_ASK,
        "changes": [{"when": fmt_when(c.get("created_at")), "text": setting_change_text(c),
                     "actor": "這台電腦" if c.get("actor") == "local" else c.get("actor") or ""} for c in changes],
    }


# ---- 統一發票對獎(/lottery) ---------------------------------------------------
# 號碼由家人照公告輸入;只比對號碼經 QR Code 核對相符的發票,其他只算張數。比對規則在 src/lottery.py。

_ICON_PATHS.update({
    "star": '<path d="m12 3 2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z"/>',
})
LOTTERY_FIELDS: tuple[tuple[str, str], ...] = (
    ("special", "特別獎（1,000 萬元）"), ("grand", "特獎（200 萬元）"),
    ("first1", "頭獎（第 1 組）"), ("first2", "頭獎（第 2 組）"), ("first3", "頭獎（第 3 組）"),
)
# 離開欄位時瀏覽器檢查用,和 src.lottery.normalize_number 收的一樣:8 位數字,全形也可以,中間可以有空白與連字號
LOTTERY_NUMBER_PATTERN = r"\s*([0-9０-９][\s\-]*){8}"
LOTTERY_NUMBER_HELP = "8 位數字"
LOTTERY_NUMBER_ERROR = "請輸入公告上的 8 位數字"     # 離開欄位時和送出後都是這一句,同一個問題只說一種話


def lottery_view(period: str, periods: list[tuple[str, int]], invoices: list[dict[str, Any]],
                 numbers: Numbers | None, *, can_edit: bool, can_remind: bool,
                 reminded: frozenset[int] | set[int] = frozenset(),
                 values: dict[str, Any] | None = None, errors: dict[str, str] | None = None) -> dict[str, Any]:
    """對獎頁的畫面資料。

    periods:(期別, 這一期的發票張數),給上面的期別連結;invoices:這一期的發票(Store 的文件)。
    numbers:這一期存好的號碼(還沒輸入是 None);reminded:已經加過領獎提醒的文件編號。
    values/errors:存檔沒過時重畫表單用,留住家人剛填的值、寫出哪一欄不對。文件文字一律當資料顯示。
    """
    errors = errors or {}
    if values is not None:
        shown = {name: str(values.get(name) or "") for name, _ in LOTTERY_FIELDS} | {
            "claim_by": str(values.get("claim_by") or "")}
    elif numbers is not None:
        shown = {"special": numbers.special, "grand": numbers.grand,
                 **{f"first{i}": n for i, n in enumerate(numbers.first, 1)}, "claim_by": numbers.claim_by.isoformat()}
    else:
        shown = {name: "" for name, _ in LOTTERY_FIELDS} | {"claim_by": ""}
    rows, skipped, taxed = [], 0, 0
    for doc in sorted(invoices, key=lambda d: (str((d.get("result") or {}).get("date") or ""), d["id"])):
        result = doc.get("result") or {}
        number = verified_invoice_number(result)
        if number is None:
            skipped += 1
            continue
        buyer = qr_buyer_has_tax_id(result)     # 打統編的發票不能兌獎(src/lottery.py)
        if buyer is not False:
            taxed += buyer is True
            skipped += buyer is None
            continue
        hit = match(number, numbers) if numbers else None
        meta = "・".join(x for x in (_plain(result.get("vendor") or ""), fmt_short_date(result.get("date")),
                                     _amount_text(result)) if x)
        rows.append({"number": f"{number[:2]}-{number[2:]}", "meta": meta, "href": f"/doc/{doc['id']}",
                     "hit": prize_text(hit) if hit else "",
                     "hint": claim_hint(numbers.claim_by) if hit and numbers else "",
                     "remind": ({"doc_id": doc["id"], "done": doc["id"] in reminded}
                                if hit and can_remind else None)})
    return {
        "period": period, "period_label": period_label(period),
        "periods": [{"label": period_label(key), "count": count, "href": f"/lottery?period={key}",
                     "current": key == period} for key, count in periods],
        "fields": [{"name": name, "label": label, "value": shown[name],
                    "error": LOTTERY_NUMBER_ERROR if name in errors else ""} for name, label in LOTTERY_FIELDS],
        # 灰字只寫填寫要求:不能改的人(長輩)不必看
        "number_help": LOTTERY_NUMBER_HELP if can_edit else "", "number_pattern": LOTTERY_NUMBER_PATTERN,
        "number_error": LOTTERY_NUMBER_ERROR,
        "claim_by": shown["claim_by"], "claim_error": errors.get("claim_by", ""), "claim_required": "請照公告選領獎期限的日期",
        "has_numbers": numbers is not None, "can_edit": can_edit,
        "rows": rows, "skipped": skipped, "taxed": taxed,
        "hits": sum(1 for r in rows if r["hit"]),
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
