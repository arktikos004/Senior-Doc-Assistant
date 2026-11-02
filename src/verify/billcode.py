"""帳單的超商代收三段式條碼:解碼 Code 39 → 辨認三段 → 與辨識結果比對。

台灣的水費、電費、電信費、地方稅、健保費等帳單,多印有給超商代收用的三段式 Code 39 一維條碼。條碼是代收單位的
程式直接印上的機器資料,與模型怎麼「看」影像無關,所以和電子發票左側 QR 一樣能當**獨立證據**:
讀到的應繳金額與條碼一致,就不只是模型的一面之詞;不一致則幾乎可以確定讀錯。

規格依據:統一超商〈代收服務之電腦條碼及安全機制〉(個人部落格 2011 年轉載,二手),以綠界科技 ECPay 開發者文件的
兩組範例條碼(一手,2013 年版與 2025 年版文件)交叉驗證。
- 三段由上而下排列,各段都是 Code 39,前後加起訖字元 `*`;
- 第一段 9 碼:代收期限 yymmdd(6)+ 代收項目代號(3);yy 是民國年的末兩碼(民國 114 年 → 14);
- 第二段 16 碼:銷帳編號,各代收單位自訂(英數字),帳單的唯一性以這一段為準;
- 第三段 15 碼:應繳日期 mmdd(4)+ 校對碼(2)+ 應繳金額(9,新臺幣元,左邊補 0);
- 校對碼「公式一」:三段各自由左而右編號,英文字母換成數字(A-I → 1-9、J-R → 1-9、S-Z → 2-9),第三段的
  第 5、6 碼(校對碼本身)以 0 計;所有奇數位的和除以 11,餘數 0 記 A、10 記 B、其餘記餘數(第一碼);
  所有偶數位的和除以 11,餘數 0 記 X、10 記 Y、其餘記餘數(第二碼)。

和電子發票 QR 不同的地方(原型照這個設計):
- 金額可以直接比;期限不行:第一段的日期是**代收期限**(超商最後收款日),不一定等於帳單印的繳費期限
  (自來水:繳費期限當月 21 日、代收期限次月 21 日;地方稅:代收期限是繳納期間屆滿後 3 日)。
  發行單位怎麼填第一段、第三段的日期,目前沒有一手規格可查,所以期限比對只在「相符」時給 pass,
  差在寬限天數內是 skip(無法證實,不當成讀錯),差太多才是 fail(兩個方向都一樣,不假設誰先誰後)。
- 校對碼不符、少一段、有好幾組不同的條碼 → 一律當成「沒有可用的條碼」(skip),不會因為讀壞條碼而誤判。
- 年份只有兩位數:民國 100 年(2011)起會重複,本原型一律解讀成 2011~2110 年。
- 第二段(銷帳編號)可能含客戶編號:BillBarcode 的 repr 不印它,檢查項目的說明文字也不寫它。
- 條碼只證明「印出來的數字」和「讀出來的數字」一致,不證明帳單是真的:條碼沒有簽章,偽造的帳單一樣印得出合法的條碼。

沒有 OpenCV 或 zxing-cpp 時只是少一次解碼的機會(回空清單、檢查項目 skip),不丟例外。
"""
from __future__ import annotations

import logging
import re
import string
from dataclasses import dataclass, field
from datetime import date
from itertools import product
from pathlib import Path
from typing import Any

from ..models import ExtractionResult
from .checks import entry, is_blank, parse_iso_date
from .einvoice import alternate_images, load_image

log = logging.getLogger(__name__)

