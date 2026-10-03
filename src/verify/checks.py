"""確定性檢查:只看格式、檢查碼與合理性,不需要影像也不需要模型。

每個 check_* 函式回傳一筆驗證項目 {"status": "pass"|"fail"|"skip", "detail": str, "fields": [...]},
fields 用 src.models.REQUIRED_FIELDS 的命名("date"、"amount"、"fields.due_date"…),
由 verify_result 以檢查名稱(本檔的 CHECK_* 常數)放進 result.verification。

這些都是「弱證據」:通過只代表「沒有明顯讀錯」,不代表讀對(格式正確的錯字一樣會通過);
不通過則幾乎可以確定有欄位讀錯。verified_confidence 的公式依此設計(見 verify/__init__.py)。
值是空的(None、""、模型的哨兵值)一律回 skip,不當成讀錯。
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

# 檢查名稱(即 result.verification 的鍵,直接顯示在介面上)
CHECK_INVOICE_FORMAT = "字軌格式"
CHECK_SELLER_TAX_ID = "賣方統編檢查碼"
CHECK_BUYER_TAX_ID = "買方統編檢查碼"
CHECK_DATE = "日期合理性"
CHECK_PERIOD = "期別一致"
CHECK_DUE_DATE = "繳費期限"
CHECK_AMOUNT = "金額合理性"
CHECK_AMOUNT_SUM = "金額加總"
CHECK_SUBJECT = "主旨完整"
CHECK_MEDICATION_ITEMS = "藥品清單完整"

EARLIEST_DATE = date(2000, 1, 1)       # 早於 2000 年的單據不在本系統的使用情境內
FUTURE_TOLERANCE = timedelta(days=1)   # 時區、跨夜拍照的時差
AMOUNT_MAX = 10_000_000                # 家用文件超過一千萬極罕見,寧可轉人工
SUBJECT_MIN_CHARS, SUBJECT_MAX_CHARS = 4, 300  # 公文主旨少於 4 字多半沒讀完整,過長多半混進說明段
CONSUMER_TAX_ID = "00000000"           # 買受人為一般消費者時,發票記載 8 個 0

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_INVOICE_NUMBER = re.compile(r"[A-Z]{2}\d{8}")


def entry(status: str, detail: str, fields: list[str]) -> dict[str, Any]:
    """組成一筆驗證項目(契約見 verify/__init__.py)。"""
    return {"status": status, "detail": detail, "fields": list(fields)}


def is_blank(value: Any) -> bool:
    """None、空字串或只有空白:模型沒讀到(含提示詞約定的哨兵值 "")。"""
    return value is None or (isinstance(value, str) and not value.strip())


def parse_iso_date(text: Any) -> date | None:
    """嚴格解析 YYYY-MM-DD;格式不對或日期不存在(如 2 月 30 日)回 None。"""
    if not isinstance(text, str) or not _ISO_DATE.fullmatch(text.strip()):
        return None
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


# --- 統一編號 ---

_TAX_ID_WEIGHTS = (1, 2, 1, 2, 1, 2, 4, 1)


def tax_id_checksum_ok(tax_id: Any, divisor: int = 5) -> bool:
    """營利事業統一編號檢查碼。

    依財政部財政資訊中心〈營利事業統一編號檢查碼邏輯修正說明〉(110-12-22 公告)及其附件:
    https://www.fia.gov.tw/singlehtml/3?cntId=c4d9cff38c8642ef8872774ee9987283
    1. 8 位數字依序乘上邏輯乘數 1,2,1,2,1,2,4,1;
    2. 每個乘積的十位數與個位數相加,再把 8 個結果加總得 Z;
    3. Z 能被 5 整除即符合邏輯(112 年 4 月 1 日起由「可被 10 整除」放寬為「可被 5 整除」,
       新舊號碼格式相容,舊號碼在新規則下仍然通過);
    4. 特例:第 7 位數為 7 時,7×4=28 → 2+8=10,這一位「再相加時最後第二位數分別取 1 或 0」,
       也就是分別以 1 與 0 計入,得 Z1、Z2,其中之一能被 5 整除即符合。

    divisor 只給測試用來對照舊規則(10),正式檢查一律用 5。
    """
    if not isinstance(tax_id, str) or not re.fullmatch(r"\d{8}", tax_id):
        return False
    digits = [int(c) for c in tax_id]
    reduced = [sum(divmod(d * w, 10)) for d, w in zip(digits, _TAX_ID_WEIGHTS)]
    if digits[6] == 7:
        others = sum(reduced) - reduced[6]   # reduced[6] 為 10
        return (others + 1) % divisor == 0 or others % divisor == 0
    return sum(reduced) % divisor == 0


def check_tax_id(value: Any, field_name: str, label: str) -> dict[str, Any]:
    """統編檢查碼;買方為一般消費者(00000000)時沒有統編可檢查。"""
    if is_blank(value):
        return entry("skip", f"沒有讀到{label}", [field_name])
    text = str(value).strip()
    if text == CONSUMER_TAX_ID:
        return entry("skip", f"{label}為 {CONSUMER_TAX_ID}(一般消費者),不檢查", [field_name])
    if tax_id_checksum_ok(text):
        return entry("pass", f"{label} {text} 符合統編檢查碼規則", [field_name])
    return entry("fail", f"{label} {text} 不符合統編檢查碼規則(應為 8 位數字且加權和可被 5 整除)",
                 [field_name])


# --- 發票字軌 ---

def normalize_invoice_number(value: Any) -> str | None:
    """去掉連字號與空白並轉大寫:證明聯印「AB-12345678」,QR 與資料庫存「AB12345678」。"""
    if is_blank(value):
        return None
    return re.sub(r"[\s\-]", "", str(value)).upper()


def check_invoice_number_format(value: Any) -> dict[str, Any]:
    """發票字軌號碼:2 個大寫英文字母 + 8 位數字。"""
    number = normalize_invoice_number(value)
    if number is None:
        return entry("skip", "沒有讀到發票號碼", ["invoice_number"])
    if _INVOICE_NUMBER.fullmatch(number):
        return entry("pass", f"發票號碼 {number} 符合「2 碼英文 + 8 碼數字」", ["invoice_number"])
    return entry("fail", f"發票號碼 {value} 不符合「2 碼英文 + 8 碼數字」", ["invoice_number"])


# --- 日期 ---

def check_date_plausible(value: Any, today: date, field_name: str = "date") -> dict[str, Any]:
    """日期格式與合理性:YYYY-MM-DD、確實存在、不晚於明天、不早於 2000 年。"""
    if is_blank(value):
        return entry("skip", "沒有讀到日期", [field_name])
    parsed = parse_iso_date(value)
    if parsed is None:
        return entry("fail", f"日期 {value} 不是有效的 YYYY-MM-DD", [field_name])
    if parsed > today + FUTURE_TOLERANCE:
        return entry("fail", f"日期 {parsed} 晚於今天 {today},不合理", [field_name])
    if parsed < EARLIEST_DATE:
        return entry("fail", f"日期 {parsed} 早於 {EARLIEST_DATE.year} 年,不合理", [field_name])
    return entry("pass", f"日期 {parsed} 格式正確且在合理範圍內", [field_name])


_PERIOD_TEXT = re.compile(r"(\d{2,3})\s*年\s*(\d{1,2})\s*[-~～至到]\s*(\d{1,2})\s*月")
_PERIOD_CODE = re.compile(r"(\d{3})(\d{2})")   # 一維條碼的年期別:民國年 3 碼 + 雙數月 2 碼


def parse_period(value: Any) -> tuple[int, int, int] | None:
    """解析發票期別,回傳 (西元年, 起月, 迄月);不是合法的雙月期別回 None。

    接受證明聯印刷的「115年07-08月」(也容許「115年7-8月」、「~」)與一維條碼的「11508」。
    統一發票每兩個月一期,起月必為奇數(1、3、…、11)、迄月為起月 + 1。
    """
    if is_blank(value):
        return None
    text = str(value).strip()
    m = _PERIOD_TEXT.search(text)
    if m:
        roc, start, end = (int(x) for x in m.groups())
    else:
        m = _PERIOD_CODE.fullmatch(text)
        if not m:
            return None
        roc, end = (int(x) for x in m.groups())
        start = end - 1
    if not (start % 2 == 1 and 1 <= start <= 11 and end == start + 1) or roc <= 0:
        return None
    return roc + 1911, start, end


def check_date_in_period(date_value: Any, period_value: Any) -> dict[str, Any]:
    """發票開立日期必須落在期別的兩個月內(例:115年07-08月 = 2026-07-01 ~ 2026-08-31)。"""
    fields = ["date", "fields.period"]
    if is_blank(date_value) or is_blank(period_value):
        return entry("skip", "日期或期別沒有讀到,無法比對", fields)
    period = parse_period(period_value)
    if period is None:
        # 期別本身不合法:是期別讀錯,不牽連日期
        return entry("fail", f"期別「{period_value}」不是合法的雙月期別", ["fields.period"])
    parsed = parse_iso_date(date_value)
    if parsed is None:
        return entry("skip", f"日期 {date_value} 無法解析,無法與期別比對", fields)
    year, start, end = period
    if parsed.year == year and start <= parsed.month <= end:
        return entry("pass", f"日期 {parsed} 落在期別 {year} 年 {start}-{end} 月內", fields)
    return entry("fail", f"日期 {parsed} 不在期別「{period_value}」({year} 年 {start}-{end} 月)內",
                 fields)


def check_due_date(due_value: Any, date_value: Any) -> dict[str, Any]:
    """帳單繳費期限:YYYY-MM-DD 且不早於出帳日(有出帳日時)。"""
    fields = ["fields.due_date"]
    if is_blank(due_value):
        return entry("skip", "沒有讀到繳費期限", fields)
    due = parse_iso_date(due_value)
    if due is None:
        return entry("fail", f"繳費期限 {due_value} 不是有效的 YYYY-MM-DD", fields)
    if due < EARLIEST_DATE:
        return entry("fail", f"繳費期限 {due} 早於 {EARLIEST_DATE.year} 年,不合理", fields)
    issued = parse_iso_date(date_value)
    if issued is None:
        return entry("pass", f"繳費期限 {due} 格式正確(沒有出帳日可比對)", fields)
    if due < issued:
        return entry("fail", f"繳費期限 {due} 早於出帳日 {issued},不合理", fields)
    return entry("pass", f"繳費期限 {due} 格式正確且不早於出帳日 {issued}", fields)


# --- 金額 ---

def check_amount_plausible(amount: Any, currency: str = "NTD") -> dict[str, Any]:
    """金額合理性:大於 0、低於一千萬;新臺幣為整數,其他幣別最多 2 位小數。"""
    fields = ["amount"]
    if amount is None:
        return entry("skip", "沒有讀到金額", fields)
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return entry("fail", f"金額 {amount} 不是數字", fields)
    if value <= 0:
        return entry("fail", f"金額 {amount} 必須大於 0", fields)
    if value >= AMOUNT_MAX:
        return entry("fail", f"金額 {value:,.0f} 超過家用文件的合理範圍,請人工確認", fields)
    decimals = 0 if (currency or "NTD") == "NTD" else 2
    if abs(value * 10 ** decimals - round(value * 10 ** decimals)) > 1e-6:
        rule = "新臺幣金額應為整數" if decimals == 0 else "金額最多 2 位小數"
        return entry("fail", f"金額 {amount}:{rule}", fields)
    return entry("pass", f"金額 {value:g} {currency} 在合理範圍內", fields)


def check_amount_sum(total: Any, line_amounts: list[float] | None) -> dict[str, Any]:
    """明細加總 = 總計。

    注意:算術通過**不能當唯一證據**——模型會「湊數」讓明細剛好加到它讀的總額,
    所以這項只算弱證據,與格式檢查同級。目前的 ExtractionResult 沒有明細欄位,
    verify_result 傳 None,一律 skip;介面先留好,之後有明細時直接接上。
    """
    fields = ["amount"]
    if total is None or not line_amounts:
        return entry("skip", "沒有明細可加總(算術通過也不能當唯一證據,模型可能湊數讓總額對上)",
                     fields)
    subtotal = sum(float(x) for x in line_amounts)
    if abs(subtotal - float(total)) < 0.005:
        return entry("pass", f"明細加總 {subtotal:g} 等於總計(僅為弱證據)", fields)
    return entry("fail", f"明細加總 {subtotal:g} 不等於總計 {float(total):g}", fields)


# --- 公文、藥袋 ---

def check_subject(value: Any) -> dict[str, Any]:
    """公文主旨:讀得到且長度合理(弱證據:只確認有讀出完整的一句,不確認內容正確)。"""
    fields = ["fields.subject"]
    if is_blank(value):
        return entry("skip", "沒有讀到主旨", fields)
    text = str(value).strip()
    if len(text) < SUBJECT_MIN_CHARS:
        return entry("fail", f"主旨只有 {len(text)} 字,可能沒讀完整", fields)
    if len(text) > SUBJECT_MAX_CHARS:
        return entry("fail", f"主旨長達 {len(text)} 字,可能混進了說明段落", fields)
    return entry("pass", f"主旨讀出 {len(text)} 字,長度合理", fields)


def check_medication_items(items: Any) -> dict[str, Any]:
    """藥品清單結構完整:每一項都有藥名,且至少有頻次、時段或「需要時」其中之一。

    只檢查「有沒有讀出來」,不檢查藥名或劑量是否正確(系統不提供藥學判斷);
    服藥時間表不論分數高低,一律要家人確認(見 src/actions)。
    """
    fields = ["fields.items"]
    if not isinstance(items, list) or not items:
        return entry("skip", "沒有讀到藥品清單", fields)
    incomplete = []
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict) or is_blank(item.get("name")):
            incomplete.append(f"第 {i} 項沒有藥名")
            continue
        has_usage = (not is_blank(item.get("frequency_text"))
                     or bool(item.get("timing")) or bool(item.get("prn")))
        if not has_usage:
            incomplete.append(f"第 {i} 項沒有讀到用法")
    if incomplete:
        return entry("fail", "藥品清單不完整:" + "、".join(incomplete), fields)
    return entry("pass", f"讀出 {len(items)} 項藥品,每項都有藥名與用法", fields)
