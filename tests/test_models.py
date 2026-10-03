"""資料模型測試:ExtractionResult 從 SQLite 的 result_json 還原(家人更正讀值時的底)。"""
from src.models import ExtractionResult


def _full() -> ExtractionResult:
    return ExtractionResult(
        doc_type="帳單", date="2026-09-20", vendor="範例電力公司", amount=1286.0, currency="NTD",
        invoice_number=None, confidence=0.55, notes="金額處有點模糊", unreadable=["fields.due_date"],
        fields={"due_date": "2026-10-20", "bill_kind": "電費"}, plain_summary="這是電費帳單。",
        actions=[{"kind": "calendar", "tier": "confirm", "payload": {"title": "繳電費"}}],
        verification={"繳費期限": {"status": "pass", "detail": "", "fields": ["fields.due_date"]}},
        verified_confidence=0.55, source_model="mock:test",
    )


def test_round_trip_keeps_every_key():
    original = _full()
    assert ExtractionResult.from_dict(original.to_dict()).to_dict() == original.to_dict()


def test_missing_keys_take_defaults():
    restored = ExtractionResult.from_dict({"doc_type": "公文"})
    assert restored.doc_type == "公文" and restored.currency == "NTD" and restored.confidence == 0.0
    assert restored.fields == {} and restored.unreadable == [] and restored.verified_confidence is None
    assert ExtractionResult.from_dict(None).doc_type == "其他"


def test_wrong_types_count_as_missing():
    """舊資料或格式壞掉的紀錄:型別不對就當成沒有,不讓更正頁壞掉;bool 不是數字。"""
    restored = ExtractionResult.from_dict({
        "doc_type": 3, "amount": True, "confidence": "0.9", "fields": ["x"], "unreadable": ["date", 5],
        "verification": "fail", "verified_confidence": "高", "actions": {"kind": "payment"},
    })
    assert restored.doc_type == "其他" and restored.amount is None and restored.confidence == 0.0
    assert restored.fields == {} and restored.unreadable == ["date"] and restored.verification == {}
    assert restored.verified_confidence is None and restored.actions == []


def test_restored_object_does_not_share_inner_data():
    data = _full().to_dict()
    data["fields"]["items"] = [{"name": "範例錠A"}]
    restored = ExtractionResult.from_dict(data)
    restored.fields["items"][0]["name"] = "改過的"
    restored.fields["due_date"] = "2099-01-01"
    assert data["fields"]["items"][0]["name"] == "範例錠A" and data["fields"]["due_date"] == "2026-10-20"


# ---- 文件四大類(使用者 10/2 決定) ------------------------------------------------

def test_category_follows_family_choice_then_document_type():
    from src.models import category_for
    assert category_for("公文", "財產資產") == "財產資產"        # 上傳時選了大類就照選的
    assert category_for("藥袋") == "醫療與保險"
    assert category_for("發票") == category_for("收據") == "財產資產"
    assert category_for("帳單") == "生活契約"
    assert category_for("公文") == category_for("其他") == category_for(None) == "未分類"
    assert category_for("帳單", "亂填") == "生活契約"           # 不認得的大類當作沒選


def test_category_tables_are_consistent():
    from src.models import CATEGORIES, CATEGORY_TYPES, DOC_TYPES, SENSITIVE_CATEGORIES, TYPE_CATEGORY
    assert tuple(CATEGORY_TYPES) == CATEGORIES
    assert all(t in DOC_TYPES for types in CATEGORY_TYPES.values() for t in types)
    assert SENSITIVE_CATEGORIES <= set(CATEGORIES) and {"醫療與保險", "身分證明"} <= SENSITIVE_CATEGORIES
    assert set(TYPE_CATEGORY.values()) <= set(CATEGORIES)
    for doc_type, category in TYPE_CATEGORY.items():           # 預設歸類一定是上傳時選得到的組合
        assert doc_type in CATEGORY_TYPES[category]


# ---- 上傳選項清單(使用者 10/3 決定) ------------------------------------------------

def test_upload_catalog_is_the_users_list():
    """每類的常見文件與順序照使用者 10/3 的決定;類型提示只用初賽能自動判讀的五種,None 是還不能自動判讀。"""
    from src.models import CATEGORIES, CATEGORY_DOCS
    assert tuple(CATEGORY_DOCS) == CATEGORIES
    assert CATEGORY_DOCS == {
        "身分證明": (("身分證", None), ("健保卡", None), ("戶口名簿", None), ("駕照", None), ("護照", None),
                    ("印鑑證明", None)),
        "財產資產": (("發票", "發票"), ("收據", "收據"), ("稅單", "帳單"), ("存摺", None), ("房地權狀", None),
                    ("公文", "公文")),
        "醫療與保險": (("藥袋", "藥袋"), ("醫療收據", "收據"), ("保單", None), ("診斷證明", None),
                      ("檢驗報告", None), ("公文", "公文")),
        "生活契約": (("水電瓦斯費", "帳單"), ("電信費", "帳單"), ("管理費", "帳單"), ("租約", None),
                    ("公文", "公文")),
    }


def test_upload_catalog_is_consistent_with_the_types():
    """清單和類型表對得起來:提示只能是能自動判讀的類型;名稱在同一類不重複、沒有空白(app.js 用空白分隔);
    同一個名稱在每一類的提示一樣;CATEGORY_TYPES 是每類能自動判讀的類型(由清單推出)。"""
    from src.models import CATEGORY_DOCS, CATEGORY_TYPES, DOC_LABELS, REQUIRED_FIELDS
    readable = set(REQUIRED_FIELDS)                       # 五種:發票、收據、帳單、公文、藥袋
    for category, docs in CATEGORY_DOCS.items():
        names = [name for name, _ in docs]
        assert len(names) == len(set(names)) and all(name and name == "".join(name.split()) for name in names)
        assert all(hint is None or hint in readable for _, hint in docs)
        assert all(DOC_LABELS[name] == hint for name, hint in docs)
        assert set(CATEGORY_TYPES[category]) == {hint for _, hint in docs if hint}
    assert CATEGORY_TYPES == {"身分證明": (), "財產資產": ("發票", "收據", "帳單", "公文"),
                              "醫療與保險": ("藥袋", "收據", "公文"), "生活契約": ("帳單", "公文")}


def test_catalog_entry_only_knows_the_chosen_categorys_list():
    from src.models import catalog_entry
    assert catalog_entry("財產資產", "稅單") == ("稅單", "帳單")
    assert catalog_entry("醫療與保險", "保單") == ("保單", None)
    assert catalog_entry("生活契約", "水電瓦斯費") == ("水電瓦斯費", "帳單")
    assert catalog_entry("身分證明", "身分證") == ("身分證", None)
    assert catalog_entry("生活契約", "公文") == ("公文", "公文")
    # 別類的名稱、多了空白、類型名稱不是清單上的名稱、沒選大類、「未分類」、不確定:都不算
    for category, name in [("生活契約", "保單"), ("財產資產", "帳單"), ("醫療與保險", "保單 "), (None, "發票"),
                           ("未分類", "公文"), ("醫療與保險", "不確定"), ("醫療與保險", None)]:
        assert catalog_entry(category, name) is None, (category, name)
