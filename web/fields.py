"""各文件類型的欄位規格:畫面上的欄位名稱、值的種類、是否必填、顯示順序。

由 src.models 的 REQUIRED_FIELDS(必要欄位)與 FIELD_LABELS(欄位中文名)加上各類型的別名組成
(公文的 vendor 是「發文機關」、藥袋的 date 是「調劑日期」…)。結果頁「讀到的內容」、轉人工的原因、
重點整理都從這裡取欄位名,改名只要改一處;之後「家人更正任何類型」的表單也照這份規格產生欄位。
欄位鍵的寫法同 REQUIRED_FIELDS:共通欄位寫 "amount",類型專屬欄位寫 "fields.due_date"。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.models import FIELD_LABELS, REQUIRED_FIELDS

FieldKind = Literal["text", "date", "amount", "list"]


@dataclass(frozen=True)
class FieldSpec:
    """一個欄位的規格。kind 決定值怎麼顯示(之後的更正表單也用它決定輸入方式)。"""

    key: str            # "amount" 或 "fields.due_date"
    label: str          # 畫面上的欄位名稱
    kind: FieldKind     # text 文字 / date 日期 YYYY-MM-DD / amount 金額 / list 清單
    required: bool      # 自動存檔前必須讀到(REQUIRED_FIELDS)


# 同一個欄位在不同類型有不同的叫法(src/models.py 的契約註解)
_TYPE_ALIASES: dict[str, dict[str, str]] = {
    "公文": {"vendor": "發文機關", "date": "發文日期"},
    "藥袋": {"vendor": "醫療院所", "date": "調劑日期"},
    "帳單": {"vendor": "開單單位", "date": "開單日期"},
}
# FIELD_LABELS 沒列到的類型專屬欄位(鍵名依 src/models.py 的契約註解)
_MORE_LABELS: dict[str, str] = {
    "fields.seller_tax_id": "賣方統編", "fields.buyer_tax_id": "買方統編", "fields.random_code": "隨機碼",
    "fields.period": "期別", "fields.bill_kind": "帳單種類", "fields.doc_number": "發文字號",
    "fields.deadline_text": "期限原文", "fields.deadline": "期限(推算)", "fields.required_actions": "應辦事項",
    "fields.pharmacist_phone": "藥師電話",
}
_KINDS: dict[str, FieldKind] = {
    "date": "date", "fields.due_date": "date", "fields.deadline": "date",
    "amount": "amount",
    "fields.required_actions": "list", "fields.items": "list",
}
# 各類型的欄位順序,最重要的在前;必要欄位沒列到的會接在最後(field_specs 保證必要欄位都在)
_ORDER: dict[str, tuple[str, ...]] = {
    "帳單": ("amount", "fields.due_date", "fields.bill_kind", "vendor", "date"),
    "公文": ("fields.subject", "fields.deadline", "fields.deadline_text", "fields.required_actions",
             "vendor", "date", "fields.doc_number"),
    "藥袋": ("vendor", "date", "fields.pharmacist_phone"),
    "發票": ("date", "vendor", "amount", "invoice_number", "fields.period",
             "fields.seller_tax_id", "fields.buyer_tax_id", "fields.random_code"),
    "收據": ("date", "vendor", "amount"),
}
_DEFAULT_ORDER = ("date", "vendor", "amount", "invoice_number")


def field_spec(doc_type: str, key: str) -> FieldSpec:
    """任一欄位的規格;模型多給、規格裡沒有的類型專屬欄位,名稱用原鍵名、當成文字。"""
    label = (_TYPE_ALIASES.get(doc_type, {}).get(key) or FIELD_LABELS.get(key) or _MORE_LABELS.get(key)
             or key.removeprefix("fields."))
    return FieldSpec(key, label, _KINDS.get(key, "text"), key in REQUIRED_FIELDS.get(doc_type, ()))


def field_specs(doc_type: str) -> tuple[FieldSpec, ...]:
    """這個類型在畫面上的欄位,依顯示順序;必要欄位一定在內。不支援的類型用共通欄位。"""
    order = _ORDER.get(doc_type, _DEFAULT_ORDER)
    keys = order + tuple(k for k in REQUIRED_FIELDS.get(doc_type, ()) if k not in order)
    return tuple(field_spec(doc_type, key) for key in keys)


def required_specs(doc_type: str) -> tuple[FieldSpec, ...]:
    """必要欄位,順序同 REQUIRED_FIELDS(轉人工原因照這個順序列欄位名);不支援的類型是空的。"""
    return tuple(field_spec(doc_type, key) for key in REQUIRED_FIELDS.get(doc_type, ()))
