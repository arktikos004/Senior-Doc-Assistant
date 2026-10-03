"""電子發票 QR 驗證測試:規格範例解析、qrcode 產生影像 → OpenCV 解碼 → 與辨識結果比對。

QR 影像都在測試中以 qrcode 套件即時產生並寫進 tmp_path,不讀任何真實發票。
"""
from datetime import date
from pathlib import Path

import pytest
import qrcode
from PIL import Image

from src.models import ExtractionResult
from src.verify import einvoice

# 財政部〈電子發票證明聯一維及二維條碼規格說明〉v1.9 第貳章四(一)的範例左方 QR
SPEC_LEFT = ("AB112233441020523999900000144000001540000000001234567ydXZt4LAN1U"
             "HN/j1juVcRA==:**********:3:3:0:乾電池:1:105:")
# 同文件附錄「一維及二維條碼檢查表」的 77 碼範例
SPEC_CHECKLIST = "QQ000815241000801396600000014000000141234567828433892qk90D8qgCwuEvOngCZaEdaw="

# 本測試用的合成發票(統編 04595257 取自財政部檢查碼說明的範例號碼)
ISSUED = date(2026, 7, 4)
LEFT = einvoice.build_left_qr(
    "ZX10293847", ISSUED, "5847", 333, 350, "00000000", "04595257",
    "SyntheticTestOnly0000A==", tail=":**********:2:2:1:鮮乳:2:90:")
RIGHT = "**吐司:1:170"


# --- 解析器 ---

def test_parse_spec_example_fields():
    qr = einvoice.parse_left_qr(SPEC_LEFT)
    assert qr.invoice_number == "AB11223344"
    assert qr.roc_date == "1020523"
    assert qr.date == "2013-05-23"          # 民國 102 年 → 西元 2013 年
    assert qr.random_code == "9999"
    assert qr.sales_amount == 0x144 == 324   # 16 進位
    assert qr.total_amount == 0x154 == 340
    assert qr.buyer_tax_id == "00000000"
    assert qr.seller_tax_id == "01234567"
    assert qr.encrypted == "ydXZt4LAN1UHN/j1juVcRA=="


def test_parse_checklist_example_exactly_77_chars():
    assert len(SPEC_CHECKLIST) == einvoice.LEFT_QR_LENGTH
    qr = einvoice.parse_left_qr(SPEC_CHECKLIST)
    assert (qr.invoice_number, qr.date, qr.random_code) == ("QQ00081524", "2011-08-01", "3966")
    assert (qr.sales_amount, qr.total_amount) == (20, 20)
    assert (qr.buyer_tax_id, qr.seller_tax_id) == ("12345678", "28433892")


def test_parse_accepts_lowercase_hex_and_b2b_blank_random_code():
    # 規格第伍章原始碼用 ToString("x8") → 小寫;B2B 發票隨機碼為 4 個空白
    text = "AB11223344" "1150704" "    " "0000014d" "0000015e" "04595257" "10458575" + "A" * 24
    qr = einvoice.parse_left_qr(text)
    assert (qr.sales_amount, qr.total_amount) == (333, 350)
    assert qr.random_code == "    "


def test_parse_large_hex_amount():
    text = einvoice.build_left_qr("AB11223344", ISSUED, "0001", 0, 1_234_567,
                                  "00000000", "04595257", "B" * 24)
    assert text[21:37] == "00000000" "0012D687"
    assert einvoice.parse_left_qr(text).total_amount == 1_234_567


def test_build_then_parse_round_trip():
    qr = einvoice.parse_left_qr(LEFT)
    assert qr.invoice_number == "ZX10293847"
    assert qr.roc_date == "1150704" and qr.date == "2026-07-04"
    assert (qr.sales_amount, qr.total_amount) == (333, 350)
    assert qr.raw == LEFT


@pytest.mark.parametrize("text", [
    SPEC_LEFT[:76],                                    # 不足 77 碼
    RIGHT,                                             # 右側 QR
    "ab" + SPEC_LEFT[2:],                              # 字軌必須大寫
    SPEC_LEFT.replace("1020523", "1020230", 1),        # 2 月 30 日不存在
    SPEC_LEFT.replace("00000144", "0000014G", 1),      # 非 16 進位
    "https://example.com/" + "x" * 80,                 # 一般網址 QR
])
def test_parse_rejects_non_left_qr(text):
    with pytest.raises(ValueError):
        einvoice.parse_left_qr(text)


def test_find_left_qr_ignores_right_and_foreign_codes():
    assert einvoice.find_left_qr([RIGHT, "https://example.com", LEFT]).invoice_number == "ZX10293847"
    assert einvoice.find_left_qr([RIGHT]) is None
    assert einvoice.find_left_qr([]) is None


# --- 比對 ---

def _result(**overrides) -> ExtractionResult:
    base = dict(doc_type="發票", date="2026-07-04", vendor="合成測試商店", amount=350.0,
                invoice_number="ZX-10293847", confidence=0.9,
                fields={"seller_tax_id": "04595257", "random_code": "5847", "buyer_tax_id": ""})
    base.update(overrides)
    return ExtractionResult(**base)


def test_compare_all_match():
    checks = einvoice.compare_with_result(einvoice.parse_left_qr(LEFT), _result())
    assert checks[einvoice.CHECK_QR_INVOICE]["status"] == "pass"   # 連字號正規化後相符
    assert checks[einvoice.CHECK_QR_DATE]["status"] == "pass"
    assert checks[einvoice.CHECK_QR_TOTAL]["status"] == "pass"
    assert checks[einvoice.CHECK_QR_SELLER]["status"] == "pass"
    assert checks[einvoice.CHECK_QR_RANDOM]["status"] == "pass"
    assert checks[einvoice.CHECK_QR_BUYER]["status"] == "skip"     # 一般消費者
    assert checks[einvoice.CHECK_QR_TOTAL]["fields"] == ["amount"]


