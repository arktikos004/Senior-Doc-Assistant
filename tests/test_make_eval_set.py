"""多版型合成評測集產生器(tools/make_eval_set.py)的測試。

影像只寫進 tmp_path;不呼叫模型、不連網。標準答案與影像上印的字都是產生器自己選的數值,
這裡檢查:契約格式、dev/test 切分、同 seed 重現、降質條件的性質,以及「印出來的字 = 標準答案」。
"""
import ast
import hashlib
import json
import math
import random
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import make_eval_set as ev  # noqa: E402
from src.dates import parse_date, resolve_deadline  # noqa: E402

# 與 EV-3WAY 共用的 labels.jsonl 契約:欄位名稱與順序一字不差(這份是測試自己的副本,產生器改名就會被抓到)
CONTRACT_KEYS = ["file", "split", "template", "variant", "condition", "doc_type", "vendor", "date", "amount",
                 "invoice_number", "fields", "qr", "barcode"]
CONDITIONS = ["blur", "tilt", "glare", "crop", "dark"]


def labels_of(**kwargs) -> list[dict]:
    return ev.labels_for(ev.plan(**kwargs))


def first_label(template: str, condition: str = "clean") -> dict:
    item = ev.plan(per_template=1, templates=[template])[0]
    return item.label(condition)


def digits(text: str) -> int:
    return int(re.sub(r"\D", "", text))


# --- 民國年寫法 ---

def test_roc_styles_print_the_expected_text_and_the_system_parser_reads_them_back():
    day = date(2026, 8, 5)
    expected = {"cn": "115年8月5日", "cn_pad": "115年08月05日", "slash": "115/08/05", "dot": "115.08.05",
                "full": "中華民國115年8月5日", "full_pad": "中華民國115年08月05日"}
    for style, text in expected.items():
        assert ev.roc(day, style) == text
        assert parse_date(text) == day
    assert set(ev.BILL_STYLES) <= set(expected)
    with pytest.raises(ValueError):
        ev.roc(day, "iso")


def test_money_has_thousands_separator():
    assert ev.money(1286) == "1,286" and ev.money(-100) == "-100" and ev.money(12345) == "12,345"


# --- 標準答案的格式 ---

def test_label_contract_keys_and_order():
    label = first_label("bill_power")
    assert list(label) == CONTRACT_KEYS
    assert list(label["qr"]) == ["present", "readable"]
    assert label["barcode"] == {"present": True}
    assert label["file"] == "dev/bill_power_01_clean.png"


def test_every_planned_label_passes_the_schema_check():
    labels = labels_of(per_template=6)
    assert labels
    for label in labels:
        assert ev.validate_label(label) == [], label["file"]


@pytest.mark.parametrize("mutate, expected", [
    (lambda lb: lb.pop("qr"), "缺少欄位 qr"),
    (lambda lb: lb.update(extra=1), "多出欄位 extra"),
    (lambda lb: lb.update(split="val"), "split"),
    (lambda lb: lb.update(file="test/bill_power_01_clean.png"), "file"),
    (lambda lb: lb.update(file="/dev/x.png"), "file"),
    (lambda lb: lb.update(file="dev\\x.png"), "file"),
    (lambda lb: lb.update(variant=0), "variant"),
    (lambda lb: lb.update(variant=True), "variant"),
    (lambda lb: lb.update(condition="sepia"), "condition"),
    (lambda lb: lb.update(doc_type="其他"), "doc_type"),
    (lambda lb: lb.update(vendor=""), "vendor"),
    (lambda lb: lb.update(date="2026/07/06"), "date"),
    (lambda lb: lb.update(date="2026-02-30"), "date"),
    (lambda lb: lb.update(amount=1286.0), "amount"),
    (lambda lb: lb.update(amount="1286"), "amount"),
    (lambda lb: lb.update(amount=0), "amount"),
    (lambda lb: lb.update(invoice_number="AB-12345678"), "invoice_number"),
    (lambda lb: lb["qr"].update(present=True), "qr"),
    (lambda lb: lb["qr"].update(readable="no"), "qr"),
    (lambda lb: lb.pop("barcode"), "缺少欄位 barcode"),
    (lambda lb: lb["barcode"].update(present="yes"), "barcode"),
    (lambda lb: lb["barcode"].update(readable=True), "barcode"),
    (lambda lb: lb.update(doc_type="收據", fields={}), "只有帳單的 barcode.present"),
    (lambda lb: lb["fields"].pop("due_date"), "fields"),
    (lambda lb: lb["fields"].update(bill_kind="水電"), "bill_kind"),
    (lambda lb: lb["fields"].update(due_date="115/10/20"), "due_date"),
])
def test_schema_check_catches_problems(mutate, expected):
    label = first_label("bill_power")
    mutate(label)
    problems = ev.validate_label(label)
    assert problems and any(expected in p for p in problems), problems


def test_schema_check_rejects_non_objects():
    assert ev.validate_label([]) and ev.validate_label(None) and ev.validate_label("{}")


# --- 切分、數量、日期、個資 ---

DEV_TEMPLATES = {"bill_power", "bill_water", "notice_receipt_days", "notice_fixed_date", "med_bag_a", "receipt_a",
                 "einvoice_a"}
TEST_TEMPLATES = {"bill_telecom", "bill_gas", "notice_service_next_day", "med_bag_b", "receipt_b", "einvoice_b"}
TODAY = date(2026, 10, 5)


def test_splits_are_by_template_and_do_not_overlap():
    dev = {n for n, t in ev.TEMPLATES.items() if t.split == "dev"}
    test = {n for n, t in ev.TEMPLATES.items() if t.split == "test"}
    assert dev == DEV_TEMPLATES and test == TEST_TEMPLATES and not dev & test
    labels = labels_of(per_template=2)
    assert {lb["template"] for lb in labels if lb["split"] == "dev"} == DEV_TEMPLATES
    assert {lb["template"] for lb in labels if lb["split"] == "test"} == TEST_TEMPLATES
    assert all(lb["file"].startswith(lb["split"] + "/") for lb in labels)
    for split in ("dev", "test"):                                                   # 每種 doc_type 兩個 split 都有
        assert {lb["doc_type"] for lb in labels if lb["split"] == split} == set(ev.LABEL_DOC_TYPES)


