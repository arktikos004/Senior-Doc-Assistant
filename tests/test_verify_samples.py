"""合成電子發票樣本(tools/make_sample.py --einvoice)的端到端測試。

樣本只寫進 tmp_path;驗證:每張(含模糊、旋轉 ±7°、褪色、JPEG 壓縮)的 QR 都解得出來且與標準答案一致,
統編都通過檢查碼,「印刷金額 ≠ QR」樣本會被驗證攔截。
"""
import csv
import importlib.util
import random
import sys
from datetime import date
from pathlib import Path

import pytest

from src.config import AppConfig
from src.decision import decide
from src.models import ExtractionResult
from src.verify import einvoice, run_verification
from src.verify.checks import parse_period, tax_id_checksum_ok

_SPEC = importlib.util.spec_from_file_location(
    "tools_make_sample", Path(__file__).resolve().parent.parent / "tools" / "make_sample.py")
make_sample = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = make_sample   # dataclass 需要在 sys.modules 找得到所屬模組
_SPEC.loader.exec_module(make_sample)

TODAY = date(2026, 9, 30)


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    out = tmp_path_factory.mktemp("einvoice")
    rows = make_sample.make_einvoices(out, count=5, seed=7)
    return out, rows


def test_writes_only_to_given_folder_with_labels(samples):
    out, rows = samples
    assert len(rows) == 6   # 5 張輪流劣化 + 1 張金額不符
    names = {p.name for p in out.iterdir()}
    assert names == {r["檔名"] for r in rows} | {"labels.csv"}
    with open(out / "labels.csv", encoding="utf-8-sig", newline="") as f:
        labels = list(csv.DictReader(f))
    assert [r["檔名"] for r in labels] == [r["檔名"] for r in rows]
    assert {r["degrade"] for r in rows} == {"", "模糊", "旋轉", "褪色", "壓縮"}
    assert any(name.endswith(".jpg") for name in names)


def test_synthetic_data_is_fictional_and_valid(samples):
    _, rows = samples
    for row in rows:
        assert "示範" in row["vendor"]                 # 虛構店名
        assert tax_id_checksum_ok(row["seller_tax_id"])
        assert parse_period(row["period"]) is not None


@pytest.mark.parametrize("index", range(6))
def test_every_sample_qr_decodes_and_matches_label(samples, index):
    out, rows = samples
    row = rows[index]
    texts = einvoice.decode_qr_texts(einvoice.load_image(out / row["檔名"]))
    qr = einvoice.find_left_qr(texts)
    assert qr is not None, f"{row['檔名']} 的左側 QR 解不出來"
    assert qr.invoice_number == row["invoice_number"]
    assert qr.date == row["date"]
    assert qr.total_amount == int(row["amount"])
    assert qr.seller_tax_id == row["seller_tax_id"]
    assert qr.random_code == row["random_code"]
    assert any(t.startswith("**") for t in texts) or row["degrade"], "右側 QR 應以 ** 開頭"


def _read_as_printed(row: dict) -> ExtractionResult:
    """模擬模型照印刷讀出所有欄位(金額不符樣本就會讀到錯的印刷金額)。"""
    return ExtractionResult(
        doc_type="發票", date=row["date"], vendor=row["vendor"], amount=float(row["printed_total"]),
        invoice_number=row["invoice_number"], confidence=0.9,
        fields={"seller_tax_id": row["seller_tax_id"], "random_code": row["random_code"],
                "period": row["period"]})


def test_clean_sample_verifies_and_mismatch_sample_is_caught(samples):
    out, rows = samples
    clean = next(r for r in rows if not r["degrade"] and not r["note"])
    result = _read_as_printed(clean)
    run_verification(result, out / clean["檔名"], TODAY)
    assert result.verified_confidence >= 0.95
    assert decide(result, AppConfig()).action == "archive"

    bad = next(r for r in rows if r["note"])
    assert int(bad["printed_total"]) != int(bad["qr_total"])
    result = _read_as_printed(bad)
    run_verification(result, out / bad["檔名"], TODAY)
    assert result.verification[einvoice.CHECK_QR_TOTAL]["status"] == "fail"
    assert result.verified_confidence <= 0.30
    assert decide(result, AppConfig()).action == "review"


def test_fake_tax_ids_pass_checksum():
    rng = random.Random(1)
    ids = {make_sample.fake_tax_id(rng) for _ in range(30)}
    assert all(tax_id_checksum_ok(x) for x in ids) and len(ids) > 20


def test_period_code_matches_bimonthly_period():
    spec = make_sample._rand_einvoice(1, random.Random(3), None)
    year, start, end = parse_period(spec.period)
    assert parse_period(spec.period_code) == (year, start, end)
    assert start <= spec.issued.month <= end
