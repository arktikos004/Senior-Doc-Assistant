"""確定性檢查的單元測試:統編檢查碼、字軌格式、日期/期別/繳費期限、金額。

統編測例取自財政部財政資訊中心〈營利事業統一編號檢查碼邏輯修正說明〉附件的官方範例。
"""
from datetime import date

import pytest

from src.verify import checks

TODAY = date(2026, 9, 30)


# --- 統一編號檢查碼 ---

@pytest.mark.parametrize("tax_id", [
    "04595257",  # 官方範例:Z=40,新舊規則皆通過
    "04595252",  # 官方範例:Z=35,只有 112 年起「可被 5 整除」才通過
    "10458575",  # 官方範例:第 7 位為 7,Z2=20
    "10458574",  # 官方範例:第 7 位為 7,Z1=20
    "10458570",  # 官方範例:第 7 位為 7,新規則 Z2=15
])
def test_valid_tax_ids(tax_id):
    assert checks.tax_id_checksum_ok(tax_id)


@pytest.mark.parametrize("tax_id", [
    "04595253",   # Z=36
    "10458571",   # 第 7 位為 7:Z1=17、Z2=16,兩者都不能被 5 整除
    "12345678",   # 第 7 位為 7:Z1=33、Z2=32
    "04595250",   # Z=33
    "0459525",    # 只有 7 碼
    "045952570",  # 9 碼
    "0459525A",   # 含字母
    "",
    None,
])
def test_invalid_tax_ids(tax_id):
    assert not checks.tax_id_checksum_ok(tax_id)


def test_2023_rule_change_accepts_numbers_old_rule_rejected():
    # 舊規則(可被 10 整除)會擋掉 04595252;112/4/1 起的新規則要放行
    assert checks.tax_id_checksum_ok("04595252")
    assert checks.tax_id_checksum_ok("04595252", divisor=10) is False
    assert checks.tax_id_checksum_ok("04595257", divisor=10) is True


def test_check_tax_id_entry():
    ok = checks.check_tax_id("04595257", "fields.seller_tax_id", "賣方統編")
    assert ok["status"] == "pass" and ok["fields"] == ["fields.seller_tax_id"]
    bad = checks.check_tax_id("04595253", "fields.seller_tax_id", "賣方統編")
    assert bad["status"] == "fail" and "04595253" in bad["detail"]
    assert checks.check_tax_id("", "fields.seller_tax_id", "賣方統編")["status"] == "skip"
    assert checks.check_tax_id(None, "fields.seller_tax_id", "賣方統編")["status"] == "skip"
    # 買方為一般消費者時 QR/發票記載 00000000,不是統編,不檢查
    consumer = checks.check_tax_id("00000000", "fields.buyer_tax_id", "買方統編")
    assert consumer["status"] == "skip"


# --- 發票字軌 ---

@pytest.mark.parametrize("number, status", [
    ("AB12345678", "pass"),
    ("AB-12345678", "pass"),   # 證明聯印刷格式含連字號,正規化後比對
    ("ab 12345678", "pass"),
    ("A812345678", "fail"),    # 字母位置讀成數字
    ("AB1234567", "fail"),
    ("AB123456789", "fail"),
    (None, "skip"),
    ("", "skip"),
])
def test_invoice_number_format(number, status):
    assert checks.check_invoice_number_format(number)["status"] == status


# --- 日期合理性 ---

@pytest.mark.parametrize("text, status", [
    ("2026-09-30", "pass"),   # 今天
    ("2026-10-01", "pass"),   # 容許 1 天時差(時區、跨夜)
    ("2026-10-02", "fail"),   # 未來日期
    ("2000-01-01", "pass"),   # 下限當天
    ("1999-12-31", "fail"),   # 早於 2000 年
    ("2026-02-30", "fail"),   # 不存在的日期
    ("2026/09/30", "fail"),   # 格式不對
    (None, "skip"),
    ("", "skip"),
])
def test_date_plausible(text, status):
    entry = checks.check_date_plausible(text, today=TODAY)
    assert entry["status"] == status
    assert entry["fields"] == ["date"]


