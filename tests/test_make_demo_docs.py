"""Demo 合成文件產生器測試:能產出三張影像、都是虛構內容,日期由展示日推算,電費單的條碼讀得出來。"""
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import make_demo_docs  # noqa: E402
from src.dates import resolve_deadline  # noqa: E402


def test_generates_all_demo_docs(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["make_demo_docs.py", "--out", str(tmp_path)])
    assert make_demo_docs.main() == 0
    names = sorted(p.name for p in tmp_path.glob("*.png"))
    assert names == sorted(make_demo_docs.DOCS)
    for p in tmp_path.glob("*.png"):
        with Image.open(p) as img:
            assert img.width >= 1000 and img.height >= 1400


def test_all_names_are_marked_as_sample():
    # 虛構文件的檔名一律帶「範例」,避免和真實文件混在一起
    assert all(name.startswith("範例") for name in make_demo_docs.DOCS)


@pytest.mark.parametrize("as_of", [date(2026, 11, 7), date(2026, 3, 1), date(2027, 1, 2)])
def test_dates_keep_the_reminders_on_the_home_page_on_the_demo_day(as_of):
    """文件日期不晚於展示日(核對允許到隔天),期限在展示日或之後(首頁濾掉過期的提醒);跨月、跨年也成立。"""
    d = make_demo_docs.demo_dates(as_of)
    for key in ("bill_issued", "dispensed", "letter_issued", "letter_applied"):
        assert d[key] <= as_of, key
    assert d["bill_due"] >= as_of and d["bill_issued"] < d["bill_due"]
    start, end = d["bill_period"]
    assert start.day == 1 and end + timedelta(days=1) == d["bill_issued"].replace(day=1) and start < end
    letter_due = resolve_deadline("收到本函後15日內", d["letter_issued"])
    assert letter_due is not None and letter_due >= as_of
    assert d["letter_applied"] < d["letter_issued"]


def test_as_of_changes_the_printed_dates(tmp_path):
    a = make_demo_docs.generate(tmp_path / "a", date(2026, 11, 7))
    b = make_demo_docs.generate(tmp_path / "b", date(2026, 12, 1))
    for pa, pb in zip(a, b):
        assert pa.name == pb.name and pa.read_bytes() != pb.read_bytes()


def test_diagonal_watermark_tints_without_hiding_text():
    """對角線浮水印只淡淡染色:太深會蓋住字,模型會把被蓋到的欄位當成讀不清(10/27 dev 評測)。
    左上角的紅色「範例」標籤照舊。"""
    import numpy as np
    page = make_demo_docs._watermark(Image.new("RGB", (1240, 1754), (252, 252, 250)))
    arr = np.asarray(page).astype(int)
    outside_badge = np.ones(arr.shape[:2], bool)
    outside_badge[:80, :210] = False
    assert arr[outside_badge].min() >= 230
    assert (arr[outside_badge].min(axis=1) < 250).sum() > 20_000          # 還看得到浮水印
    badge = arr[20:66, 20:190]
    assert ((badge[..., 0] > 150) & (badge[..., 1] < 60)).mean() > 0.5     # 紅標籤還在


def test_bill_barcode_decodes_with_the_systems_own_reader(tmp_path):
    """電費單的三段條碼:用 src.verify.billcode 從影像解得開,金額 1,286 元,應繳日期 = 繳費期限。"""
    from src.verify import billcode
    from src.verify.einvoice import load_image
    as_of = date(2026, 11, 7)
    bill = make_demo_docs.generate(tmp_path, as_of)[0]
    found = billcode.parse_store_barcodes(billcode.decode_bill_barcodes(load_image(bill)))
    due = make_demo_docs.demo_dates(as_of)["bill_due"].isoformat()
    assert found is not None and found.amount == make_demo_docs.BILL_AMOUNT
    assert found.pay_date == due and found.due_date == due
