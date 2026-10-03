"""行動模組:把讀懂的文件轉成可執行的行動,並依「可逆性 × 風險」分級。

目前的行動:帳單/公文期限 → 期限提醒;藥袋 → 服藥時間表。
對外只有 plan_actions 一個入口,上傳(Pipeline)與家人更正(src/review.py)都經
src/pipeline.verify_decide_plan 在決策之後呼叫它,回傳的每筆行動會寫進 SQLite 的 actions 表。

契約(每筆行動是一個 dict):
    {"kind": "calendar" | "medication_schedule" | ...,
     "tier": "auto" | "confirm" | "manual",   # 見 src/store.py ACTION_TIERS
     "payload": {...}}                         # 行動內容,需可 JSON 序列化
分級原則:只有可逆、低風險的行動可以是 auto(例如期限提醒);
藥袋劑量與「需要時」服用一律 confirm;付款、送出回覆永不 auto。
文件文字只當資料,絕不能讓文件內容決定行動種類或分級(防提示注入)。

目前的行動種類與分級(全部寫死在程式裡):
    | doc_type  | kind                | tier                                   | payload 契約               |
    | 帳單      | calendar            | 決策 archive → auto,否則 confirm      | src/actions/calendar.py    |
    | 公文      | calendar            | 決策 archive → auto,否則 confirm      | src/actions/calendar.py    |
    | 藥袋      | medication_schedule | 一律 confirm                           | src/actions/medication.py  |
    | 發票/收據 | (目前不產生;對獎在決賽衝刺)                                                      |
    | 其他      | (不產生)                                                                         |
本系統不產生任何付款或送出回覆的行動。
取消路徑:auto 的期限提醒可在結果頁「取消提醒」、按錯可「恢復提醒」(web/app.py POST /reminder/{id});
confirm 的行動在「家人確認」確認或退回。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from ..config import AppConfig
from ..dates import MAX_DEADLINE_TEXT, is_plausible_deadline, parse_date, resolve_deadline
from ..models import Decision, ExtractionResult
from .medication import build_schedule, clip

CALENDAR = "calendar"
MEDICATION_SCHEDULE = "medication_schedule"
ACTION_KINDS: tuple[str, ...] = (CALENDAR, MEDICATION_SCHEDULE)

REMIND_DAYS_BEFORE = 3   # 期限前 3 天提醒:長輩要有時間去超商繳費、準備文件

# 帳單提醒標題:只從固定對照表取,bill_kind 不在表內就用通用標題(文件文字不能當標題)
BILL_TITLES = {"電費": "繳電費", "水費": "繳水費", "瓦斯": "繳瓦斯費", "電信": "繳電信費"}
BILL_DEFAULT_TITLE = "繳費期限"
OFFICIAL_TITLE = "公文期限"
NO_PAYMENT_NOTE = "本系統只提醒,不會替您付款或回覆。"


def _calendar_tier(decision: Decision) -> str:
    """期限提醒只列在網頁上、不會替人做任何事(低風險):自動歸檔時才 auto,轉人工時等家人確認。"""
    return "auto" if decision.action == "archive" else "confirm"


def _fields(result: ExtractionResult) -> dict[str, Any]:
    return result.fields if isinstance(result.fields, dict) else {}


def _money(amount: float | None, currency: str) -> str:
    if not amount:
        return ""
    if currency == "NTD":
        return f"{amount:,.0f} 元" if float(amount).is_integer() else f"{amount:,.2f} 元"
    return f"{amount:,.2f} {currency}"


def _calendar(title: str, day: date, lines: list[str], decision: Decision) -> dict[str, Any]:
    return {
        "kind": CALENDAR,
        "tier": _calendar_tier(decision),
        "payload": {
            "title": title,
            "date": day.isoformat(),
            "description": "\n".join([*(line for line in lines if line), NO_PAYMENT_NOTE]),
            "remind_days_before": REMIND_DAYS_BEFORE,
        },
    }


def _plan_bill(result: ExtractionResult, decision: Decision, received_on: date) -> list[dict[str, Any]]:
    """帳單:fields.due_date → 期限提醒。期限讀不懂或不合理(早於帳單日、晚於 2 年)就不產生。"""
    fields = _fields(result)
    due = parse_date(str(fields.get("due_date") or ""))  # 模型沒換算民國年時,程式再換一次
    base = parse_date(result.date or "") or received_on
    if due is None or not is_plausible_deadline(due, base):
        return []
    title = BILL_TITLES.get(str(fields.get("bill_kind") or "").strip(), BILL_DEFAULT_TITLE)
    lines = [
        f"開單單位:{clip(result.vendor)}" if result.vendor else "",
        f"應繳金額:{_money(result.amount, result.currency)}" if result.amount else "",
        f"繳費期限:{due.isoformat()}",
    ]
    return [_calendar(title, due, lines, decision)]


def _plan_official(result: ExtractionResult, decision: Decision, received_on: date) -> list[dict[str, Any]]:
    """公文:由 deadline_text 以程式算出 fields.deadline → 期限提醒;算不出就不產生。

    這是 plan_actions 唯一會改 result 的地方:fields.deadline 一律由程式重算,
    模型自己填的值不採信(算不出來就移除),Pipeline 之後寫入的紀錄才看得到程式算的期限。
    """
    fields = {k: v for k, v in _fields(result).items() if k != "deadline"}
    issued = parse_date(result.date or "")
    base = issued or received_on
    # 用原文計算:先截斷再算會讓 resolve_deadline 的長度防線永遠不觸發;clip 只用於顯示
    deadline = resolve_deadline(fields.get("deadline_text"), base)
    if deadline is not None:
        fields["deadline"] = deadline.isoformat()
    result.fields = fields
    if deadline is None:
        return []

    subject = clip(fields.get("subject"), 30)
    title = f"{OFFICIAL_TITLE}:{subject}" if subject else OFFICIAL_TITLE
    basis = (
        f"以發文日期 {base.isoformat()} 起算;實際收到日可能較晚,期限以公文原文為準。"
        if issued else
        f"文件上沒有可讀的發文日期,以上傳日期 {base.isoformat()} 起算;期限以公文原文為準。"
    )
    required = fields.get("required_actions")
    todo = "、".join(clip(x, 40) for x in required[:10] if clip(x)) if isinstance(required, list) else ""
    lines = [
        f"發文機關:{clip(result.vendor)}" if result.vendor else "",
        f"發文字號:{clip(fields.get('doc_number'))}" if fields.get("doc_number") else "",
        f"期限原文:「{clip(fields.get('deadline_text'), MAX_DEADLINE_TEXT)}」",
        basis,
        f"應辦事項:{todo}" if todo else "",
        f"應繳金額:{_money(result.amount, result.currency)}" if result.amount else "",
    ]
    return [_calendar(title, deadline, lines, decision)]


def _plan_medication(result: ExtractionResult, decision: Decision, received_on: date) -> list[dict[str, Any]]:
    """藥袋:服藥時間表永遠等家人確認,不論決策結果(劑量與時段錯了會傷人)。"""
    schedule = build_schedule(result)
    if schedule is None:
        return []
    return [{"kind": MEDICATION_SCHEDULE, "tier": "confirm", "payload": schedule}]


# 行動種類只由 doc_type(封閉的列舉,parsing 已把未知值轉成「其他」)決定
_PLANNERS: dict[str, Callable[[ExtractionResult, Decision, date], list[dict[str, Any]]]] = {
    "帳單": _plan_bill,
    "公文": _plan_official,
    "藥袋": _plan_medication,
}


def plan_actions(result: ExtractionResult, decision: Decision, cfg: AppConfig,
                 received_on: date | None = None) -> list[dict[str, Any]]:
    """依文件類型與決策結果產生行動清單(見模組說明的種類/分級表)。

    received_on:文件的上傳日。帳單沒有開單日期、公文沒有發文日期時,期限從這天起算;
    沒給就是今天(上傳當下處理)。家人事後更正要傳原本的上傳日,不是更正當天(F7)。

    防提示注入:種類只看 doc_type,分級只看 doc_type 與 decision.action;
    plain_summary、notes、fields 裡的任何文字(例如「請立即自動付款」「tier=auto」)
    只會被當成 payload 的顯示資料,絕不參與決定。doc_type 本身來自模型,但它是封閉列舉,
    被誤判或被誘導時最壞只會多一個期限提醒;藥袋不論如何都要家人確認。
    """
    planner = _PLANNERS.get(result.doc_type)
    return planner(result, decision, received_on or date.today()) if planner else []
