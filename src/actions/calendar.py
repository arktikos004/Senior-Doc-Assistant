"""行事曆提醒:把期限類行動轉成 .ics(RFC 5545)。

目前沒有接到網頁:2026-10-01 使用者決定提醒先以網頁為主(首頁「要記得的事」),拿掉了 .ics 下載。
之後要加回「加入手機行事曆」時,把 to_ics 接回 web/app.py 即可。

calendar 行動的 payload 契約:
    {"title": str,                 # 例「繳電費」
     "date": "YYYY-MM-DD",         # 期限當天(全天事件)
     "description": str,           # 白話說明,可空
     "remind_days_before": int}    # 提前幾天提醒;plan_actions 一律填 3(REMIND_DAYS_BEFORE),
                                   # 缺漏或不合理時 to_ics 用 DEFAULT_REMIND_DAYS(1)

產出格式重點(RFC 5545):
- 全天事件:DTSTART;VALUE=DATE 為期限當天,DTEND 為隔天(§3.6.1,DTEND 不含當天)。
- VALARM 在期限前 N 天的早上 9 點提醒(相對全天事件 00:00 的觸發時間),N=0 為當天 9 點。
- 換行一律 CRLF;邏輯行超過 75 octets 就折行(CRLF + 一個空白),不切斷 UTF-8 多位元組字元(§3.1)。
- TEXT 值跳脫 `\\`、`;`、`,` 與換行(§3.3.11),並去掉其他控制字元。
- UID 由標題 + 日期雜湊而來:重複下載同一個提醒會更新同一事件,不會在行事曆裡重複新增。
只用標準函式庫,不讀 payload 以外的任何資料。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any

PRODID = "-//khuannu//khuannu//ZH-TW"   # 看有(khuànn-ū)
DEFAULT_TITLE = "看有提醒"
DEFAULT_REMIND_DAYS = 1
MAX_REMIND_DAYS = 30
REMIND_HOUR = 9          # 提醒時刻:當地早上 9 點
MAX_LINE_OCTETS = 75

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _utcnow() -> datetime:
    """DTSTAMP 用的現在時間(獨立成函式,測試可替換)。"""
    return datetime.now(timezone.utc)


def _clean_text(value: Any) -> str:
    """統一換行為 \\n、tab 換空白,去掉其他控制字元。"""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    return "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch) != "Cc")


def _escape(text: str) -> str:
    """RFC 5545 §3.3.11 TEXT 跳脫;反斜線要最先處理。"""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 §3.1:每一實體行不超過 75 octets,續行以一個空白開頭。"""
    out: list[str] = []
    current, size = [], 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > MAX_LINE_OCTETS:
            out.append("".join(current))
            current, size = [" "], 1
        current.append(ch)
        size += n
    out.append("".join(current))
    return "\r\n".join(out)


def _parse_date(value: Any) -> date:
    text = str(value or "")
    if not _ISO_DATE.match(text):
        raise ValueError(f"行事曆日期必須是 YYYY-MM-DD:{value!r}")
    return date.fromisoformat(text)  # 不存在的日期(13 月)會丟 ValueError


def _remind_days(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_REMIND_DAYS
    return value if 0 <= value <= MAX_REMIND_DAYS else DEFAULT_REMIND_DAYS


def _trigger(days_before: int) -> str:
    """期限前 N 天 09:00 相對於當天 00:00 的觸發時間,例 N=1 → -PT15H。"""
    hours_before = days_before * 24 - REMIND_HOUR
    sign = "-" if hours_before > 0 else ""
    d, h = divmod(abs(hours_before), 24)
    return f"{sign}P{f'{d}D' if d else ''}T{h}H"


def to_ics(payload: dict[str, Any]) -> str:
    """把 calendar 行動的 payload 轉成單一全天事件的 .ics 文字(CRLF 換行)。

    date 不是合法的 YYYY-MM-DD 時丟 ValueError;remind_days_before 不合理時用預設 1 天。
    """
    day = _parse_date(payload.get("date"))
    title = _clean_text(payload.get("title")).replace("\n", " ").strip() or DEFAULT_TITLE
    description = _clean_text(payload.get("description")).strip()
    days_before = _remind_days(payload.get("remind_days_before", DEFAULT_REMIND_DAYS))

    uid = hashlib.sha1(f"{title}|{day.isoformat()}".encode("utf-8")).hexdigest()[:20]
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}@khuannu",
        f"DTSTAMP:{_utcnow().strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}",
        f"SUMMARY:{_escape(title)}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    lines += [
        "TRANSP:TRANSPARENT",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{_escape(title)}",
        f"TRIGGER:{_trigger(days_before)}",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "".join(_fold(line) + "\r\n" for line in lines)