def test_default_set_has_six_variants_per_template_each_with_clean_plus_one_degraded():
    labels = labels_of()
    assert len(labels) == 162 and {lb["split"] for lb in labels} == {"dev", "test"}
    assert sum(lb["split"] == "dev" for lb in labels) == 84 and sum(lb["split"] == "test" for lb in labels) == 78
    for name in ev.TEMPLATES:
        mine = [lb for lb in labels if lb["template"] == name]
        assert {lb["variant"] for lb in mine} == set(range(1, 7))
        extra = 1 if name == "einvoice_b" else 0
        assert len(mine) == 6 * (2 + extra)
        for variant in range(1, 7):
            conds = [lb["condition"] for lb in mine if lb["variant"] == variant]
            assert conds[0] == "clean" and conds[1] == ev.condition_for(variant) and len(conds) == 2 + extra
    assert sorted(lb["condition"] for lb in labels if lb["condition"] not in ("clean", "blur", "qr_damaged")) \
        == sorted(["tilt", "glare", "crop", "dark"] * 13)
    assert sum(lb["condition"] == "qr_damaged" for lb in labels) == 6


def test_every_vendor_is_fictional():
    assert all(lb["vendor"].startswith("青禾") for lb in labels_of(per_template=6))


def test_all_dates_pass_the_systems_own_plausibility_checks():
    """文件日期落在 2026-07-01 至 09-30;繳費期限與公文期限不早於開立日、不超過 2 年:不會被合理性檢查判錯。"""
    from src.dates import is_plausible_deadline
    from src.verify.checks import check_date_plausible, check_due_date
    for lb in labels_of(per_template=6):
        day = date.fromisoformat(lb["date"])
        assert ev.DATE_FIRST <= day <= ev.DATE_LAST and check_date_plausible(lb["date"], TODAY)["status"] == "pass"
        if lb["doc_type"] == "帳單":
            due = lb["fields"]["due_date"]
            assert check_due_date(due, lb["date"])["status"] == "pass" and is_plausible_deadline(date.fromisoformat(due), day)
            assert date.fromisoformat(due) > day
        if lb["doc_type"] == "公文":
            assert is_plausible_deadline(date.fromisoformat(lb["fields"]["deadline"]), day)


PRIVATE_WORDS = ("電話", "專線", "分機", "地址", "門號", "電號", "水號", "表號", "姓名", "病歷號", "受文者", "正本", "經手人",
                 "調劑藥師", "戶名", "用戶名稱", "承辦人")


def test_personal_fields_are_masked_with_circles():
    """人名、地址、電話、各種號碼一律用「○」遮蔽:印著這些欄位名稱的那一行不會有任何數字。"""
    for name, template in ev.TEMPLATES.items():
        if template.has_qr:
            continue                                                                # 證明聯的版面由 make_sample 畫,沒有個資欄位
        for item in ev.plan(per_template=3, templates=[name]):
            texts = template.render(item.spec).texts
            assert any("○○○" in t for t in texts) or name.startswith("receipt_a"), name
            for text in texts:
                assert not (any(word in text for word in PRIVATE_WORDS) and re.search(r"\d", text)), (name, text)


# --- 帳單 ---

BILL_TEMPLATES ={"bill_power": ("dev", "電費", "青禾電力股份有限公司"), "bill_water": ("dev", "水費", "青禾自來水公司"),
                  "bill_telecom": ("test", "電信", "青禾電信"), "bill_gas": ("test", "瓦斯", "青禾瓦斯股份有限公司")}


@pytest.mark.parametrize("name", BILL_TEMPLATES)
def test_bill_values_are_distinct_valid_and_formatted(name):
    split, kind, vendor = BILL_TEMPLATES[name]
    template = ev.TEMPLATES[name]
    assert template.split == split and template.doc_type == "帳單"
    items = ev.plan(per_template=6, templates=[name])
    specs = [i.spec for i in items]
    assert [i.variant for i in items] == [1, 2, 3, 4, 5, 6]
    assert len({s.amount for s in specs}) == 6 and len({s.issued for s in specs}) == 6   # 6 組不同數值
    assert {s.style for s in specs} == set(ev.BILL_STYLES)                                # 民國年寫法有變化
    for s in specs:
        assert s.kind == kind and s.vendor == vendor
        assert ev.DATE_FIRST <= s.issued <= ev.DATE_LAST
        assert 14 <= (s.due - s.issued).days <= 25                  # 期限在出帳後,不早於出帳日
        assert s.amount >= 1000                                       # 金額有千分位
        assert s.items == () or sum(v for _, v in s.items) == s.amount
        assert s.prev != s.amount and s.meter < s.issued              # 干擾項不等於答案


@pytest.mark.parametrize("name", BILL_TEMPLATES)
def test_bill_printed_text_matches_the_label(name):
    """影像上印的出帳日、繳費期限、金額,和標準答案是同一組數值(金額印千分位)。"""
    for item in ev.plan(per_template=6, templates=[name]):
        label, rendered = item.label("clean"), item.template.render(item.spec)
        printed_date, printed_due = rendered.printed("date"), rendered.printed("due_date")
        printed_amount, printed_vendor = rendered.printed("amount"), rendered.printed("vendor")
        assert len(printed_date) == len(printed_due) == len(printed_amount) == len(printed_vendor) == 1
        assert parse_date(printed_date[0]).isoformat() == label["date"]
        assert parse_date(printed_due[0]).isoformat() == label["fields"]["due_date"]
        assert digits(printed_amount[0]) == label["amount"] and "," in printed_amount[0]
        assert printed_vendor[0] == label["vendor"]


BARCODE_COLLECT_AFTER = {"bill_power": 0, "bill_water": 30, "bill_gas": 3}     # 代收期限晚於繳費期限幾天


@pytest.mark.parametrize("name", BILL_TEMPLATES)
def test_store_barcodes_carry_the_label_amount_and_parse_with_the_systems_rules(name):
    """電費、水費、瓦斯費印三段條碼(電信費沒有):校對碼合法、金額 = 標準答案、應繳日期 = 繳費期限。"""
    from src.verify import billcode
    for item in ev.plan(per_template=6, templates=[name]):
        label, spec = item.label("clean"), item.spec
        if name not in BARCODE_COLLECT_AFTER:
            assert spec.barcode is None and label["barcode"] == {"present": False}
            continue
        assert label["barcode"] == {"present": True}
        parsed = billcode.parse_store_barcodes(list(spec.barcode))
        assert parsed is not None and parsed.raw == spec.barcode
        assert parsed.amount == label["amount"] and parsed.pay_date == label["fields"]["due_date"]
        assert parsed.due_date == (spec.due + timedelta(days=BARCODE_COLLECT_AFTER[name])).isoformat()