# 檢查名稱(result.verification 的鍵,直接顯示在介面上)。本檔不動既有契約:整合時由整合者決定哪些併進
# src.verify.INDEPENDENT_CHECKS(見 spike 文件)。
CHECK_BARCODE = "超商代收條碼"
CHECK_BARCODE_AMOUNT = "條碼應繳金額"
CHECK_BARCODE_DUE = "條碼代收期限"
# 金額是獨立證據:條碼金額是代收單位印上的機器資料,不一致幾乎可以確定讀錯。
BILL_INDEPENDENT_CHECKS = frozenset({CHECK_BARCODE_AMOUNT})
# 期限只是輔助:條碼第一段是超商的「代收期限」,不等於帳單印的「繳費期限」(自來水差約一個月、地方稅差 3 天),
# 日期完全相同時是強證據(不太可能是巧合),不同時卻無法證實或否定。建議先不併入 INDEPENDENT_CHECKS
# (它的 fail 照樣會讓必要欄位檢查不通過、轉人工),等真實帳單驗證後再決定要不要升級。
BILL_SUPPORT_CHECKS = frozenset({CHECK_BARCODE_DUE})

COLLECT_GRACE_DAYS = 45   # 帳單印的期限和條碼的日期差幾天以內,算「可能是不同的日期」而不是讀錯(台水約 31 天、地方稅 3 天)
YEAR_BASE = 2011          # 第一段的 yy 是民國年的末兩碼:民國 100 年 = 2011 年,yy 00~99 → 2011~2110 年
TILT_ANGLES = (8.0, -8.0, 16.0, -16.0, 24.0, -24.0, 32.0, -32.0)   # 解不開時依序試的轉正角度(度,逆時針為正)
MAX_EDGE = 8000           # 銳化、放大的前處理只做到這麼大(像素;手機照片最大約 8000×6000)
ROTATE_MAX_EDGE = 4000    # 轉正前先把長邊縮到這麼大(像素)

_SEG1 = re.compile(r"(?P<yy>[0-9]{2})(?P<mm>[0-9]{2})(?P<dd>[0-9]{2})(?P<item>[0-9A-Z]{3})")
_SEG2 = re.compile(r"[0-9A-Z]{16}")
_SEG3 = re.compile(r"(?P<mmdd>[0-9]{4})(?P<check>[0-9A-Z]{2})(?P<amount>[0-9]{9})")

# 校對碼公式一的字母換算表:A-I → 1-9、J-R → 1-9、S-Z → 2-9
_LETTER_VALUE = dict(zip(string.ascii_uppercase, [*range(1, 10), *range(1, 10), *range(2, 10)]))


@dataclass(frozen=True)
class BillBarcode:
    """三段條碼的解析結果:期限已轉西元、金額已轉整數、校對碼已驗過。"""

    due_date: str          # 第一段的代收期限(超商最後收款日),西元 YYYY-MM-DD
    item_code: str         # 第一段後 3 碼:代收項目代號(超商給的碼,本系統不解讀)
    pay_date: str | None   # 第三段前 4 碼 mmdd 的應繳日期(年份取最接近代收期限的那一年);沒有這個月日為 None
    check: str             # 第三段第 5、6 碼:校對碼
    amount: int            # 第三段後 9 碼:應繳金額,新臺幣元
    account_ref: str = field(repr=False)             # 第二段:銷帳編號(可能含客戶編號,不寫進紀錄與畫面)
    raw: tuple[str, str, str] = field(repr=False)    # 三段原文


# --- 校對碼、組出與解析 ---

def _value(ch: str) -> int:
    if "0" <= ch <= "9":
        return ord(ch) - ord("0")
    try:
        return _LETTER_VALUE[ch]
    except KeyError:
        raise ValueError(f"校對碼公式不認得字元 {ch!r}") from None


def check_code(seg1: str, seg2: str, seg3: str) -> str:
    """校對碼公式一:由三段條碼算出兩碼校對碼。seg3 的第 5、6 碼是校對碼本身,不論填什麼都以 0 計。

    奇偶位是每一段各自由左而右數(第 1 碼是奇數位),不是三段接起來數:規格範例與兩組綠界文件範例都符合這個讀法。
    """
    odd = even = 0
    for seg in (seg1, seg2, seg3[:4] + "00" + seg3[6:]):
        for position, ch in enumerate(seg, start=1):
            if position % 2:
                odd += _value(ch)
            else:
                even += _value(ch)
    first, second = odd % 11, even % 11
    return (("A" if first == 0 else "B" if first == 10 else str(first))
            + ("X" if second == 0 else "Y" if second == 10 else str(second)))


