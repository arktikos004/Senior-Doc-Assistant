"""欄位規格(web/fields.py)的測試:結果頁、轉人工原因、重點整理共用的欄位名稱、種類、必填與順序。"""
import pytest

from samples import BILL
from src.models import REQUIRED_FIELDS
from web.fields import field_spec, field_specs, required_specs
from web.render import field_rows, review_reason


@pytest.mark.parametrize("doc_type", sorted(REQUIRED_FIELDS))
def test_every_required_field_has_a_chinese_spec(doc_type):
    specs = {s.key: s for s in field_specs(doc_type)}
    assert all(specs[key].required for key in REQUIRED_FIELDS[doc_type])     # 必要欄位一定在規格內
    assert [s.key for s in required_specs(doc_type)] == list(REQUIRED_FIELDS[doc_type])
    assert not any(s.label.isascii() for s in specs.values())                 # 長輩看不到英文欄位名


def test_labels_follow_the_document_type():
    assert field_spec("公文", "vendor").label == "發文機關" and field_spec("藥袋", "date").label == "調劑日期"
    assert field_spec("發票", "vendor").label == "商家" and field_spec("帳單", "fields.due_date").label == "繳費期限"
    assert field_spec("收據", "fields.新欄位").label == "新欄位"                 # 模型多給的欄位用原名


def test_kinds_decide_how_values_are_shown():
    assert field_spec("帳單", "amount").kind == "amount"
    assert field_spec("公文", "date").kind == field_spec("公文", "fields.deadline").kind == "date"
    assert field_spec("公文", "fields.required_actions").kind == "list"
    assert field_spec("發票", "fields.seller_tax_id").kind == "text"


def test_field_table_follows_the_spec_order():
    assert [r["label"] for r in field_rows(BILL)] == ["金額", "繳費期限", "帳單種類", "開單單位", "開單日期"]


def test_unsupported_type_shows_common_fields_but_requires_nothing():
    assert [s.key for s in field_specs("其他")] == ["date", "vendor", "amount", "invoice_number"]
    assert required_specs("其他") == () and not field_spec("其他", "amount").required
    assert "不在支援範圍" in review_reason({"doc_type": "其他"})


def test_missing_fields_are_named_in_required_order():
    assert review_reason({"doc_type": "公文", "fields": {}}) == "有必要的欄位沒讀到(發文日期、主旨),請對照原件補上。"


def test_medication_bag_without_its_list_says_so_in_chinese():
    """藥袋讀不到藥品清單時,原因原本寫出英文鍵名「items」;改用 src.models.FIELD_LABELS 的「藥品清單」。"""
    result = {"doc_type": "藥袋", "date": "2026-09-30", "fields": {}}
    assert review_reason(result) == "有必要的欄位沒讀到(藥品清單),請對照原件補上。"