def test_render_item_refuses_to_write_a_bill_whose_barcode_does_not_decode(monkeypatch):
    item = ev.plan(per_template=1, templates=["bill_gas"])[0]
    monkeypatch.setattr(ev, "read_store_barcode", lambda image: None)                   # 解碼器什麼都讀不到
    with pytest.raises(RuntimeError, match="超商代收條碼"):
        ev.render_item(item)


def test_per_template_can_grow_without_changing_the_earlier_sets():
    """--per-template 加大只會多出新的組(擴充實例用),前面的組數值不變。"""
    few, many = labels_of(per_template=3), labels_of(per_template=8)
    assert few == [lb for lb in many if lb["variant"] <= 3]


def test_different_seeds_draw_different_values():
    assert labels_of(seed=1, per_template=3) != labels_of(seed=2, per_template=3)


def test_condition_cycle_is_blur_tilt_glare_crop_dark():
    assert [ev.condition_for(v) for v in range(1, 8)] == CONDITIONS + ["blur", "tilt"]
    for item in ev.plan(per_template=6, templates=["bill_gas"]):
        assert item.conditions == ["clean", ev.condition_for(item.variant)]


def test_unknown_template_and_bad_count_are_rejected():
    with pytest.raises(ValueError):
        ev.plan(templates=["bill_steam"])
    with pytest.raises(ValueError):
        ev.plan(per_template=0)


# --- 公文 ---

LETTER_TEMPLATES = {"notice_receipt_days": ("dev", "青禾市政府稅務局", r"收到本函後(\d+)日內"),
                    "notice_fixed_date": ("dev", "青禾市政府社會局", r"請於(\d+)年(\d+)月(\d+)日前"),
                    "notice_service_next_day": ("test", "青禾市政府民政局", r"自送達之次日起(\d+)日內")}


@pytest.mark.parametrize("name", LETTER_TEMPLATES)
@pytest.mark.parametrize("seed", [1, 42, 2026])
def test_letter_deadlines_agree_with_the_rule_in_dates_py(name, seed):
    """標準答案的期限是產生器用選定的數值自己指定的;src.dates.resolve_deadline 算出來要一樣。

    不一樣就代表我們對期限規則的理解和 dates.py 有落差:不要改這裡湊答案,回報給整合者。
    """
    items = ev.plan(seed=seed, per_template=15, templates=[name])
    assert len(items) == 15
    for item in items:
        fields = item.spec.truth()["fields"]
        got = resolve_deadline(fields["deadline_text"], item.spec.issued)
        assert got == date.fromisoformat(fields["deadline"]), (item.file("clean"), fields["deadline_text"])


def test_the_generator_never_calls_resolve_deadline_to_make_answers():
    """標準答案不得由 resolve_deadline 產生:產生器的程式碼裡沒有 import src.dates、也沒有用到這個函式。"""
    tree = ast.parse(Path(ev.__file__).read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree)
                                                                      if isinstance(n, ast.Attribute)}
    assert not any("dates" in name.split(".") for name in imported), imported
    assert "resolve_deadline" not in imported | used


@pytest.mark.parametrize("name", LETTER_TEMPLATES)
def test_letter_values_follow_the_stated_rule(name):
    split, agency, pattern = LETTER_TEMPLATES[name]
    template = ev.TEMPLATES[name]
    assert template.split == split and template.doc_type == "公文"
    specs = [i.spec for i in ev.plan(per_template=6, templates=[name])]
    assert len({s.issued for s in specs}) == 6 and len({s.doc_number for s in specs}) == 6
    assert len({s.subject for s in specs}) == 6                                  # 6 組不同數值
    assert {s.style for s in specs} == set(ev.LETTER_STYLES)                    # 發文日期的寫法有變化
    days = []
    for s in specs:
        truth = s.truth()
        assert truth["vendor"] == agency and truth["amount"] is None and truth["invoice_number"] is None
        assert ev.DATE_FIRST <= s.issued <= ev.DATE_LAST
        assert ev.DATE_FIRST <= s.ref_date < s.issued                            # 說明裡的干擾日期也不早於 7/1
        found = re.fullmatch(pattern, s.deadline_text)
        assert found, s.deadline_text
        if name == "notice_fixed_date":
            written = date(int(found[1]) + 1911, int(found[2]), int(found[3]))   # 期限就是文件上寫明的那一天
            assert s.deadline == written and 21 <= (s.deadline - s.issued).days <= 45
        else:
            days.append(int(found[1]))
            assert (s.deadline - s.issued).days == int(found[1])                  # 發文日 + N 日
        assert s.doc_number.startswith("青") and s.doc_number.endswith("號") and "○" not in s.doc_number
        assert len(s.actions) == 2 and len(set(s.actions)) == 2
    assert len(set(days)) == len(days)


@pytest.mark.parametrize("name", LETTER_TEMPLATES)
@pytest.mark.parametrize("seed", [42, 7])
def test_letter_printed_text_matches_the_label(name, seed):
    """影像上印的發文日期、字號、主旨、期限原文、應辦事項,都和標準答案一致;期限原文連續印在同一行。"""
    template = ev.TEMPLATES[name]
    for item in ev.plan(seed=seed, per_template=8, templates=[name]):
        label, rendered = item.label("clean"), template.render(item.spec)
        fields = label["fields"]
        assert parse_date(rendered.printed("date")[0]).isoformat() == label["date"]
        assert rendered.printed("doc_number") == [fields["doc_number"]]
        assert "".join(rendered.printed("subject")) == fields["subject"]
        lines = rendered.printed("subject") + rendered.printed("body")
        assert sum(fields["deadline_text"] in line for line in lines) == 1, lines
        for action in fields["required_actions"]:
            assert sum(action in line for line in lines) == 1, action


def test_letter_deadline_sits_in_the_subject_for_two_templates_and_in_the_body_for_one():
    """期限原文的位置不固定:避免評測變成「只認主旨」。"""
    where = {}
    for name in LETTER_TEMPLATES:
        item = ev.plan(per_template=1, templates=[name])[0]
        rendered = item.template.render(item.spec)
        where[name] = ("subject" if item.spec.deadline_text in "".join(rendered.printed("subject")) else "body")
    assert where == {"notice_receipt_days": "subject", "notice_fixed_date": "body",
                     "notice_service_next_day": "body"}


# --- 藥袋 ---

