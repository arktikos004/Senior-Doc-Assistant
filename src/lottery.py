"""統一發票對獎(純函式):期別、中獎號碼的正規化與檢查、比對獎項、領獎提醒的內容。

規則依財政部公告(115 年):兩個月一期,單數月 25 日開出上一期的號碼。
- 特別獎 1,000 萬元:8 碼和特別獎號碼一樣。
- 特獎 200 萬元:8 碼和特獎號碼一樣。
- 頭獎 20 萬元:8 碼和任一組頭獎號碼一樣;二獎 4 萬、三獎 1 萬、四獎 4 千、五獎 1 千、六獎 200 元:
  末 7、6、5、4、3 碼和任一組頭獎號碼的末幾碼一樣。
同一張只算最高的一個獎。增開六獎不是每期都有,表單沒有這一欄,不比。

號碼與領獎期限由家人照公告輸入(期限遇假日會順延,所以不由程式推算);程式不連網、不去查財政部的資料。
只比對發票號碼經 QR Code 核對相符的發票(src.duplicates.verified_invoice_number),結果只供參考。
打統編的發票不比對:〈統一發票給獎辦法〉第 11 條,買受人是機關、公立學校、營業人等的發票不能領獎;
有沒有打統編看 QR 記載的買方統編(qr_buyer_has_tax_id),不看模型讀到的。
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .actions import CALENDAR, REMIND_DAYS_BEFORE
from .verify.checks import CONSUMER_TAX_ID
from .verify.einvoice import CHECK_QR_BUYER, QR_BUYER_CONSUMER

SETTING_PREFIX = "lottery:"     # 號碼存在設定裡:lottery:<期別>,例 lottery:115-09
FIRST_SETS = 3                  # 頭獎號碼的組數
CLAIM_WINDOW_DAYS = 200         # 領獎期限只收開獎日之後 200 天內(公告約 3 個月,遇假日順延;擋打錯年份)
REMINDER_TITLE = "兌領統一發票獎金"
NO_CLAIM_NOTE = "本系統只提醒，不會替您領獎。"

_PERIOD = re.compile(r"(\d{3})-(0[1-9]|1[0-2])")
_INVOICE = re.compile(r"[A-Z]{2}(\d{8})")


@dataclass(frozen=True)
class Prize:
    name: str
    amount: int     # 元


SPECIAL = Prize("特別獎", 10_000_000)
GRAND = Prize("特獎", 2_000_000)
# 和頭獎號碼比末幾碼:末 8 碼一樣是頭獎,依序到末 3 碼的六獎
BY_TAIL: tuple[tuple[int, Prize], ...] = (
    (8, Prize("頭獎", 200_000)), (7, Prize("二獎", 40_000)), (6, Prize("三獎", 10_000)),
    (5, Prize("四獎", 4_000)), (4, Prize("五獎", 1_000)), (3, Prize("六獎", 200)),
)


@dataclass(frozen=True)
class Numbers:
    """一期的中獎號碼與領獎期限(家人照公告輸入的,已檢查過格式)。"""
    special: str
    grand: str
    first: tuple[str, ...]
    claim_by: date

    def to_json(self) -> str:
        return json.dumps({"special": self.special, "grand": self.grand, "first": list(self.first),
                           "claim_by": self.claim_by.isoformat()}, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str | None) -> Numbers | None:
        """設定裡存的值 → Numbers;沒有或壞掉(格式不對)回 None,畫面當作還沒輸入。"""
        try:
            raw = json.loads(text or "")
            first = tuple(normalize_number(x) for x in raw["first"])
            numbers = cls(normalize_number(raw["special"]), normalize_number(raw["grand"]), first,
                          date.fromisoformat(raw["claim_by"]))
        except (ValueError, TypeError, KeyError):
            return None
        if not numbers.special or not numbers.grand or len(first) != FIRST_SETS or not all(first):
            return None
        return numbers


@dataclass(frozen=True)
class Hit:
    prize: Prize
    detail: str     # 對到哪一個號碼,例「末 3 碼和頭獎第 3 組一樣」


def period_of(day: date) -> str:
    """發票日期 → 期別「115-09」(民國年-這一期的單數月)。"""
    odd = day.month if day.month % 2 else day.month - 1
    return f"{day.year - 1911:03d}-{odd:02d}"


def parse_period(key: Any) -> tuple[int, int] | None:
    """期別 → (西元年, 單數月);格式不對或不是單數月回 None。"""
    m = _PERIOD.fullmatch(key) if isinstance(key, str) else None
    if not m or int(m.group(2)) % 2 == 0 or int(m.group(1)) == 0:
        return None
    return int(m.group(1)) + 1911, int(m.group(2))


def period_label(key: str) -> str:
    """期別 → 「115 年 9、10 月期」。"""
    year, month = parse_period(key) or (0, 0)
    return f"{year - 1911} 年 {month}、{month + 1} 月期" if month else key


def draw_date(key: str) -> date | None:
    """這一期的開獎日:第二個月的下一個月 25 日(9、10 月期在 11 月 25 日,11、12 月期在隔年 1 月 25 日)。"""
    parsed = parse_period(key)
    if parsed is None:
        return None
    year, month = parsed
    return date(year + 1, 1, 25) if month == 11 else date(year, month + 2, 25)


def latest_drawn(today: date) -> str:
    """今天以前最近開過獎的一期(對獎頁預設這一期);例 10 月 29 日 → 115-07(9 月 25 日開獎)。"""
    year, month = today.year, today.month if today.month % 2 else today.month - 1
    while True:
        key = f"{year - 1911:03d}-{month:02d}"
        if draw_date(key) <= today:
            return key
        year, month = (year - 1, 11) if month == 1 else (year, month - 2)


def normalize_number(text: Any) -> str | None:
    """8 碼數字:全形數字轉半形、去掉空白與連字號;不是 8 碼數字回 None。"""
    if text is None:
        return None
    cleaned = re.sub(r"[\s\-]", "", unicodedata.normalize("NFKC", str(text)))
    return cleaned if re.fullmatch(r"\d{8}", cleaned) else None


def parse_form(form: dict[str, Any], period: str) -> tuple[Numbers | None, dict[str, str]]:
    """對獎頁的表單 → (Numbers, 每一欄的錯誤說明);有錯時 Numbers 是 None。表單值一律當資料。"""
    errors: dict[str, str] = {}
    special = normalize_number(form.get("special"))
    grand = normalize_number(form.get("grand"))
    first = tuple(normalize_number(form.get(f"first{i}")) for i in range(1, FIRST_SETS + 1))
    for name, value in (("special", special), ("grand", grand),
                        *((f"first{i}", v) for i, v in enumerate(first, 1))):
        if value is None:
            errors[name] = "請輸入公告上的 8 碼數字"
    claim_by = None
    drawn = draw_date(period)
    try:
        claim_by = date.fromisoformat(str(form.get("claim_by") or ""))
    except ValueError:
        errors["claim_by"] = "請照公告選領獎期限的日期"
    if claim_by is not None and drawn is not None and not drawn < claim_by <= drawn + timedelta(days=CLAIM_WINDOW_DAYS):
        errors["claim_by"] = f"領獎期限要在開獎日（{drawn.month}月{drawn.day}日）之後，請對照公告"
    if errors or claim_by is None:
        return None, errors
    return Numbers(special, grand, first, claim_by), {}


def match(invoice_number: str | None, numbers: Numbers) -> Hit | None:
    """這張發票可能中的獎(只算最高的一個);號碼不是「2 碼英文 + 8 碼數字」或沒中回 None。"""
    m = _INVOICE.fullmatch(invoice_number or "")
    if not m:
        return None
    digits = m.group(1)
    if digits == numbers.special:
        return Hit(SPECIAL, "8 碼和特別獎號碼一樣")
    if digits == numbers.grand:
        return Hit(GRAND, "8 碼和特獎號碼一樣")
    for tail, prize in BY_TAIL:
        for i, first in enumerate(numbers.first, 1):
            if digits[-tail:] == first[-tail:]:
                return Hit(prize, f"8 碼和頭獎第 {i} 組一樣" if tail == 8 else f"末 {tail} 碼和頭獎第 {i} 組一樣")
    return None


def qr_buyer_has_tax_id(result: dict[str, Any] | None) -> bool | None:
    """QR 記載這張發票有沒有買方統編(打統編):有 True、一般消費者 False、沒有 QR 可看 None。

    看的是核對結果(result.verification 的「QR 買方統編」),所以模型沒讀到或讀錯買方統編也照樣分得出來:
    - 一般消費者:說明是 QR_BUYER_CONSUMER;或模型讀到一組買方統編,但 QR 記載的是 00000000(不符,expected 是 00000000)。
    - 其他情況(相符、不符、模型沒讀到而 QR 有記載)都表示 QR 上有買方統編。
    """
    verification = result.get("verification") if isinstance(result, dict) else None
    check = verification.get(CHECK_QR_BUYER) if isinstance(verification, dict) else None
    if not isinstance(check, dict) or check.get("status") not in ("pass", "fail", "skip"):
        return None
    if check.get("status") == "skip" and check.get("detail") == QR_BUYER_CONSUMER:
        return False
    expected = check.get("expected")
    if check.get("status") == "fail" and isinstance(expected, dict) \
            and expected.get("fields.buyer_tax_id") == CONSUMER_TAX_ID:
        return False
    return True


def prize_text(hit: Hit) -> str:
    """「可能中了六獎（200 元）：末 3 碼和頭獎第 3 組一樣」"""
    return f"可能中了{hit.prize.name}（{hit.prize.amount:,} 元）：{hit.detail}"


def claim_hint(claim_by: date) -> str:
    return (f"請對照發票正本，在{claim_by.year - 1911}年{claim_by.month}月{claim_by.day}日前帶發票正本和身分證件去兌領；"
            "地點與方式以財政部公告為準。")


def reminder_action(invoice_number: str, hit: Hit, numbers: Numbers, period: str) -> dict[str, Any]:
    """家人在對獎頁按「加進要記得的事」時產生的領獎提醒(格式同 src.actions 的行動)。

    分級一律 auto:是家人自己按的、只列在網頁上、可以在發票的結果頁取消(原則 4)。標題固定,說明全由程式組字;
    號碼與獎項是伺服器用存好的中獎號碼重新比對得出的,文件文字與表單送來的值都不參與。
    """
    lines = [f"發票號碼：{invoice_number[:2]}-{invoice_number[2:]}", f"對獎：{period_label(period)}",
             prize_text(hit), claim_hint(numbers.claim_by), NO_CLAIM_NOTE]
    return {"kind": CALENDAR, "tier": "auto",
            "payload": {"title": REMINDER_TITLE, "date": numbers.claim_by.isoformat(),
                        "description": "\n".join(lines), "remind_days_before": REMIND_DAYS_BEFORE,
                        "source": "lottery", "period": period}}


def is_lottery_reminder(action: dict[str, Any], period: str | None = None) -> bool:
    """這筆行動是不是對獎頁加的領獎提醒(period 有給時,還要是同一期)。"""
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    return (action.get("kind") == CALENDAR and payload.get("source") == "lottery"
            and (period is None or payload.get("period") == period))
