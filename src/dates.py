"""日期解析與期限計算:民國年寫法、公文相對期限、合理性檢查。

LLM 做日期運算不可靠,所以模型只負責「照抄」期限原文(例如「收到本函後15日內」),
換算成實際日期一律在這裡用程式算。所有函式都不讀系統時間:基準日(發文日期或
上傳日期)由呼叫端傳入,測試才能重現。算不出來或結果不合理一律回 None,不猜。

期間計算依行政程序法第 48 條(準用民法第 120~122 條):
- 以日、星期、月定期間者,始日不算入:「收到本函後15日內」= 基準日 + 15 日。
- 「自即日起」「自某年某月某日起」明示起算日,該日算入(第 1 天)。
- 以月定期間,末日為最後之月與起算日相當日之前一日;最後之月無相當日者,以該月末日為末日。
- 刻意**不做**「末日為假日順延」:國定假日需要行事曆資料,而提醒寧早勿晚。
"""
from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import date, datetime, timedelta

# 期限合理範圍:不得早於基準日、不得晚於基準日這麼多年
MAX_DEADLINE_YEARS = 2
# 期限原文長度上限:模型照抄的應該只是一句話,太長代表抄錯段落
MAX_DEADLINE_TEXT = 200

# 民國元年 = 西元 1912 年;西元年上限只是防呆
_ROC_OFFSET = 1911
_MIN_WESTERN_YEAR, _MAX_WESTERN_YEAR = 1912, 2199

