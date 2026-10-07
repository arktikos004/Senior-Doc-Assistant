"""日期解析與期限計算測試:民國年寫法、相對期限、合理性檢查(全部由程式算,不靠模型)。"""
from datetime import date

import pytest

from src.dates import is_plausible_deadline, parse_date, resolve_deadline

BASE = date(2026, 8, 3)  # 民國 115 年 8 月 3 日


@pytest.mark.parametrize(
    "text",
    [
        "115年8月3日",
        "115/08/03",
        "中華民國115年8月3日",
        "115.8.3",
        "115-8-3",
        "民國 115 年 08 月 03 日",
        "１１５年８月３日",                 # 全形數字
        "中華民國一百一十五年八月三日",     # 國字數字
        "中華民國一一五年八月三日",
        "發文日期：中華民國115年8月3日",   # 前後有其他文字
        "2026-08-03",
        "2026/8/3",
        "2026年8月3日",
    ],
)
def test_parse_date_roc_and_western(text):
    assert parse_date(text) == date(2026, 8, 3)


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "115年07-08月",      # 發票期別,不是日期
        "115年13月1日",      # 月份不存在
        "115年2月30日",      # 日期不存在
        "1850-01-01",        # 西元年太早(民國前)
        "看不清楚",
        "0912-345-678",      # 電話號碼不是日期
    ],
)
def test_parse_date_rejects_invalid(text):
    assert parse_date(text) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        # 始日不算入(行政程序法第 48 條、民法第 120 條):收到當天不算,第 15 天為末日
        ("收到本函後15日內", date(2026, 8, 18)),
        ("自送達之次日起30日內", date(2026, 9, 2)),
        ("於115年10月31日前", date(2026, 10, 31)),
        ("請於中華民國115年10月31日以前辦理", date(2026, 10, 31)),
        ("文到十日內", date(2026, 8, 13)),
        ("收到本函後十五日內", date(2026, 8, 18)),
        ("收到後二週內", date(2026, 8, 17)),
        ("本函送達後1個月內", date(2026, 9, 3)),
        ("三個月內", date(2026, 11, 3)),
        # 「即日起」「自某日起」含當天
        ("自即日起30日內", date(2026, 9, 1)),
        ("自115年8月10日起10日內", date(2026, 8, 19)),
        ("收到本函後15天內", date(2026, 8, 18)),
    ],
)
def test_resolve_deadline(text, expected):
    assert resolve_deadline(text, BASE) == expected


def test_resolve_deadline_accepts_iso_string_base():
    assert resolve_deadline("收到本函後15日內", "2026-08-03") == date(2026, 8, 18)


def test_resolve_deadline_month_end_clamps():
    # 以月定期間,最後之月無相當日者,以該月末日為期間末日(民法第 121 條)
    assert resolve_deadline("收到本函後1個月內", date(2026, 1, 31)) == date(2026, 2, 28)


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "儘速辦理",                     # 沒有可計算的期限
        "於114年1月1日前",              # 早於基準日
        "於118年12月31日前",            # 晚於基準日 2 年
        "收到本函後1000日內",           # 晚於基準日 2 年
        "115年07-08月",                 # 期別不是期限
    ],
)
def test_resolve_deadline_returns_none_when_unsure(text):
    assert resolve_deadline(text, BASE) is None


@pytest.mark.parametrize("base", [None, "", "不是日期"])
def test_resolve_deadline_requires_base(base):
    assert resolve_deadline("收到本函後15日內", base) is None


def test_plausibility_boundaries():
    assert is_plausible_deadline(BASE, BASE)                        # 當天可以
    assert is_plausible_deadline(date(2028, 8, 3), BASE)            # 剛好 2 年可以
    assert not is_plausible_deadline(date(2028, 8, 4), BASE)        # 超過 2 年
    assert not is_plausible_deadline(date(2026, 8, 2), BASE)        # 早於基準日
    # 2/29 的兩年後沒有 2/29,上限取 2/28
    assert is_plausible_deadline(date(2030, 2, 28), date(2028, 2, 29))
    assert not is_plausible_deadline(date(2030, 3, 1), date(2028, 2, 29))


def test_resolve_deadline_rejects_overlong_text():
    # 模型把整段公文抄進 deadline_text 時,寧可不算也不要從中亂抓一個數字
    assert resolve_deadline("說明：" + "依規定辦理。" * 50 + "收到本函後15日內", BASE) is None