def build_store_barcodes(due: date, item_code: str, account_ref: str, amount: int,
                         pay_date: date | None = None) -> tuple[str, str, str]:
    """依規格組出三段條碼字串(合成樣本與測試用)。

    due 是代收期限(第一段);pay_date 是第三段的應繳日期,沒給就用 due 的月日;account_ref 要剛好 16 碼英數字。
    """
    if not YEAR_BASE <= due.year <= YEAR_BASE + 99:
        raise ValueError("代收期限只能是 2011~2110 年(第一段只放民國年的末兩碼)")
    if not 0 <= amount <= 999_999_999:
        raise ValueError("應繳金額必須是 0~999,999,999 的整數")
    if not (re.fullmatch(r"[0-9A-Z]{3}", item_code) and _SEG2.fullmatch(account_ref)):
        raise ValueError("代收項目代號要 3 碼、銷帳編號要 16 碼,都只能是大寫英文字母與數字")
    seg1 = f"{due.year - YEAR_BASE:02d}{due.month:02d}{due.day:02d}{item_code}"
    pay = pay_date or due
    head, tail = f"{pay.month:02d}{pay.day:02d}", f"{amount:09d}"
    return seg1, account_ref, head + check_code(seg1, account_ref, head + "00" + tail) + tail


def _nearest_date(month: int, day: int, near: date) -> date | None:
    """月日對上離 near 最近的那一年(前一年、當年、後一年挑);三年都沒有這一天(2 月 29 日)回 None。"""
    best = None
    for year in (near.year - 1, near.year, near.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((candidate - near).days) < abs((best - near).days):
            best = candidate
    return best


def _parse_triple(seg1: str, seg2: str, seg3: str) -> BillBarcode:
    """辨認一組三段條碼;不是合法的(長度、字元、日期或校對碼不對)丟 ValueError,訊息就是原因。"""
    m1, m3 = _SEG1.fullmatch(seg1), _SEG3.fullmatch(seg3)
    if not (m1 and _SEG2.fullmatch(seg2) and m3):
        raise ValueError("字元不符規格（三段要是 9、16、15 碼的大寫英文字母與數字）")
    try:
        due = date(YEAR_BASE + int(m1["yy"]), int(m1["mm"]), int(m1["dd"]))
    except ValueError:
        raise ValueError("第一段的代收期限不是有效的日期") from None
    month, day = int(m3["mmdd"][:2]), int(m3["mmdd"][2:])
    try:
        date(2024, month, day)   # 2024 是閏年:只確認有這個月日,不管哪一年
    except ValueError:
        raise ValueError("第三段的前 4 碼不是有效的月日") from None
    if m3["check"] != check_code(seg1, seg2, seg3):
        raise ValueError("校對碼不符，可能有一個字讀錯")
    pay = _nearest_date(month, day, due)
    return BillBarcode(
        due_date=due.isoformat(), item_code=m1["item"], pay_date=pay.isoformat() if pay else None,
        check=m3["check"], amount=int(m3["amount"]), account_ref=seg2, raw=(seg1, seg2, seg3))


def _scan(texts: list[str]) -> tuple[list[BillBarcode], str]:
    """從解出的字串找合法的三段條碼,回傳 (合法的組合, 不是剛好一組時的原因)。

    三段用長度分辨(9、16、15 碼);校對碼把三段綁在一起,所以同一段有兩個候選(其中一個讀錯)時,
    只有正確的組合通過。有兩組以上都通過時:期限與金額都一樣(同一張帳單印了好幾組條碼)就不影響比對,
    取第一組;期限或金額不同(例如一頁有兩張帳單)就無法確定是哪一組,不比對。
    """
    buckets: dict[int, list[str]] = {9: [], 16: [], 15: []}
    for raw in texts:
        text = raw.strip().strip("*")   # 有的解碼器會連起訖字元一起回
        bucket = buckets.get(len(text))
        if bucket is not None and text not in bucket:
            bucket.append(text)
    firsts, seconds, thirds = buckets[9], buckets[16], buckets[15]
    if not texts:
        return [], "影像中找不到可以解碼的超商條碼"
    if not (firsts or seconds or thirds):
        return [], "有讀到條碼，但不是超商代收的三段條碼"
    missing = [name for name, bucket in (("一", firsts), ("二", seconds), ("三", thirds)) if not bucket]
    if missing:
        return [], f"只讀到三段條碼中的 {3 - len(missing)} 段（缺第{'、'.join(missing)}段），可能被折到、遮住或太模糊"
    valid: list[BillBarcode] = []
    reason = ""
    for combo in product(firsts, seconds, thirds):
        try:
            valid.append(_parse_triple(*combo))
        except ValueError as exc:
            reason = str(exc)
    if not valid:
        return [], f"三段條碼都讀到了，但{reason}，不拿來比對"
    if len({(bb.due_date, bb.pay_date, bb.amount) for bb in valid}) > 1:
        return valid, "影像中有好幾組不同的三段條碼，不知道哪一組是這張帳單的，不比對"
    return valid[:1], ""


def parse_store_barcodes(texts: list[str]) -> BillBarcode | None:
    """從解出的字串辨認一組三段條碼;校對碼不符、少一段、有好幾組不同的條碼都回 None(不當證據)。"""
    valid, _ = _scan(texts)
    return valid[0] if len(valid) == 1 else None


# --- 影像解碼 ---

def _decode_zxing(image) -> list[str]:
    """用 zxing-cpp 解 Code 39(單一影像、不做前處理);沒安裝或解碼出錯都回空清單。"""
    try:
        import zxingcpp
    except ImportError:
        return []
    try:
        codes = zxingcpp.read_barcodes(image, formats=zxingcpp.BarcodeFormat.Code39)
    except Exception as exc:
        log.info("zxing 解碼 Code 39 出錯:%s", type(exc).__name__)
        return []
    return [code.text for code in codes if code.text]


def _shrink(gray, longest: int):
    """等比例縮小到長邊 longest 像素。"""
    import cv2

    scale = longest / max(gray.shape[:2])
    return cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)