_CN_DIGITS = {
    "〇": 0, "零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
# 數字:阿拉伯數字或國字數字(公文常見「十五日」「一百一十五年」)
_NUM = r"[0-9]+|[〇零一二兩三四五六七八九十百]+"

# 「(中華民國)115年8月3日」或「115/08/03」「115.8.3」「2026-08-03」(同一種分隔符)
_DATE_RE = re.compile(
    rf"(?:中華民國|民國)?\s*(?P<y1>{_NUM})\s*年\s*(?P<m1>{_NUM})\s*月\s*(?P<d1>{_NUM})\s*[日號]"
    r"|(?<![0-9])(?P<y2>[0-9]{2,4})(?P<sep>[/.\-])(?P<m2>[0-9]{1,2})(?P=sep)(?P<d2>[0-9]{1,2})(?![0-9])"
)

# 相對期限:「15日內」「三十日以內」「二週內」「1個月內」
_RELATIVE_RE = re.compile(rf"(?P<n>{_NUM})\s*(?:個)?\s*(?P<unit>日|天|星期|週|周|月)\s*(?:之)?\s*(?:內|以內|為限)")

# 絕對日期在計算相對期限前先換成私用區字元,避免「10月31日內」被誤認成「31日內」
_PLACEHOLDER_BASE = 0xE000
# 明示起算日:「自即日起」「自<日期>起」(該日算入);「<日期>之次日起」則不算入
_EXPLICIT_START_RE = re.compile(r"(?P<ph>[-])\s*(?P<next>之?次日)?\s*(?:起|開始)")
_TODAY_START_RE = re.compile(r"即日\s*(?:起|開始)")


def _normalize(text: str) -> str:
    """全形轉半形(NFKC):「１１５／８／３」→「115/8/3」。"""
    return unicodedata.normalize("NFKC", text)


def _to_int(token: str) -> int | None:
    """阿拉伯或國字數字 → 整數。「一百一十五」「十五」為位值寫法,「一一五」為逐位寫法。"""
    if token.isascii() and token.isdigit():
        return int(token)
    if "十" not in token and "百" not in token:
        try:
            return int("".join(str(_CN_DIGITS[c]) for c in token))
        except (KeyError, ValueError):
            return None
    total, current = 0, 0
    for c in token:
        if c in _CN_DIGITS:
            current = _CN_DIGITS[c]
        elif c == "百":
            total += (current or 1) * 100
            current = 0
        elif c == "十":
            total += (current or 1) * 10
            current = 0
    return total + current


def _year(token: str) -> int | None:
    """四位數視為西元年,其餘視為民國年(+1911);超出合理範圍回 None。"""
    n = _to_int(token)
    if n is None or n <= 0:
        return None
    if (len(token) == 4 and token.isdigit()) or n >= _MIN_WESTERN_YEAR:
        year = n
    else:
        year = n + _ROC_OFFSET
    return year if _MIN_WESTERN_YEAR <= year <= _MAX_WESTERN_YEAR else None


def _match_to_date(m: re.Match) -> date | None:
    if m.group("y1") is not None:
        y, mo, d = _year(m.group("y1")), _to_int(m.group("m1")), _to_int(m.group("d1"))
    else:
        y, mo, d = _year(m.group("y2")), int(m.group("m2")), int(m.group("d2"))
    if y is None or mo is None or d is None:
        return None
    try:
        return date(y, mo, d)
    except ValueError:  # 13 月、2 月 30 日等不存在的日期
        return None


def parse_date(text: str | None) -> date | None:
    """從字串中取出第一個日期(民國或西元),無法確定就回 None。

    支援「115年8月3日」「中華民國115年8月3日」「115/08/03」「115.8.3」「115-8-3」、
    國字「中華民國一百一十五年八月三日」、全形數字,以及西元「2026-08-03」「2026年8月3日」。
    發票期別「115年07-08月」沒有日,不是日期。
    """
    if not text:
        return None
    m = _DATE_RE.search(_normalize(str(text)))
    return _match_to_date(m) if m else None


def _as_date(value: date | str | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return parse_date(value)
    return None


def _add_years(d: date, years: int) -> date:
    """加年數;2/29 遇到平年取 2/28。"""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def _add_months(d: date, months: int) -> tuple[date, bool]:
    """加月數,回傳 (日期, 是否有相當日);沒有相當日(例 1/31 + 1 月)時取該月末日。"""
    index = d.month - 1 + months
    year, month = d.year + index // 12, index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    if d.day > last_day:
        return date(year, month, last_day), False
    return date(year, month, d.day), True


def _period_end(start: date, n: int, unit: str, start_counts: bool) -> date:
    """依期間算末日。start_counts=True 表示起算日本身是第 1 天(「自即日起」)。"""
    if unit in ("日", "天", "星期", "週", "周"):
        days = n * 7 if unit in ("星期", "週", "周") else n
        return start + timedelta(days=days - 1 if start_counts else days)
    # 以月定期間(民法第 121 條)
    if start_counts:
        corresponding, exists = _add_months(start, n)
        return corresponding - timedelta(days=1) if exists else corresponding
    return _add_months(start, n)[0]


def is_plausible_deadline(deadline: date, base: date) -> bool:
    """期限不得早於基準日,也不得晚於基準日 MAX_DEADLINE_YEARS 年。"""
    return base <= deadline <= _add_years(base, MAX_DEADLINE_YEARS)


def resolve_deadline(text: str | None, base: date | str | None) -> date | None:
    """把期限原文換算成日期;失敗或不合理回 None。

    text:模型照抄的期限原文,例「收到本函後15日內」「自送達之次日起30日內」「於115年10月31日前」。
    base:基準日(發文日期或上傳日期),date 或日期字串,由呼叫端決定,本函式不讀系統時間。
    """
    base_date = _as_date(base)
    if base_date is None or not text or len(str(text)) > MAX_DEADLINE_TEXT:
        return None

    normalized = _normalize(str(text))
    found: list[date | None] = []

    def _stash(m: re.Match) -> str:
        found.append(_match_to_date(m))
        return chr(_PLACEHOLDER_BASE + len(found) - 1)

    rest = _DATE_RE.sub(_stash, normalized)
    if any(d is None for d in found):  # 文字裡有寫壞的日期(例 13 月),寧可不算
        return None

    rel = _RELATIVE_RE.search(rest)
    if rel:
        n = _to_int(rel.group("n"))
        if not n:
            return None
        start, start_counts = base_date, False
        explicit = _EXPLICIT_START_RE.search(rest[: rel.start()])
        if explicit:
            start = found[ord(explicit.group("ph")) - _PLACEHOLDER_BASE]
            start_counts = explicit.group("next") is None
        elif _TODAY_START_RE.search(rest[: rel.start()]):
            start_counts = True
        deadline = _period_end(start, n, rel.group("unit"), start_counts)
    elif found:
        deadline = found[-1]  # 「於115年10月31日前」:直接取寫明的日期
    else:
        return None

    return deadline if is_plausible_deadline(deadline, base_date) else None