def test_compare_amount_mismatch_fails_with_both_values_in_detail():
    checks = einvoice.compare_with_result(einvoice.parse_left_qr(LEFT), _result(amount=530.0))
    total = checks[einvoice.CHECK_QR_TOTAL]
    assert total["status"] == "fail"
    assert "350" in total["detail"] and "530" in total["detail"]


def test_compare_missing_model_values_skip_and_show_qr_value():
    checks = einvoice.compare_with_result(
        einvoice.parse_left_qr(LEFT), _result(invoice_number=None, amount=None, fields={}))
    assert checks[einvoice.CHECK_QR_INVOICE]["status"] == "skip"
    assert "ZX10293847" in checks[einvoice.CHECK_QR_INVOICE]["detail"]
    assert checks[einvoice.CHECK_QR_TOTAL]["status"] == "skip"
    assert checks[einvoice.CHECK_QR_SELLER]["status"] == "skip"


def test_compare_skips_total_for_cross_border_zero():
    text = einvoice.build_left_qr("AB11223344", ISSUED, "1234", 0, 0, "00000000", "04595257", "C" * 24)
    checks = einvoice.compare_with_result(einvoice.parse_left_qr(text), _result())
    assert checks[einvoice.CHECK_QR_TOTAL]["status"] == "skip"


def test_compare_does_not_modify_result():
    result = _result(invoice_number="ZX-10293847", amount=530.0)
    before = result.to_dict()
    einvoice.compare_with_result(einvoice.parse_left_qr(LEFT), result)
    assert result.to_dict() == before


def test_code39_is_skip():
    assert einvoice.code39_entry()["status"] == "skip"


# --- 端到端:qrcode 產生影像 → OpenCV 解碼 → 比對 ---

def _qr(text: str, box: int) -> Image.Image:
    # 規格:左方 QR 使用 V6 以上、容錯 Level L 以上
    code = qrcode.QRCode(version=6, error_correction=qrcode.constants.ERROR_CORRECT_L,
                         box_size=box, border=4)
    code.add_data(text.encode("utf-8"))
    code.make(fit=True)
    return code.make_image(fill_color="black", back_color="white").convert("RGB")


def _invoice_image(left: str = LEFT, right: str | None = RIGHT, box: int = 4) -> Image.Image:
    """仿證明聯版面:左右兩個 QR 上緣對齊、大小一致,周圍留白。"""
    a = _qr(left, box)
    size = a.width
    page = Image.new("RGB", (size * 2 + 120, size + 240), "white")
    page.paste(a, (40, 160))
    if right is not None:
        page.paste(_qr(right, box).resize((size, size), Image.NEAREST), (size + 80, 160))
    return page


def test_opencv_decodes_both_qr_and_picks_left(tmp_path):
    path = tmp_path / "發票.png"
    _invoice_image().save(path)
    texts = einvoice.decode_qr_texts(einvoice.load_image(path))
    assert LEFT in texts
    qr = einvoice.find_left_qr(texts)
    assert qr.invoice_number == "ZX10293847" and qr.total_amount == 350


def test_end_to_end_match_is_pass(tmp_path):
    path = tmp_path / "合成發票.png"
    _invoice_image().save(path)
    checks = einvoice.verify_einvoice(_result(), path)
    for name in (einvoice.CHECK_QR_INVOICE, einvoice.CHECK_QR_DATE, einvoice.CHECK_QR_TOTAL):
        assert checks[name]["status"] == "pass", checks[name]


def test_end_to_end_mismatch_is_fail(tmp_path):
    path = tmp_path / "合成發票.jpg"
    _invoice_image().save(path, quality=90)
    checks = einvoice.verify_einvoice(_result(amount=380.0, date="2026-07-14"), path)
    assert checks[einvoice.CHECK_QR_TOTAL]["status"] == "fail"
    assert checks[einvoice.CHECK_QR_DATE]["status"] == "fail"
    assert checks[einvoice.CHECK_QR_INVOICE]["status"] == "pass"


def test_end_to_end_pdf_rendered_at_higher_scale(tmp_path):
    path = tmp_path / "合成發票.pdf"
    _invoice_image(box=3).save(path, "PDF", resolution=150)
    checks = einvoice.verify_einvoice(_result(), path)
    assert checks[einvoice.CHECK_QR_TOTAL]["status"] == "pass"


def test_no_qr_or_unreadable_file_is_skip(tmp_path):
    blank = tmp_path / "空白.png"
    Image.new("RGB", (400, 300), "white").save(blank)
    assert einvoice.verify_einvoice(_result(), blank) == {
        einvoice.CHECK_QR: {"status": "skip", "detail": "影像中找不到可解碼的電子發票 QR Code",
                            "fields": []}}
    fake = tmp_path / "壞檔.png"
    fake.write_bytes(b"fake image bytes")
    assert einvoice.verify_einvoice(_result(), fake)[einvoice.CHECK_QR]["status"] == "skip"
    missing = tmp_path / "不存在.png"
    assert einvoice.verify_einvoice(_result(), missing)[einvoice.CHECK_QR]["status"] == "skip"


def test_foreign_qr_is_skip_not_fail(tmp_path):
    path = tmp_path / "網址.png"
    _invoice_image(left="https://example.com/menu", right=None).save(path)
    entry = einvoice.verify_einvoice(_result(), path)[einvoice.CHECK_QR]
    assert entry["status"] == "skip" and "不是電子發票左側 QR" in entry["detail"]
