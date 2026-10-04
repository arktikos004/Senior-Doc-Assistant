"""電子發票證明聯驗證:解碼左側 QR Code,與模型辨識結果逐欄比對。

QR 是營業人加值中心/POS 直接印上的機器資料,與模型怎麼「看」影像完全無關,
所以 QR 與模型一致是**獨立證據**(強證據),不一致則幾乎可以確定模型讀錯。

規格依據:財政部財政資訊中心〈電子發票證明聯一維及二維條碼規格說明〉v1.9(111 年 5 月)
https://www.einvoice.nat.gov.tw/static/ptl/ein_upload/attachments/1575448081679_0.pdf
- 第貳章二(一)「左方二維條碼記載事項」:前 77 碼依序為
  發票字軌號碼(10)、發票開立日期(7,民國 yyyMMdd)、隨機碼(4)、
  銷售額(8,十六進位,未稅;買受人非營業人且無法分離稅額時記 00000000)、
  總計額(8,十六進位,含稅)、買方統編(8,一般消費者記 00000000)、
  賣方統編(8)、加密驗證資訊(24,AES + Base64);之後以「:」接續營業人自行使用區、品目筆數等。
- 第貳章二(二):右方 QR 以「**」開頭,記載品項明細的延續(本模組不解析)。
- 第貳章三:境外電商的銷售額、總計額以 00000000 記載(此時無總計額可比對)。
- 第伍章參考原始碼以 ToString("x8") 產生金額,十六進位可能是小寫,解析時大小寫都接受。
- 第貳章五 6.:B2B 發票的隨機碼為 4 個空白。
- 第壹章:一維條碼為 Code 39(年期別 5 + 字軌 10 + 隨機碼 4 = 19 碼)。實測 OpenCV 5.0 的
  cv2.barcode 只解 EAN/UPC:Code 39 可以定位但解不出內容,因此一維條碼一律回 skip,不另加依賴。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..models import ExtractionResult
from .checks import CONSUMER_TAX_ID, entry, is_blank, normalize_invoice_number, parse_iso_date
from .logname import log_label

log = logging.getLogger(__name__)

# 檢查名稱(result.verification 的鍵)。INDEPENDENT_CHECKS 為獨立證據,confidence 公式據此分級。
CHECK_QR = "電子發票 QR"
CHECK_QR_INVOICE = "QR 字軌號碼"
CHECK_QR_DATE = "QR 開立日期"
CHECK_QR_TOTAL = "QR 總計額"
CHECK_QR_SELLER = "QR 賣方統編"
CHECK_QR_BUYER = "QR 買方統編"
CHECK_QR_RANDOM = "QR 隨機碼"
CHECK_CODE39 = "一維條碼"
INDEPENDENT_CHECKS = frozenset({
    CHECK_QR_INVOICE, CHECK_QR_DATE, CHECK_QR_TOTAL,
    CHECK_QR_SELLER, CHECK_QR_BUYER, CHECK_QR_RANDOM,
})

LEFT_QR_LENGTH = 77
PDF_RENDER_SCALE = 4.0   # 約 288 DPI;送模型用 2 倍即可,但 QR 模組要更細才解得出來

_LEFT_QR = re.compile(
    r"(?P<invoice_number>[A-Z]{2}\d{8})"
    r"(?P<roc_date>\d{7})"
    r"(?P<random_code>\d{4}| {4})"
    r"(?P<sales>[0-9A-Fa-f]{8})"
    r"(?P<total>[0-9A-Fa-f]{8})"
    r"(?P<buyer>\d{8})"
    r"(?P<seller>\d{8})"
    r"(?P<encrypted>.{24})",
    re.DOTALL,
)


@dataclass(frozen=True)
class LeftQR:
    """左側 QR 前 77 碼的解析結果(金額已由十六進位轉為整數、日期已轉西元)。"""

    invoice_number: str
    roc_date: str          # 原文 yyyMMdd(民國)
    date: str              # 西元 YYYY-MM-DD
    random_code: str       # B2B 發票為 4 個空白
    sales_amount: int      # 未稅銷售額;0 表示無法分離稅額
    total_amount: int      # 含稅總計額;0 表示未記載(境外電商)
    buyer_tax_id: str      # 00000000 = 一般消費者
    seller_tax_id: str
    encrypted: str         # 加密驗證資訊(需營業人金鑰才能驗,本系統不驗)
    raw: str


def parse_left_qr(text: str) -> LeftQR:
    """解析左側 QR 字串;不是合法的左側 QR(長度、欄位格式或日期不對)丟 ValueError。"""
    if not isinstance(text, str) or len(text) < LEFT_QR_LENGTH:
        raise ValueError("不足 77 碼,不是電子發票左側 QR")
    m = _LEFT_QR.match(text)
    if not m:
        raise ValueError("前 77 碼的欄位格式不符財政部規格")
    roc = m["roc_date"]
    issued = date(int(roc[:3]) + 1911, int(roc[3:5]), int(roc[5:7]))  # 不存在的日期丟 ValueError
    return LeftQR(
        invoice_number=m["invoice_number"],
        roc_date=roc,
        date=issued.isoformat(),
        random_code=m["random_code"],
        sales_amount=int(m["sales"], 16),
        total_amount=int(m["total"], 16),
        buyer_tax_id=m["buyer"],
        seller_tax_id=m["seller"],
        encrypted=m["encrypted"],
        raw=text,
    )


def build_left_qr(
    invoice_number: str, issued: date, random_code: str, sales_amount: int, total_amount: int,
    buyer_tax_id: str, seller_tax_id: str, encrypted: str, tail: str = "",
) -> str:
    """依規格組出左側 QR 字串(合成樣本與測試用);tail 為第 77 碼之後以「:」起頭的延伸資訊。"""
    roc_date = f"{issued.year - 1911:03d}{issued.month:02d}{issued.day:02d}"
    text = (f"{invoice_number}{roc_date}{random_code}{sales_amount:08X}{total_amount:08X}"
            f"{buyer_tax_id}{seller_tax_id}{encrypted:<24}")
    if len(text) != LEFT_QR_LENGTH:
        raise ValueError(f"欄位長度不符規格,組出 {len(text)} 碼")
    return text + tail


# --- 影像讀取與解碼 ---

def load_image(file_path: Path):
    """讀原檔成 OpenCV 影像(BGR ndarray);PDF 以較高倍率渲染第一頁。讀不到回 None。"""
    try:
        import cv2
        import numpy as np
    except ImportError:
        log.warning("未安裝 opencv-python-headless,略過 QR 驗證")
        return None
    try:
        if file_path.suffix.lower() == ".pdf":
            import pypdfium2 as pdfium

            pdf = pdfium.PdfDocument(str(file_path))
            try:
                pil_image = pdf[0].render(scale=PDF_RENDER_SCALE).to_pil().convert("RGB")
            finally:
                pdf.close()
            return cv2.cvtColor(np.asarray(pil_image), cv2.COLOR_RGB2BGR)
        # np.fromfile + imdecode:cv2.imread 在 Windows 讀不了中文路徑
        data = np.fromfile(str(file_path), dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
    except Exception as exc:  # 檔案損毀、不是影像
        log.info("QR 驗證讀不到影像:%s(%s)", log_label(file_path), type(exc).__name__)
        return None


def _variants(image):
    """依序產生較難解的前處理版本:原圖 → 拉對比(褪色)→ 銳化(模糊)→ 二值化 → 放大(太小)。"""
    import cv2

    yield image
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    longest = max(gray.shape[:2])
    if longest > 2400:   # 手機原圖太大時偵測器反而找不到定位點,先縮小
        scale = 1600 / longest
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        yield gray
    norm = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    yield norm
    sharp = cv2.addWeighted(norm, 1.8, cv2.GaussianBlur(norm, (0, 0), 2), -0.8, 0)
    yield sharp
    yield cv2.threshold(norm, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    if longest < 2000:
        yield cv2.resize(sharp, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)


def _decode_once(image) -> list[str]:
    """用 OpenCV 兩種 QR 偵測器各試一次;以位元組解碼,前 77 碼是 ASCII,Big5 品名不影響。"""
    import cv2

    texts: list[str] = []
    for detector in (cv2.QRCodeDetector(), cv2.QRCodeDetectorAruco()):
        try:
            ok, decoded, _, _ = detector.detectAndDecodeBytesMulti(image)
        except cv2.error:
            continue
        if ok:
            for raw in decoded:
                text = bytes(raw).decode("utf-8", errors="replace") if raw is not None else ""
                if text:
                    texts.append(text)
    return texts


def decode_qr_texts(image) -> list[str]:
    """回傳影像中所有解得出的 QR 字串;一找到合法的左側 QR 就停止嘗試更多前處理。"""
    found: list[str] = []
    for variant in _variants(image):
        for text in _decode_once(variant):
            if text not in found:
                found.append(text)
        if any(_try_parse(t) for t in found):
            break
    return found


def _try_parse(text: str) -> LeftQR | None:
    try:
        return parse_left_qr(text)
    except ValueError:
        return None


def find_left_qr(texts: list[str]) -> LeftQR | None:
    """從解出的字串中挑出左側 QR(右側以「**」開頭,不是左側)。"""
    for text in texts:
        if not text.startswith("**"):
            qr = _try_parse(text)
            if qr is not None:
                return qr
    return None


# --- 與模型結果比對 ---

def _compare(label: str, field_name: str, qr_value: str, model_value: Any,
             normalize=lambda v: str(v).strip()) -> dict[str, Any]:
    if is_blank(model_value):
        return entry("skip", f"模型沒有讀到{label}(QR 記載 {qr_value})", [field_name])
    got = normalize(model_value)
    if got == qr_value:
        return entry("pass", f"QR 記載{label} {qr_value},與辨識結果相符", [field_name])
    return entry("fail", f"QR 記載{label} {qr_value},辨識結果為 {got}", [field_name])


def compare_with_result(qr: LeftQR, result: ExtractionResult) -> dict[str, dict[str, Any]]:
    """左側 QR 與辨識結果逐欄比對,回傳 {檢查名稱: 驗證項目}。只判斷,不改 result。"""
    fields = result.fields or {}
    out: dict[str, dict[str, Any]] = {}

    out[CHECK_QR_INVOICE] = _compare("發票號碼", "invoice_number", qr.invoice_number,
                                     result.invoice_number, normalize_invoice_number)

    parsed = parse_iso_date(result.date)
    out[CHECK_QR_DATE] = _compare("開立日期", "date", qr.date,
                                  parsed.isoformat() if parsed else result.date)

    if qr.total_amount == 0:
        out[CHECK_QR_TOTAL] = entry("skip", "QR 未記載總計額(境外電商以 00000000 記載)", ["amount"])
    elif result.amount is None:
        out[CHECK_QR_TOTAL] = entry("skip", f"模型沒有讀到金額(QR 記載總計 {qr.total_amount})",
                                    ["amount"])
    elif abs(float(result.amount) - qr.total_amount) < 0.005:
        out[CHECK_QR_TOTAL] = entry("pass", f"QR 記載總計 {qr.total_amount},與辨識金額相符",
                                    ["amount"])
    else:
        out[CHECK_QR_TOTAL] = entry(
            "fail", f"QR 記載總計 {qr.total_amount},辨識金額為 {float(result.amount):g}", ["amount"])

    out[CHECK_QR_SELLER] = _compare("賣方統編", "fields.seller_tax_id", qr.seller_tax_id,
                                    fields.get("seller_tax_id"))

    buyer = fields.get("buyer_tax_id")
    if qr.buyer_tax_id == CONSUMER_TAX_ID and (is_blank(buyer) or str(buyer).strip() == CONSUMER_TAX_ID):
        out[CHECK_QR_BUYER] = entry("skip", "買方為一般消費者,沒有買方統編可比對",
                                    ["fields.buyer_tax_id"])
    else:
        out[CHECK_QR_BUYER] = _compare("買方統編", "fields.buyer_tax_id", qr.buyer_tax_id, buyer)

    if not qr.random_code.strip():
        out[CHECK_QR_RANDOM] = entry("skip", "B2B 發票的隨機碼為空白,不比對", ["fields.random_code"])
    else:
        out[CHECK_QR_RANDOM] = _compare("隨機碼", "fields.random_code", qr.random_code,
                                        fields.get("random_code"))
    return out


def code39_entry() -> dict[str, Any]:
    """一維條碼:OpenCV 不支援 Code 39,固定 skip(見模組說明)。"""
    return entry("skip", "OpenCV 的條碼模組不支援 Code 39,未檢查;其中的字軌與隨機碼已由 QR 比對涵蓋",
                 ["fields.period", "invoice_number", "fields.random_code"])


def verify_einvoice(result: ExtractionResult, file_path: Path) -> dict[str, dict[str, Any]]:
    """讀原檔 → 解 QR → 比對。找不到左側 QR 時回一筆 skip,不當成讀錯
    (可能是傳統發票、QR 被裁掉或拍得太糊)。"""
    image = load_image(file_path)
    if image is None:
        return {CHECK_QR: entry("skip", "讀不到原檔影像,無法解碼 QR", [])}
    texts = decode_qr_texts(image)
    qr = find_left_qr(texts)
    if qr is None:
        detail = ("有解出 QR,但不是電子發票左側 QR(前 77 碼格式不符)" if texts
                  else "影像中找不到可解碼的電子發票 QR Code")
        return {CHECK_QR: entry("skip", detail, [])}
    return compare_with_result(qr, result)