# --- 期別(雙月) ---

@pytest.mark.parametrize("text, expected", [
    ("115年07-08月", (2026, 7, 8)),
    ("115年7-8月", (2026, 7, 8)),
    ("115年 11 - 12 月", (2026, 11, 12)),
    ("114年01~02月", (2025, 1, 2)),
    ("10404", (2015, 3, 4)),       # 一維條碼的年期別寫法:民國年 + 雙數月
    ("115年08-09月", None),        # 期別一定是奇數月開頭
    ("115年07-09月", None),
    ("115年13-14月", None),
    ("11507", None),               # 一維條碼寫法只記雙數月
    ("七八月", None),
    ("", None),
])
def test_parse_period(text, expected):
    assert checks.parse_period(text) == expected


@pytest.mark.parametrize("day, status", [
    ("2026-07-01", "pass"),   # 期別第一天
    ("2026-08-31", "pass"),   # 期別最後一天
    ("2026-06-30", "fail"),   # 前一期最後一天
    ("2026-09-01", "fail"),   # 下一期第一天
    ("2025-07-04", "fail"),   # 月份對但年份錯(民國換算錯)
])
def test_date_in_period_boundaries(day, status):
    entry = checks.check_date_in_period(day, "115年07-08月")
    assert entry["status"] == status
    if status == "fail":
        assert set(entry["fields"]) == {"date", "fields.period"}


def test_date_in_period_skips_and_malformed_period():
    assert checks.check_date_in_period(None, "115年07-08月")["status"] == "skip"
    assert checks.check_date_in_period("2026-07-04", "")["status"] == "skip"
    # 期別本身格式錯:只算期別欄位讀錯,不牽連日期
    bad = checks.check_date_in_period("2026-07-04", "115年08-09月")
    assert bad["status"] == "fail" and bad["fields"] == ["fields.period"]


# --- 帳單繳費期限 ---

@pytest.mark.parametrize("due, doc_date, status", [
    ("2026-10-15", "2026-09-20", "pass"),
    ("2026-09-20", "2026-09-20", "pass"),   # 當天到期不算早於
    ("2026-09-19", "2026-09-20", "fail"),   # 期限早於出帳日
    ("2026-10-15", None, "pass"),            # 沒有出帳日時只檢查格式
    ("2026/10/15", "2026-09-20", "fail"),   # 格式不對(行事曆要 YYYY-MM-DD)
    ("2026-11-31", None, "fail"),            # 不存在的日期
    ("", "2026-09-20", "skip"),
    (None, None, "skip"),
])
def test_due_date(due, doc_date, status):
    entry = checks.check_due_date(due, doc_date)
    assert entry["status"] == status
    assert entry["fields"] == ["fields.due_date"]


# --- 金額 ---

@pytest.mark.parametrize("amount, currency, status", [
    (350.0, "NTD", "pass"),
    (0.0, "NTD", "fail"),
    (-20.0, "NTD", "fail"),
    (350.5, "NTD", "fail"),        # 新臺幣單據金額是整數
    (12.34, "USD", "pass"),
    (12.345, "USD", "fail"),
    (100_000_000.0, "NTD", "fail"),  # 家用文件不合理的金額,轉人工
    (None, "NTD", "skip"),
])
def test_amount_plausible(amount, currency, status):
    entry = checks.check_amount_plausible(amount, currency)
    assert entry["status"] == status
    assert entry["fields"] == ["amount"]


def test_amount_sum_is_interface_only_this_wave():
    entry = checks.check_amount_sum(350.0, None)
    assert entry["status"] == "skip"
    assert "唯一證據" in entry["detail"]
    # 介面已備妥:有明細時比對加總,但仍只是弱證據
    assert checks.check_amount_sum(350.0, [180, 170])["status"] == "pass"
    assert checks.check_amount_sum(351.0, [180, 170])["status"] == "fail"
