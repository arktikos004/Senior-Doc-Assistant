"""網頁測試共用的合成樣本(AI 讀值的形狀同 src/models.py)。

商家、機關都是虛構的「示範」;期限一律在 2099 年,測試不會因為日子過了而突然失敗。
各測試要改某個欄位時用 dict(BILL, ...) 複製一份,不要直接改這裡的物件。
"""

BILL = {
    "doc_type": "帳單", "date": "2026-09-20", "vendor": "示範電力公司", "amount": 1854.0, "currency": "NTD",
    "plain_summary": "這是電費帳單,要在10月15日前繳1854元。",
    "fields": {"due_date": "2099-10-15", "bill_kind": "電費"},
    "verification": {}, "verified_confidence": None, "unreadable": [],
}

LETTER = {
    "doc_type": "公文", "date": "2026-09-25", "vendor": "示範區公所", "plain_summary": "",
    "fields": {"subject": "請補繳文件", "doc_number": "示範字第1150000001號",
               "deadline_text": "收到本函後15日內", "deadline": "2099-10-10",
               "required_actions": ["補繳身分證影本", "親自簽名"]},
}

INVOICE = {"doc_type": "發票", "date": "2026-09-18", "vendor": "示範超市", "amount": 320.0, "currency": "NTD"}

# 帳單自動列入的期限提醒(行動 payload,W1-C 的形狀)
REMINDER = {"title": "繳電費", "date": "2099-10-15", "description": "繳費期限:2099-10-15"}