BAG_TEMPLATES = {"med_bag_a": ("dev", "青禾家庭醫學科診所"), "med_bag_b": ("test", "青禾社區藥局")}
MEDICAL_WORDS = ("用途", "注意", "副作用", "禁忌", "嗜睡", "功效", "適應", "療效", "症狀", "劑量")   # 紙上不印、標準答案不收


def slots_printed(text: str) -> list[str]:
    """從藥袋上印的用法原文讀出明印的時段(「午」算「中」);只印次數、「需要時」的讀出來是空的。"""
    found = [slot for slot, chars in (("早", "早"), ("中", "午中"), ("晚", "晚"), ("睡前", "睡前"))
             if any(c in text for c in chars)]
    return found


@pytest.mark.parametrize("name", BAG_TEMPLATES)
def test_bag_labels_cover_multi_slot_prn_and_count_only(name):
    split, clinic = BAG_TEMPLATES[name]
    assert ev.TEMPLATES[name].split == split and ev.TEMPLATES[name].doc_type == "藥袋"
    items = ev.plan(per_template=6, templates=[name])
    bags = [i.label("clean") for i in items]
    for label in bags:
        entries = label["fields"]["items"]
        assert 2 <= len(entries) <= 4                                                   # 每袋 2–4 種藥
        assert len({e["name"] for e in entries}) == len(entries)
        assert label["vendor"] == clinic and label["amount"] is None
        for e in entries:
            assert list(e) == ["name", "timing", "prn"]
            assert not (e["prn"] and e["timing"])                                       # 需要時的藥沒印時段
    entries = [e for label in bags for e in label["fields"]["items"]]
    assert any(len(e["timing"]) >= 2 for e in entries), "要有多個時段的藥"
    assert any(e["prn"] and e["timing"] == [] for e in entries), "要有只印「需要時」的藥"
    assert any(not e["prn"] and e["timing"] == [] for e in entries), "要有只印次數、沒印時段的藥"
    assert any(len(e["timing"]) == 1 for e in entries)
    assert any(d.freq_text == "一天三次" and d.timing == () and not d.prn
               for item in items for d in item.spec.drugs), "要有只印「一天三次」的藥"
    assert {slot for e in entries for slot in e["timing"]} == {"早", "中", "晚", "睡前"}


def test_bag_coverage_shows_up_within_the_first_three_bags():
    """前三袋就涵蓋多時段、需要時、只印次數三種(--per-template 調小時也不缺)。"""
    for name in BAG_TEMPLATES:
        kinds = {d.kind for item in ev.plan(per_template=3, templates=[name]) for d in item.spec.drugs}
        assert {"multi", "prn", "count"} <= kinds


@pytest.mark.parametrize("name", BAG_TEMPLATES)
@pytest.mark.parametrize("seed", [42, 5])
def test_bag_printed_text_matches_the_label(name, seed):
    template = ev.TEMPLATES[name]
    for item in ev.plan(seed=seed, per_template=8, templates=[name]):
        label, rendered = item.label("clean"), template.render(item.spec)
        entries = label["fields"]["items"]
        assert parse_date(rendered.printed("date")[0]).isoformat() == label["date"]
        assert rendered.printed("vendor") == [label["vendor"]]
        assert rendered.printed("name") == [e["name"] for e in entries]                # 藥名一行、原樣照抄
        if name == "med_bag_a":
            for d, line in zip(item.spec.drugs, rendered.printed("usage")):
                assert slots_printed(line) == list(d.timing), line                      # 明印的時段 = timing
                assert f"每次 {d.dose} 錠" in line
                if d.prn:
                    assert "需要時" in line or "必要時" in line
        else:
            ways = iter(rendered.printed("way"))
            for row, d in enumerate(item.spec.drugs):
                marked = [m.text for m in rendered.marks if m.key == f"slot:{row}"]
                assert marked == list(d.timing)                                         # 格子裡畫圓的欄 = timing
                if d.freq_text:
                    assert d.freq_text in next(ways)
                elif any(s != "睡前" for s in d.timing):
                    assert next(ways) == "飯後"
        for text in rendered.texts:
            assert not any(word in text for word in MEDICAL_WORDS), text               # 不加任何醫療資訊
        assert any("病歷號:○" in t for t in rendered.texts) and any("○○○" in t for t in rendered.texts)


def test_count_only_drugs_are_never_converted_into_slots():
    """「一天三次」只印次數:標準答案的 timing 是空的(系統不得換算成早中晚)。"""
    for name in BAG_TEMPLATES:
        for item in ev.plan(per_template=12, templates=[name]):
            for d, e in zip(item.spec.drugs, item.label("clean")["fields"]["items"]):
                if d.kind == "count":
                    assert e["timing"] == [] and not e["prn"] and d.times in (2, 3, 4)


def test_bag_b_looks_different_from_bag_a():
    """b 是橫式勾選表格、a 是直式卡片:尺寸與印出的字都不同。"""
    a, b = (ev.TEMPLATES[n].render(ev.plan(per_template=1, templates=[n])[0].spec) for n in BAG_TEMPLATES)
    assert a.image.height > a.image.width and b.image.width > b.image.height
    assert any("早" == t for t in b.texts) and not any("早" == t for t in a.texts)
    assert not a.printed("way") and b.printed("way")


def test_wrap_prefers_clause_boundaries_and_falls_back_to_characters():
    page = ev.Page(1240, 400)
    short = "請於收到本函後15日內補送文件,逾期不受理。"
    assert "".join(page.wrap(short, 36, "sans", 1000)) == short
    lines = page.wrap("第一個子句比較長一點,第二個子句也很長,第三個子句最後。", 36, "sans", 700)
    assert all(line.endswith((",", "。")) for line in lines), lines            # 每行都收在逗號或句號
    long_clause = "一二三四五六七八九十" * 6
    parts = page.wrap(long_clause, 36, "sans", 500)
    assert len(parts) > 1 and "".join(parts) == long_clause                    # 單一子句超過一行才逐字折
    assert page.wrap("", 36, "sans", 500) == [""]


# --- 收據 ---

def test_chinese_amount_writes_zeros_the_way_receipts_do():
    expected = {1: "壹", 10: "壹拾", 105: "壹佰零伍", 1000: "壹仟", 1100: "壹仟壹佰", 1250: "壹仟貳佰伍拾", 2005: "貳仟零伍",
                3050: "參仟零伍拾", 9950: "玖仟玖佰伍拾", 10000: "壹萬", 10050: "壹萬零伍拾", 10500: "壹萬零伍佰",
                12000: "壹萬貳仟", 99999: "玖萬玖仟玖佰玖拾玖"}
    for number, words in expected.items():
        assert ev.chinese_amount(number) == words, number
    for bad in (0, -5, 100_000):
        with pytest.raises(ValueError):
            ev.chinese_amount(bad)