def _flatten(gray):
    """平場校正:除以大範圍模糊出來的背景亮度,抹平陰影與照明不均(手機照片常拍到手或機身的影子)。

    背景在縮小 8 倍的影像上估計再放大,12 百萬像素的照片也只要幾十毫秒。
    """
    import cv2

    h, w = gray.shape[:2]
    small = cv2.resize(gray, (max(1, w // 8), max(1, h // 8)), interpolation=cv2.INTER_AREA)
    background = cv2.GaussianBlur(small, (0, 0), max(small.shape[:2]) / 20)
    return cv2.divide(gray, cv2.resize(background, (w, h), interpolation=cv2.INTER_LINEAR), scale=255)


def _rotate(gray, angle: float):
    """把灰階影像繞中心轉 angle 度(OpenCV 的正角度是逆時針),畫布放大到不裁掉,空出來的地方補白。"""
    import cv2

    h, w = gray.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    new_w, new_h = int(h * sin + w * cos), int(h * cos + w * sin)
    matrix[0, 2] += new_w / 2 - w / 2
    matrix[1, 2] += new_h / 2 - h / 2
    return cv2.warpAffine(gray, matrix, (new_w, new_h), flags=cv2.INTER_LINEAR, borderValue=255)


def _sharpen(gray):
    import cv2

    return cv2.addWeighted(gray, 2.5, cv2.GaussianBlur(gray, (0, 0), 2), -1.5, 0)


def _variants(image):
    """依序產生較難解的前處理版本:原圖 → 平場校正(陰影)→ 銳化(模糊)→ 放大(解析度不足)→ 每隔 8 度轉一次(條碼歪斜)。

    後面幾種要 OpenCV;沒有 OpenCV 就只有原圖。
    - 傾斜:zxing 掃的是橫向的掃描線,條碼歪超過「條碼高度 ÷ 條碼長度」就讀不出來(合成影像實測 6~9 度),
      所以每隔 8 度轉一次、讓任何傾斜離某個角度都不到 4 度,靠「讀得出合法的三段條碼」驗證轉對了(實測可救回 ±35 度)。
      曾試過用 cv2.barcode 偵測條碼的位置與方向來估傾斜:乾淨的影像上準,但頁面有真正的文字時,
      歪 10 度以上就找不到(文字的筆畫像條碼),所以不用。
    - 陰影:平場校正(除以大範圍模糊出來的背景亮度)把「條碼那一側暗到 35% 以下」「局部陰影」從讀不到變成讀得到;
      單純拉對比(min-max)與 CLAHE 救不了。但局部的亮斑(反光)用平場校正會被抹平,所以拉對比的版本也留著。
    """
    yield image
    try:
        import cv2
        import numpy as np
    except ImportError:
        return
    if hasattr(image, "convert"):   # PIL 影像:條碼只看亮度
        gray = np.asarray(image.convert("L"))
    else:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    if max(gray.shape[:2]) > MAX_EDGE:   # 超大的圖(不是手機照片的尺寸)先縮小,不讓一張圖吃掉好幾倍記憶體
        gray = _shrink(gray, MAX_EDGE)
    stretched = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    flat = cv2.normalize(_flatten(gray), None, 0, 255, cv2.NORM_MINMAX)
    yield flat
    yield _sharpen(stretched)
    yield _sharpen(flat)
    if max(gray.shape[:2]) < 3000:   # 夠大的照片不缺解析度,放大只是浪費時間
        for base in (stretched, flat):
            yield cv2.resize(base, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    if max(flat.shape[:2]) > ROTATE_MAX_EDGE:   # 5 千萬像素的照片轉八次太慢;縮到約 1 千 6 百萬像素,A4 上的細條還有 2 像素以上
        flat = _shrink(flat, ROTATE_MAX_EDGE)
    for angle in TILT_ANGLES:
        yield _rotate(flat, angle)


def decode_bill_barcodes(image) -> list[str]:
    """回傳影像中所有解得出的 Code 39 字串;一湊齊合法的三段條碼就停止嘗試更多前處理。

    image 是 OpenCV 影像(einvoice.load_image 的結果)或 PIL 影像。不同的前處理可能各解出不同的段,
    所以累積每一次解出的字串。沒安裝 zxing-cpp、沒有 OpenCV 或解碼出錯,回已解出的部分(最差是空清單)。
    耗時:讀得到時多半是第一次(約 20 毫秒);完全沒有條碼的頁面要試完所有版本(A4 300 DPI 約 0.1~0.3 秒)。
    """
    found: list[str] = []
    try:
        for variant in _variants(image):
            for text in _decode_zxing(variant):
                if text not in found:
                    found.append(text)
            if parse_store_barcodes(found) is not None:
                break
    except Exception as exc:   # 前處理壞掉只是少一次機會
        log.info("條碼前處理出錯:%s", type(exc).__name__)
    return found


# --- 與辨識結果比對 ---

def _compare_amount(bb: BillBarcode, result: ExtractionResult) -> dict[str, Any]:
    fields = ["amount"]
    if (result.currency or "NTD") != "NTD":
        return entry("skip", f"條碼的金額是新臺幣，讀到的幣別是 {result.currency}，不比對", fields)
    if bb.amount == 0:
        return entry("skip", "條碼的應繳金額是 0（沒有記載金額），不比對", fields)
    if result.amount is None:
        return entry("skip", f"沒有讀到帳單金額（條碼上的應繳金額是 {bb.amount:,} 元）", fields)
    if abs(float(result.amount) - bb.amount) < 0.005:
        return entry("pass", f"條碼上的應繳金額 {bb.amount:,} 元，和讀到的金額一樣", fields)
    # 只附條碼上的金額(給更正頁「改成這個」);銷帳編號與條碼原文一律不附
    return {**entry("fail", f"條碼上的應繳金額是 {bb.amount:,} 元，讀到的卻是 {float(result.amount):,g} 元", fields),
            "expected": {"amount": bb.amount}}


def _compare_due(bb: BillBarcode, result: ExtractionResult, grace_days: int) -> dict[str, Any]:
    fields = ["fields.due_date"]
    value = (result.fields or {}).get("due_date")
    if is_blank(value):
        return entry("skip", f"沒有讀到繳費期限（條碼上的代收期限是 {bb.due_date}）", fields)
    got = parse_iso_date(value)
    if got is None:
        return entry("skip", "讀到的繳費期限不是有效的日期，無法和條碼比對", fields)
    due = date.fromisoformat(bb.due_date)
    pay = date.fromisoformat(bb.pay_date) if bb.pay_date else None
    if got == due:
        return entry("pass", f"條碼上的代收期限 {due}，和讀到的繳費期限一樣", fields)
    if got == pay:
        return entry("pass", f"條碼上的應繳日期 {pay}（代收期限 {due}），和讀到的繳費期限一樣", fields)
    dates = [d for d in (due, pay) if d is not None]
    gap = min(abs((got - d).days) for d in dates)   # 離條碼上最近的日期差幾天
    extra = f"（應繳日期 {pay}）" if pay and pay != due else ""
    if gap <= grace_days:
        return entry("skip", f"條碼上的代收期限是 {due}{extra}，和讀到的期限 {got} 差 {gap} 天；"
                             "代收期限常比帳單印的繳費期限晚幾天到一個月，無法用條碼證實", fields)
    return entry("fail", f"條碼上的代收期限是 {due}{extra}，和讀到的期限 {got} 差 {gap} 天，"
                         "超過代收期限與繳費期限常見的差距", fields)


def compare_with_result(bb: BillBarcode, result: ExtractionResult,
                        grace_days: int = COLLECT_GRACE_DAYS) -> dict[str, dict[str, Any]]:
    """三段條碼與辨識結果比對,回傳 {檢查名稱: 驗證項目}。只判斷,不改 result。

    金額:相符 pass、不符 fail、模型沒讀到或條碼沒記載 skip。
    期限:和條碼的代收期限或應繳日期相符 pass;差在 grace_days 天內 skip(無法證實);差更多 fail(見模組說明)。
    """
    return {
        CHECK_BARCODE_AMOUNT: _compare_amount(bb, result),
        CHECK_BARCODE_DUE: _compare_due(bb, result, grace_days),
    }


def verify_bill_barcode(result: ExtractionResult, file_path: Path) -> dict[str, dict[str, Any]]:
    """讀原檔 → 解 Code 39 → 辨認三段 → 比對。找不到合法的三段條碼時回一筆 skip,不當成讀錯
    (很多帳單沒印超商條碼,或條碼被折到、蓋章、拍得太糊)。"""
    image = load_image(file_path)
    if image is None:
        return {CHECK_BARCODE: entry("skip", "讀不到原檔影像，無法解碼條碼", [])}
    texts = decode_bill_barcodes(image)
    valid, reason = _scan(texts)
    if not valid:
        # 掃描 PDF 的原解析度、照片找到紙張並拉正補光後再試(einvoice.alternate_images);湊不齊照舊回 skip
        for extra in alternate_images(file_path, image):
            texts += [text for text in decode_bill_barcodes(extra) if text not in texts]
            valid, reason = _scan(texts)
            if valid:
                break
    if len(valid) != 1:
        return {CHECK_BARCODE: entry("skip", reason, [])}
    return compare_with_result(valid[0], result)
