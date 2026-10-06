"""資料模型:AI 辨識結果與決策結果的結構定義。"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

# 系統認得的文件類型。發票/收據沿用原歸檔流程;藥袋/帳單/公文為「看有」新增,
# 由行動模組(src/actions)轉成提醒或待辦。其餘一律歸「其他」並轉人工。
DOC_TYPES: tuple[str, ...] = ("發票", "收據", "藥袋", "帳單", "公文", "其他")

# 共通欄位名稱,模型可在 unreadable 中標記「看得到但讀不清」的欄位
COMMON_FIELDS: tuple[str, ...] = ("doc_type", "date", "vendor", "amount", "currency", "invoice_number")

# 類型專屬欄位契約(放在 ExtractionResult.fields,名稱固定,prompt / 驗證 / 行動 / 網頁共用):
#   發票:seller_tax_id 賣方統編、buyer_tax_id 買方統編、random_code 隨機碼、period 期別原文
#   帳單:due_date 繳費期限 YYYY-MM-DD、bill_kind 電費/水費/瓦斯/電信/其他
#   公文:subject 主旨、doc_number 發文字號、deadline_text 期限原文(例「收到本函後15日內」)、
#         deadline 由程式計算的 YYYY-MM-DD(模型不填)、required_actions 應辦事項(字串清單)
#   藥袋:items 藥品清單,每項 {name, dose_text, frequency_text, timing, prn, days};
#         timing 為「早/中/晚/睡前」清單,prn 表示「需要時」服用;pharmacist_phone 藥師電話
# 共通欄位對應:公文的 vendor 為發文機關、date 為發文日期;藥袋的 vendor 為醫療院所、date 為調劑日期。

# 各類型的必要欄位:缺漏或被模型標成讀不清就轉人工(見 decision.decide);
# "fields.x" 指 ExtractionResult.fields["x"]
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "發票": ("date", "amount"),
    "收據": ("date", "amount"),
    "帳單": ("amount", "fields.due_date"),
    "公文": ("date", "fields.subject"),
    "藥袋": ("date", "fields.items"),
}

FIELD_LABELS: dict[str, str] = {
    "date": "日期", "amount": "金額", "vendor": "商家", "invoice_number": "發票號碼",
    "fields.due_date": "繳費期限", "fields.subject": "主旨", "fields.items": "藥品清單",
}


# 文件四大類(使用者 10/2 決定):整理、瀏覽與隱私分流用的上層分類;擷取、核對與行動仍以 DOC_TYPES 為準
CATEGORIES: tuple[str, ...] = ("身分證明", "財產資產", "醫療與保險", "生活契約")
UNCATEGORIZED = "未分類"   # 公文等要看內容才分得出來、上傳時又沒選大類的文件,由家人指定
# 上傳時每個大類可以再選的常見文件(使用者 10/3 決定;順序就是畫面第二列的順序,畫面最後再加「不確定」):
# (名稱, 類型提示)。類型提示是能自動判讀的類型(稅單、管理費當帳單讀,醫療收據當收據讀);
# None 表示交給家人確認:照樣能上傳,但只在本機辨識、一律交給家人複核,畫面顯示家人選的名稱
CATEGORY_DOCS: dict[str, tuple[tuple[str, str | None], ...]] = {
    "身分證明": (("身分證", None), ("健保卡", None), ("戶口名簿", None), ("駕照", None), ("護照", None),
                ("印鑑證明", None)),
    "財產資產": (("發票", "發票"), ("收據", "收據"), ("稅單", "帳單"), ("存摺", None), ("房地權狀", None),
                ("公文", "公文")),
    "醫療與保險": (("藥袋", "藥袋"), ("醫療收據", "收據"), ("保單", None), ("診斷證明", None), ("檢驗報告", None),
                  ("公文", "公文")),
    "生活契約": (("水電瓦斯費", "帳單"), ("電信費", "帳單"), ("管理費", "帳單"), ("租約", None), ("公文", "公文")),
}
# 清單上的名稱 → 類型提示(同一個名稱在每個大類的提示都一樣,例如公文);畫面只顯示這裡有的名稱
DOC_LABELS: dict[str, str | None] = {name: hint for docs in CATEGORY_DOCS.values() for name, hint in docs}
# 每個大類能自動判讀的文件類型,由 CATEGORY_DOCS 推出(依清單順序);身分證明類沒有
CATEGORY_TYPES: dict[str, tuple[str, ...]] = {
    category: tuple(dict.fromkeys(hint for _, hint in docs if hint)) for category, docs in CATEGORY_DOCS.items()
}
# 沒選大類時依文件類型歸類;公文要看內容(稅務、健保、戶政…),歸「未分類」
TYPE_CATEGORY: dict[str, str] = {"發票": "財產資產", "收據": "財產資產", "藥袋": "醫療與保險", "帳單": "生活契約"}
# 屬敏感個資的大類:不論文件類型都只在本機辨識(同藥袋)
SENSITIVE_CATEGORIES: frozenset[str] = frozenset({"身分證明", "醫療與保險"})
# 身分證明類交給家人確認:選了這一類,不論讀成什麼都轉人工
IDENTITY_CATEGORY = "身分證明"


def category_for(doc_type: str | None, chosen: str | None = None) -> str:
    """文件歸哪一類:上傳時選了大類就照選的;沒選就依文件類型,分不出來(公文、其他)歸「未分類」。"""
    if chosen in CATEGORIES:
        return chosen
    return TYPE_CATEGORY.get(doc_type or "", UNCATEGORIZED)


def catalog_entry(category: str | None, name: str | None) -> tuple[str, str | None] | None:
    """上傳時選的大類與文件名稱 → 清單上的那一項 (名稱, 類型提示);名稱不在這一類的清單(或沒選大類)回 None。

    回傳的是清單裡的值,不是傳進來的字串:名稱與類型提示一律取自固定清單,表單或文件上的文字選不了(原則 3)。
    """
    docs = CATEGORY_DOCS[category] if category in CATEGORIES else ()
    return next((entry for entry in docs if entry[0] == name), None)


@dataclass
class ExtractionResult:
    """多模態 AI 對單一文件的辨識結果。"""

    doc_type: str                     # DOC_TYPES 之一
    date: str | None = None           # YYYY-MM-DD
    vendor: str | None = None         # 商家 / 開立單位(公文為發文機關、藥袋為醫療院所)
    amount: float | None = None       # 總金額
    currency: str = "NTD"             # 幣別(NTD/USD/…);對帳需分幣別加總、不可混算
    invoice_number: str | None = None # 發票號碼(收據可為空)
    confidence: float = 0.0           # 模型自評信心 0~1(弱訊號,決策改看 verified_confidence)
    notes: str = ""                   # 模型補充說明(模糊、缺角等)
    raw: dict[str, Any] = field(default_factory=dict)  # 模型原始回應
    # --- 棄答、類型專屬欄位、白話解說,以及後續步驟(行動、核對)填入的結果 ---
    unreadable: list[str] = field(default_factory=list)  # 模型自認讀不清的欄位(棄答,不猜)
    fields: dict[str, Any] = field(default_factory=dict)  # 類型專屬欄位(藥袋品項、繳費期限…)
    plain_summary: str = ""           # 給長輩看的白話解說
    actions: list[dict[str, Any]] = field(default_factory=list)  # 由 src/actions 產生的行動
    verification: dict[str, Any] = field(default_factory=dict)   # 由 src/verify 產生的逐項驗證結果
    verified_confidence: float | None = None  # 由驗證一致度算出的信心;None 表示尚未驗證
    source_model: str = ""            # 產生此結果的 provider:模型,例 "ollama:gemma4:12b"

    def get_field(self, name: str) -> Any:
        """依 REQUIRED_FIELDS 的命名取值:'amount' 或 'fields.due_date'。"""
        if name.startswith("fields."):
            return self.fields.get(name.split(".", 1)[1])
        return getattr(self, name, None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_type": self.doc_type,
            "date": self.date,
            "vendor": self.vendor,
            "amount": self.amount,
            "currency": self.currency,
            "invoice_number": self.invoice_number,
            "confidence": self.confidence,
            "notes": self.notes,
            "unreadable": list(self.unreadable),
            "fields": dict(self.fields),
            "plain_summary": self.plain_summary,
            "actions": list(self.actions),
            "verification": dict(self.verification),
            "verified_confidence": self.verified_confidence,
            "source_model": self.source_model,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ExtractionResult:
        """從 to_dict() 的結果(SQLite 的 result_json)還原;家人更正讀值時拿它當底,再重新核對。

        只還原、不重新清洗(讀值當初已經過 parsing.to_result):缺的鍵用預設值、不認得的鍵略過,
        型別不對的值當成沒有,舊資料或格式壞掉的紀錄也不會讓更正頁壞掉。內層的清單與 dict 都複製一份,
        改還原出來的物件不會動到傳進來的資料。
        """
        data = copy.deepcopy(data) if isinstance(data, dict) else {}

        def text(key: str) -> str | None:
            value = data.get(key)
            return value if isinstance(value, str) else None

        def number(key: str) -> float | None:
            value = data.get(key)   # bool 不算數字:True 不是 1 元
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        def of_type(key: str, kind: type) -> Any:
            value = data.get(key)
            return value if isinstance(value, kind) else kind()

        return cls(
            doc_type=text("doc_type") or "其他",
            date=text("date"),
            vendor=text("vendor"),
            amount=number("amount"),
            currency=text("currency") or "NTD",
            invoice_number=text("invoice_number"),
            confidence=number("confidence") or 0.0,
            notes=text("notes") or "",
            unreadable=[u for u in of_type("unreadable", list) if isinstance(u, str)],
            fields=of_type("fields", dict),
            plain_summary=text("plain_summary") or "",
            actions=of_type("actions", list),
            verification=of_type("verification", dict),
            verified_confidence=number("verified_confidence"),
            source_model=text("source_model") or "",
        )


@dataclass
class Decision:
    """決策代理的判斷結果。"""

    action: str   # archive / review / failed
    reason: str   # 判斷理由(寫入 log)