def parse_chinese_amount(words: str) -> int:
    """測試用的反向解析:國字大寫 → 數字(獨立於產生器的寫法,抓得到位數或零寫錯)。"""
    digit = {c: i for i, c in enumerate(ev.CN_DIGITS)}
    unit = {"拾": 10, "佰": 100, "仟": 1000}
    total, section, current = 0, 0, 0
    for ch in words:
        if ch in digit:
            current = digit[ch]
        elif ch in unit:
            section += current * unit[ch]
            current = 0
        elif ch == "萬":
            total += (section + current) * 10_000
            section = current = 0
    return total + section + current


def test_chinese_amount_round_trips_for_every_amount_the_generator_can_draw():
    assert all(parse_chinese_amount(ev.chinese_amount(n)) == n for n in range(1, 10_000, 1))
    assert all(parse_chinese_amount(ev.chinese_amount(n)) == n for n in range(10_000, 100_000, 37))


def test_date_text_styles():
    day = date(2026, 8, 5)
    assert [ev.date_text(day, s) for s in ev.RECEIPT_STYLES] == ["2026-08-05", "115/08/05", "115年8月5日"]
    assert all(parse_date(ev.date_text(day, s)) == day for s in ev.RECEIPT_STYLES)


@pytest.mark.parametrize("name", ["receipt_a", "receipt_b"])
def test_receipts_have_no_qr_no_invoice_number_and_valid_values(name):
    template = ev.TEMPLATES[name]
    assert template.doc_type == "收據" and template.split == ("dev" if name == "receipt_a" else "test")
    assert not template.has_qr and template.extra_conditions == ()
    specs = [i.spec for i in ev.plan(per_template=6, templates=[name])]
    assert len({s.amount for s in specs}) == 6 and len({s.issued for s in specs}) == 6
    for item in ev.plan(per_template=6, templates=[name]):
        label = item.label("clean")
        assert label["invoice_number"] is None and label["fields"] == {} and label["qr"] == {"present": False,
                                                                                            "readable": False}
        assert ev.DATE_FIRST <= item.spec.issued <= ev.DATE_LAST and label["amount"] > 0
        assert label["vendor"].startswith("青禾")


def test_receipt_a_prints_the_items_the_total_and_the_date_in_varying_styles():
    items = ev.plan(per_template=6, templates=["receipt_a"])
    assert {i.spec.style for i in items} == set(ev.RECEIPT_STYLES)
    for item in items:
        label, rendered = item.label("clean"), ev.TEMPLATES["receipt_a"].render(item.spec)
        assert sum(sub for _, _, sub in item.spec.items) == label["amount"] and 2 <= len(item.spec.items) <= 5
        assert parse_date(rendered.printed("date")[0]).isoformat() == label["date"]
        assert rendered.printed("vendor") == [label["vendor"]]
        printed_total = rendered.printed("amount")
        assert len(printed_total) == 1 and digits(printed_total[0]) == label["amount"] and printed_total[0].endswith("元")
        assert ("," in printed_total[0]) == (label["amount"] >= 1000)


def test_receipt_b_prints_the_amount_in_words_and_in_digits():
    items = ev.plan(per_template=6, templates=["receipt_b"])
    assert {i.spec.style for i in items} == {"full", "full_pad"}
    for item in items:
        label, rendered = item.label("clean"), ev.TEMPLATES["receipt_b"].render(item.spec)
        words = re.fullmatch(r"新臺幣  (\S+)  元整", rendered.printed("amount_cn")[0])[1]
        assert parse_chinese_amount(words) == label["amount"]                          # 國字大寫
        assert digits(rendered.printed("amount")[0]) == label["amount"]                # 阿拉伯數字
        assert "," in rendered.printed("amount")[0] or label["amount"] < 1000
        assert parse_date(rendered.printed("date")[0]).isoformat() == label["date"]
        assert rendered.printed("vendor") == [f"收款單位:{label['vendor']}"]
        assert str(item.spec.serial).zfill(7) in " ".join(rendered.texts)                # 收據編號是干擾項,不是發票號碼


# --- 電子發票 ---

@pytest.mark.parametrize("name, split, conditions", [("einvoice_a", "dev", 2), ("einvoice_b", "test", 3)])
def test_einvoice_templates_and_conditions(name, split, conditions):
    template = ev.TEMPLATES[name]
    assert template.doc_type == "發票" and template.split == split and template.has_qr
    for item in ev.plan(per_template=6, templates=[name]):
        assert len(item.conditions) == conditions
        assert ("qr_damaged" in item.conditions) == (name == "einvoice_b")
        label = item.label("clean")
        assert label["qr"] == {"present": True, "readable": True} and label["invoice_number"] and label["amount"] > 0
        assert ev.DATE_FIRST <= date.fromisoformat(label["date"]) <= ev.DATE_LAST
        if "qr_damaged" in item.conditions:
            assert item.label("qr_damaged")["qr"] == {"present": True, "readable": False}


def test_einvoice_values_are_synthetic_and_internally_consistent():
    from src.verify.checks import check_date_in_period, parse_period, tax_id_checksum_ok
    stores = {"einvoice_a": ev.EINVOICE_STORES_A, "einvoice_b": ev.EINVOICE_STORES_B}
    for name, pool in stores.items():
        specs = [i.spec for i in ev.plan(per_template=6, templates=[name])]
        assert len({s.spec.invoice_number for s in specs}) == 6 and len({s.spec.issued.date() for s in specs}) == 6
        for s in specs:
            truth, spec = s.truth(), s.spec
            assert (truth["vendor"], spec.branch) in pool and "示範" in truth["vendor"] and "青禾" in truth["vendor"]
            assert re.fullmatch(r"[A-Z]{2}\d{8}", truth["invoice_number"]) and truth["amount"] == spec.total
            assert tax_id_checksum_ok(truth["fields"]["seller_tax_id"])
            assert parse_period(truth["fields"]["period"]) is not None
            assert check_date_in_period(truth["date"], truth["fields"]["period"])["status"] == "pass"
            assert re.fullmatch(r"\d{4}", truth["fields"]["random_code"])
    assert not set(ev.EINVOICE_STORES_A) & set(ev.EINVOICE_STORES_B)                  # 店名池 dev/test 不重疊


def test_einvoice_qr_boxes_really_sit_on_the_qr_codes():
    """QR 的位置是照 make_sample 貼圖的座標算的:版面若被改動,這裡會抓到(亮斑與汙損都靠這個位置)。"""
    item = ev.plan(per_template=1, templates=["einvoice_a"])[0]
    rendered = ev.TEMPLATES["einvoice_a"].render(item.spec)
    gray = np.asarray(rendered.image.convert("L"))
    for x0, y0, x1, y1 in rendered.boxes("qr"):
        dark = (gray[y0:y1, x0:x1] < 128).mean()
        assert 0.2 < dark < 0.6, dark
        assert (gray[y0 - 12:y0 - 2, x0:x1] > 200).all(), "QR 上方的空白被占用,位置算錯了"
    left, right = rendered.boxes("qr")
    assert left[2] < right[0] and left[1] == right[1]


def test_read_left_qr_and_qr_matches_are_not_vacuous():
    item = ev.plan(per_template=1, templates=["einvoice_a"])[0]
    image = ev.TEMPLATES["einvoice_a"].render(item.spec).image
    qr = ev.read_left_qr(image)
    assert ev.qr_matches(qr, item.spec.spec)
    assert not ev.qr_matches(None, item.spec.spec)
    other = ev.plan(per_template=2, templates=["einvoice_a"])[1].spec.spec
    assert not ev.qr_matches(qr, other)                                                 # 換一張的規格就不相符
    assert ev.read_left_qr(Image.new("RGB", (400, 400), "white")) is None


def test_render_item_refuses_to_write_labels_that_lie_about_the_qr(monkeypatch):
    """標了 readable 的影像解不開(或解出來與標準答案不同),產生器不寫出去。"""
    item = ev.plan(per_template=1, templates=["einvoice_b"])[0]
    monkeypatch.setattr(ev, "read_left_qr", lambda image: None)                         # 解碼器什麼都讀不到
    with pytest.raises(RuntimeError, match="解不開"):
        ev.render_item(item)


def test_qr_damage_is_made_heavier_until_the_qr_cannot_be_read(monkeypatch):
    item = ev.plan(per_template=1, templates=["einvoice_b"])[0]
    rendered = ev.TEMPLATES["einvoice_b"].render(item.spec)
    calls = []

    def read_on_third_try(image):
        calls.append(image)
        return None if len(calls) >= 3 else object()                                    # 前兩次汙損還解得開

    monkeypatch.setattr(ev, "read_left_qr", read_on_third_try)
    out = ev._render_condition(item, rendered, rendered.image, "qr_damaged", random.Random(1))
    assert len(calls) == 3 and out.size == rendered.image.size
    monkeypatch.setattr(ev, "read_left_qr", lambda image: object())                    # 怎麼汙損都解得開
    with pytest.raises(RuntimeError, match="汙損到最重"):
        ev._render_condition(item, rendered, rendered.image, "qr_damaged", random.Random(1))


# --- 降質條件 ---

def sample_page(qr: bool = False) -> "ev.Rendered":
    """一張小的合成版面(600×800,四邊留白 ≥ 9%),拿來測降質函式。"""
    page = ev.Page(600, 800)
    for i in range(8):
        page.text(80, 120 + i * 80, f"第 {i} 行測試文字 {i * 111}", 36, key="line")
    if qr:
        page.d.rectangle((400, 600, 520, 720), fill=(0, 0, 0))
        page.marks.append(ev.Mark("qr", "", (400, 600, 520, 720)))
    return page.done()


def ink(img: Image.Image) -> int:
    return int((np.asarray(img.convert("L")) < 128).sum())


def test_crop_removes_only_blank_margin_on_one_side():
    rendered = sample_page()
    for seed in range(20):
        out = ev.degrade_crop(rendered.image, random.Random(seed), rendered)
        dw, dh = rendered.image.width - out.width, rendered.image.height - out.height
        assert (dw == 0) != (dh == 0), "一次只裁一側"
        ratio = (dw / rendered.image.width) if dw else (dh / rendered.image.height)
        assert ev.CROP_MIN <= ratio <= ev.CROP_MAX
        assert ink(out) == ink(rendered.image), "裁掉的只能是留白,字一個都不能少"


def test_crop_keeps_every_templates_content_whole():
    """每個版型、每一組:crop 之後內容外框的寬高不變(只裁掉留白,關鍵欄位完整),且一定裁得動。"""
    for name, template in ev.TEMPLATES.items():
        for item in ev.plan(seed=5, per_template=1, templates=[name]):
            rendered = template.render(item.spec)
            before = ev.content_box(rendered.image, rendered.bg)
            for seed in range(3):
                cropped = ev.degrade_crop(rendered.image, random.Random(seed), rendered)
                x0, y0, x1, y1 = ev.content_box(cropped, rendered.bg)
                assert (x1 - x0, y1 - y0) == (before[2] - before[0], before[3] - before[1]), (name, seed)
                assert cropped.size != rendered.image.size


def test_crop_refuses_when_there_is_not_enough_margin():
    page = ev.Page(300, 300)
    page.d.rectangle((5, 5, 295, 295), fill=(0, 0, 0))
    rendered = page.done()
    with pytest.raises(ValueError):
        ev.degrade_crop(rendered.image, random.Random(1), rendered)


def recover_tilt(w: int, h: int, new_w: int, new_h: int) -> float:
    """從 rotate(expand=True) 後的尺寸反推傾斜角度(度)。"""
    angles = [math.radians(a / 100) for a in range(0, 1001)]
    return math.degrees(min(angles, key=lambda t: abs(w * math.cos(t) + h * math.sin(t) - new_w)
                            + abs(w * math.sin(t) + h * math.cos(t) - new_h)))


def test_tilt_is_between_5_and_8_degrees_in_both_directions():
    rendered = sample_page()
    w, h = rendered.image.size
    directions = set()
    for seed in range(30):
        out = ev.degrade_tilt(rendered.image, random.Random(seed), rendered)
        assert out.width > w and out.height > h
        assert 4.8 <= recover_tilt(w, h, out.width, out.height) <= 8.2
        ys, xs = np.nonzero(np.asarray(out.convert("L")) < 128)
        directions.add(bool(np.corrcoef(xs, ys)[0, 1] > 0))     # 字的整體往右下傾或往左下傾
    assert directions == {True, False}


def test_dark_is_between_38_and_50_percent_of_the_original_brightness():
    rendered = sample_page()
    base = np.asarray(rendered.image).mean()
    for seed in range(10):
        out = ev.degrade_dark(rendered.image, random.Random(seed), rendered)
        assert 0.36 <= np.asarray(out).mean() / base <= 0.52


def test_blur_softens_edges_and_keeps_the_size():
    rendered = sample_page()
    out = ev.degrade_blur(rendered.image, random.Random(1), rendered)
    assert out.size == rendered.image.size
    sharp = np.abs(np.diff(np.asarray(rendered.image.convert("L")).astype(int), axis=1)).sum()
    soft = np.abs(np.diff(np.asarray(out.convert("L")).astype(int), axis=1)).sum()
    assert soft < sharp * 0.85


def test_glare_lights_up_text_without_erasing_it_and_keeps_away_from_qr():
    rendered = sample_page(qr=True)
    base = np.asarray(rendered.image).astype(int)
    for seed in range(20):
        out = np.asarray(ev.degrade_glare(rendered.image, random.Random(seed), rendered)).astype(int)
        change = out - base
        assert change.max() >= 100, "亮斑要真的洗淡字"
        text_pixels = base.max(axis=2) < 128
        assert out.max(axis=2)[text_pixels].min() < 200, "最黑的字也不能被整個抹成白色"
        x0, y0, x1, y1 = rendered.boxes("qr")[0]
        assert change[y0:y1, x0:x1].max() <= 0.4 * 255, "QR 不能被亮斑洗掉(電子發票的 QR 標準答案是解得開)"


def test_degrades_are_deterministic_per_rng():
    rendered = sample_page(qr=True)
    for name, degrade in ev.DEGRADES.items():
        a = degrade(rendered.image, random.Random("x"), rendered)
        b = degrade(rendered.image, random.Random("x"), rendered)
        assert a.tobytes() == b.tobytes() and a.size == b.size, name


# --- 產生:檔案、labels.jsonl、重現 ---

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    """全部版型、每版型 1 組(seed 7)。"""
    out = tmp_path_factory.mktemp("eval_set")
    return out, ev.generate(out, seed=7, per_template=1)


CONDITION_TEMPLATES = ["bill_gas", "receipt_a", "einvoice_a", "einvoice_b"]


@pytest.fixture(scope="module")
def conditions(tmp_path_factory):
    """每個版型 5 組:blur、tilt、glare、crop、dark 都出現一次(einvoice_b 另有 qr_damaged)。"""
    out = tmp_path_factory.mktemp("eval_conditions")
    return out, ev.generate(out, seed=11, per_template=5, templates=CONDITION_TEMPLATES)


def test_generate_writes_images_and_a_jsonl_that_matches_the_return_value(generated):
    out, labels = generated
    text = (out / "labels.jsonl").read_text(encoding="utf-8")
    assert not text.startswith(chr(0xFEFF)) and "\r" not in text and text.endswith("\n")
    on_disk = [json.loads(line) for line in text.splitlines()]
    assert on_disk == labels and len(labels) == len({lb["file"] for lb in labels})
    assert {p.relative_to(out).as_posix() for p in out.rglob("*.png")} == {lb["file"] for lb in labels}
    for label in labels:
        assert ev.validate_label(label) == []
        with Image.open(out / label["file"]) as img:
            assert img.mode == "RGB" and min(img.size) >= 600, label["file"]


def test_clean_bill_barcodes_decode_from_the_written_images(generated):
    """寫出去的 clean 帳單影像,條碼用 src.verify.billcode 解得開,金額與標準答案相同;沒印條碼的解不出東西。"""
    from src.verify import billcode
    from src.verify.einvoice import load_image
    out, labels = generated
    bills = [lb for lb in labels if lb["doc_type"] == "帳單" and lb["condition"] == "clean"]
    assert {lb["barcode"]["present"] for lb in bills} == {True, False}
    for label in bills:
        found = billcode.parse_store_barcodes(billcode.decode_bill_barcodes(load_image(out / label["file"])))
        if label["barcode"]["present"]:
            assert found is not None and found.amount == label["amount"], label["file"]
        else:
            assert found is None, label["file"]


def test_every_clean_image_carries_the_sample_badge(generated):
    """每張影像都要有「範例」標示:clean 的左上角有 make_demo_docs._watermark 的紅色標籤。"""
    out, labels = generated
    for label in labels:
        if label["condition"] != "clean":
            continue
        arr = np.asarray(Image.open(out / label["file"]).convert("RGB")).astype(int)[16:70, 16:196]
        red = (arr[..., 0] > 150) & (arr[..., 1] < 60) & (arr[..., 2] < 60)
        assert red.sum() > 4000, label["file"]


def test_degraded_images_differ_from_clean_in_the_expected_way(conditions):
    out, labels = conditions
    files = {(lb["template"], lb["variant"], lb["condition"]): lb["file"] for lb in labels}
    degraded = [(key, file) for key, file in files.items() if key[2] != "clean"]
    assert {key[2] for key, _ in degraded} == {"blur", "tilt", "glare", "crop", "dark", "qr_damaged"}
    for (template, variant, condition), file in degraded:
        with Image.open(out / files[(template, variant, "clean")]) as clean, Image.open(out / file) as img:
            if condition == "tilt":
                assert img.width > clean.width and img.height > clean.height
            elif condition == "crop":
                assert (img.width < clean.width) != (img.height < clean.height)
            else:
                assert img.size == clean.size
                assert np.asarray(img).tobytes() != np.asarray(clean).tobytes()


def test_einvoice_qr_decodes_with_the_systems_own_decoder_and_matches_the_labels(conditions):
    """clean 與降質(blur/tilt/glare/crop/dark)的電子發票:左側 QR 解得開,字軌、日期、金額、統編與標準答案相同;
    qr_damaged 解不開。用的是 src.verify.einvoice 的 load_image → decode_qr_texts → find_left_qr。"""
    from src.verify.einvoice import decode_qr_texts, find_left_qr, load_image
    out, labels = conditions
    invoices = [lb for lb in labels if lb["doc_type"] == "發票"]
    assert {lb["condition"] for lb in invoices} == {"clean", "blur", "tilt", "glare", "crop", "dark", "qr_damaged"}
    assert {lb["template"] for lb in invoices} == {"einvoice_a", "einvoice_b"}
    for label in invoices:
        qr = find_left_qr(decode_qr_texts(load_image(out / label["file"])))
        if label["condition"] == "qr_damaged":
            assert qr is None and label["qr"] == {"present": True, "readable": False}, label["file"]
            continue
        assert label["qr"] == {"present": True, "readable": True}
        assert qr is not None, f"{label['file']} 的左側 QR 解不出來"
        assert qr.date == label["date"] and qr.total_amount == label["amount"], label["file"]
        assert qr.invoice_number == label["invoice_number"]
        assert qr.seller_tax_id == label["fields"]["seller_tax_id"] and qr.random_code == label["fields"]["random_code"]


def test_qr_damage_touches_only_the_left_qr(conditions):
    """汙損的影像和同一張的 clean 只在左側 QR 附近不同:日期、金額、發票號碼的字與右側 QR 都沒動。"""
    out, labels = conditions
    files = {(lb["template"], lb["variant"], lb["condition"]): lb["file"] for lb in labels}
    damaged = [key for key in files if key[2] == "qr_damaged"]
    assert len(damaged) == 5
    for template, variant, _ in damaged:
        item = next(i for i in ev.plan(seed=11, per_template=5, templates=[template]) if i.variant == variant)
        x0, y0, x1, y1 = ev.TEMPLATES[template].render(item.spec).boxes("qr")[0]
        with Image.open(out / files[(template, variant, "clean")]) as clean, \
                Image.open(out / files[(template, variant, "qr_damaged")]) as bad:
            diff = np.abs(np.asarray(clean).astype(int) - np.asarray(bad).astype(int)).max(axis=2) > 0
        ys, xs = np.nonzero(diff)
        assert len(xs) > 2000, "汙損要看得出來"
        slack = 50                                                                      # 墨漬會稍微超出 QR 的邊
        assert xs.min() >= x0 - slack and xs.max() <= x1 + slack, (xs.min(), xs.max(), x0, x1)
        assert ys.min() >= y0 - slack and ys.max() <= y1 + slack, (ys.min(), ys.max(), y0, y1)


def test_every_image_in_every_condition_still_shows_the_sample_mark(generated, conditions):
    """「範例」標示:左上角的紅標籤與對角線的淡紅浮水印;再怎麼降質(含裁切、傾斜、偏暗)都看得到。"""
    for out, labels in (generated, conditions):
        for label in labels:
            arr = np.asarray(Image.open(out / label["file"]).convert("RGB")).astype(int)
            reddish = (arr[..., 0] - arr[..., 1] >= 8) & (arr[..., 0] >= 60)
            assert reddish.sum() > 5000, (label["file"], int(reddish.sum()))


def test_same_seed_gives_identical_labels_and_images(generated, tmp_path):
    """同 seed 重跑:labels.jsonl 逐位元相同、每張影像的雜湊相同(全部版型)。"""
    out, labels = generated
    again = ev.generate(tmp_path, seed=7, per_template=1)
    assert again == labels
    assert (tmp_path / "labels.jsonl").read_bytes() == (out / "labels.jsonl").read_bytes()
    assert len(labels) >= 27
    for label in labels:
        assert sha(tmp_path / label["file"]) == sha(out / label["file"]), label["file"]


def test_a_different_seed_changes_labels_and_images(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    names = ["bill_power", "einvoice_b"]
    labels_a, labels_b = (ev.generate(d, seed=s, per_template=1, templates=names) for d, s in ((a, 3), (b, 4)))
    assert labels_a != labels_b
    assert any(sha(a / x["file"]) != sha(b / y["file"]) for x, y in zip(labels_a, labels_b))


def test_regenerating_clears_the_previous_run_but_nothing_else(tmp_path):
    names = ["bill_power", "bill_water"]
    ev.generate(tmp_path, seed=3, per_template=2, templates=names)
    keep = tmp_path / "dev" / "notes.txt"
    keep.write_text("不是這個工具產生的", encoding="utf-8")
    stale = {p.name for p in (tmp_path / "dev").glob("*_02_*.png")}
    assert stale
    labels = ev.generate(tmp_path, seed=3, per_template=1, templates=names)
    assert keep.read_text(encoding="utf-8") == "不是這個工具產生的"
    assert {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.png")} == {lb["file"] for lb in labels}
    assert not stale & {p.name for p in (tmp_path / "dev").iterdir()}


def test_generate_aborts_before_writing_anything_when_a_label_breaks_the_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "validate_label", lambda label: ["故意的問題"])
    with pytest.raises(ValueError, match="契約"):
        ev.generate(tmp_path / "out", seed=1, per_template=1, templates=["bill_power"])
    assert not (tmp_path / "out").exists()


def test_generate_refuses_to_draw_without_a_chinese_font(tmp_path, monkeypatch):
    monkeypatch.setattr(ev.make_demo_docs, "_FONT_CANDIDATES", [str(tmp_path / "沒有這個字型.ttc")])
    with pytest.raises(RuntimeError, match="字型"):
        ev.generate(tmp_path / "out", seed=1, per_template=1, templates=["bill_power"])
    assert not (tmp_path / "out").exists()


def test_clear_previous_ignores_paths_outside_the_folder(tmp_path):
    out, outside = tmp_path / "out", tmp_path / "outside.png"
    out.mkdir()
    outside.write_bytes(b"x")
    (out / "labels.jsonl").write_text(
        json.dumps({"file": "../outside.png"}) + "\nnot json\n" + json.dumps({"file": 3}) + "\n", encoding="utf-8")
    assert ev.clear_previous(out) == 0 and outside.read_bytes() == b"x"


# --- 命令列 ---

def test_cli_generates_to_out_and_prints_a_summary(tmp_path, capsys):
    assert ev.main(["--out", str(tmp_path), "--per-template", "1", "--seed", "5",
                    "--templates", "bill_power, bill_gas"]) == 0
    printed = capsys.readouterr().out
    assert "labels.jsonl" in printed and "dev:" in printed and "test:" in printed and "seed=5" in printed
    lines = (tmp_path / "labels.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(list(tmp_path.rglob("*.png")))


def test_cli_templates_filter_and_bad_arguments(tmp_path, capsys):
    assert ev.main(["--out", str(tmp_path), "--per-template", "2", "--templates", "bill_gas"]) == 0
    rows = [json.loads(line) for line in (tmp_path / "labels.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {r["template"] for r in rows} == {"bill_gas"} and len(rows) == 4
    with pytest.raises(SystemExit) as exc:
        ev.main(["--out", str(tmp_path / "x"), "--templates", "nope"])
    assert exc.value.code == 2 and "nope" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        ev.main(["--out", str(tmp_path / "y"), "--per-template", "0"])
    assert exc.value.code == 2


def test_default_output_is_the_ignored_eval_folder():
    assert ev.DEFAULT_OUT == ev.BASE / "data" / "samples" / "eval"
