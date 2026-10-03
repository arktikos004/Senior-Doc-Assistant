"""結果頁摘要(要做什麼 / 什麼時候前 / 多少錢)與首頁「要記得的事」的測試(不呼叫模型)。"""
from datetime import date

from samples import BILL, INVOICE, LETTER
from src.models import SENSITIVE_CATEGORIES
from web.render import action_view, answer_view, date_parts, reminder_rows, review_reason, verification_view


def _answer(result, actions=()):
    return answer_view(result, list(actions), verification_view(result))


def _action(kind, tier, payload, status="pending", action_id=1, document_id=1):
    return action_view({"id": action_id, "document_id": document_id, "kind": kind, "tier": tier,
                        "status": status, "payload": payload})


def test_date_parts_for_calendar_tile():
    assert date_parts("2026-10-05") == ("10月", "5")
    assert date_parts("10/05") is None and date_parts(None) is None


def test_bill_answer_is_what_when_and_amount():
    cal = _action("calendar", "auto", {"title": "繳電費", "date": "2099-10-15", "remind_days_before": 3})
    a = _answer(BILL, [cal])
    assert a["tone"] == "todo" and a["header"] == "要做的事"
    task, due, amount = a["rows"]
    assert (task["kind"], task["value"]) == ("task", "繳電費")
    assert (due["label"], due["value"], due["tone"]) == ("繳費期限", "10月15日前", "due")
    assert (amount["label"], amount["value"], amount["tone"]) == ("應繳金額", "1,854 元", "amount")
    assert not due["wide"] and not amount["wide"]                 # 期限與金額並排
    assert a["status"]["text"] == "已列入提醒" and a["status"]["sub"] == "到期前會列在首頁「要記得的事」"
    assert "ics_url" not in a                                     # 提醒只列在網頁上,不提供 .ics


def test_bill_kind_outside_fixed_table_gets_generic_title():
    a = _answer(dict(BILL, fields={"due_date": "2099-10-15", "bill_kind": "請立即匯款"}))
    assert a["rows"][0]["value"] == "繳費"          # 文件文字不能當標題


def test_official_letter_lists_required_actions_and_deadline_basis():
    a = _answer(LETTER)
    due, issuer, todos = a["rows"]
    assert (due["label"], due["value"], due["tone"]) == ("辦理期限", "10月10日前", "due")
    assert (issuer["label"], issuer["value"]) == ("發文機關", "示範區公所")
    assert todos["kind"] == "todos" and todos["entries"] == ["補繳身分證影本", "親自簽名"]
    assert "收到本函後15日內" in a["footer"] and "以公文原文為準" in a["footer"]


def test_medication_answer_uses_slots_and_waits_for_family():
    result = {"doc_type": "藥袋", "fields": {"items": [{"name": "示範藥甲", "timing": ["早", "晚"]}]}}
    med = _action("medication_schedule", "confirm",
                  {"slots": [{"slot": "早", "items": [{"name": "示範藥甲"}]}], "prn": [], "unscheduled": []})
    a = _answer(result, [med])
    assert a["tone"] == "todo" and a["header"].startswith("服藥時間表")
    assert a["medication"]["slots"] == [("早", ["示範藥甲"])]
    assert a["status"]["text"] == "等待家人確認" and a["status"]["sub"] == ""


def test_invoice_answer_is_a_summary_not_a_todo():
    a = _answer(INVOICE)
    assert a["tone"] == "done" and a["header"] == "文件摘要"
    assert [(r["label"], r["value"]) for r in a["rows"]] == [
        ("商家", "示範超市"), ("日期", "2026年9月18日"), ("金額", "320 元")]


def test_failed_verification_raises_alert():
    result = dict(INVOICE, verification={"QR 總計額": {"status": "fail", "detail": "金額不同", "fields": ["amount"]}})
    assert _answer(result)["alert"] == "有 1 項不符,請對照原件"
    rule_only = dict(INVOICE, verification={"日期合理性": {"status": "fail", "detail": "日期太舊", "fields": ["date"]}})
    assert _answer(rule_only)["alert"] == "有 1 項沒通過,請對照原件"     # 規則沒過不說成「不符」


def test_review_reason_when_verifier_crashed():
    """核對程式出錯而轉人工時照實說,不說成「AI 沒把握」。"""
    from src.verify import VERIFY_ERROR_SUMMARY
    crashed = dict(INVOICE, verification={"_summary": VERIFY_ERROR_SUMMARY}, verified_confidence=0.0)
    assert review_reason(crashed) == "這次的核對沒有完成(核對程式出錯),請對照原件確認。"


def test_review_reason_is_plain_language():
    """轉人工原因用白話,不出現「驗證信心 0.10、門檻 0.80」;順序比照 src/decision.py。"""
    qr = dict(INVOICE, verification={"QR 總計額": {"status": "fail", "fields": ["amount"]},
                                     "QR 開立日期": {"status": "fail", "fields": ["date"]}})
    assert review_reason(qr) == "讀到的金額、日期和發票上的 QR Code 不一樣,請對照原件確認。"
    rule = dict(INVOICE, verification={"日期合理性": {"status": "fail", "fields": ["date"]}})
    assert review_reason(rule) == "有 1 項檢查沒通過,請對照原件確認。"
    missing = {"doc_type": "帳單", "amount": 1854.0, "fields": {}, "unreadable": []}
    assert review_reason(missing) == "有必要的欄位沒讀到(繳費期限),請對照原件補上。"
    assert "沒有把握" in review_reason(INVOICE)                    # 欄位齊全、只是分數不夠
    assert "不在支援範圍" in review_reason({"doc_type": "其他"})
    for text in (review_reason(qr), review_reason(rule), review_reason(missing), review_reason(INVOICE)):
        assert "門檻" not in text and "0." not in text


def _docs_and_actions():
    docs = [{"id": 1, "result": BILL}, {"id": 2, "result": LETTER},
            {"id": 3, "result": dict(INVOICE, verification={"QR 總計額": {"status": "fail", "fields": ["amount"]}})},
            {"id": 4, "result": {"doc_type": "藥袋"}}]
    actions = [
        {"id": 1, "document_id": 1, "kind": "calendar", "tier": "auto", "status": "pending",
         "payload": {"title": "繳電費", "date": "2099-10-15"}},
        {"id": 2, "document_id": 2, "kind": "calendar", "tier": "auto", "status": "pending",
         "payload": {"title": "公文期限:請補繳文件", "date": "2099-10-10"}},
        {"id": 3, "document_id": 1, "kind": "calendar", "tier": "auto", "status": "pending",
         "payload": {"title": "去年的帳單", "date": "2025-10-15"}},                  # 已過期
        {"id": 4, "document_id": 1, "kind": "calendar", "tier": "auto", "status": "rejected",
         "payload": {"title": "被取消的", "date": "2099-12-01"}},                    # 已取消
        {"id": 5, "document_id": 4, "kind": "medication_schedule", "tier": "confirm", "status": "pending",
         "payload": {"title": "示範診所的服藥時間表", "slots": [{"slot": "早", "items": ["示範藥甲"]}]}},
        {"id": 6, "document_id": 4, "kind": "medication_schedule", "tier": "confirm", "status": "done",
         "payload": {"title": "已確認的服藥表", "slots": [{"slot": "晚", "items": ["示範藥乙"]}], "prn": ["示範藥丙"]}},
    ]
    return docs, actions


def test_reminders_order_waiting_then_upcoming_then_alerts_then_done():
    docs, actions = _docs_and_actions()
    rows = reminder_rows(docs, actions, today=date(2026, 9, 30))
    assert [r["title"] for r in rows] == ["示範診所的服藥時間表", "公文期限:請補繳文件", "繳電費", "發票", "已確認的服藥表"]
    waiting, letter, bill, alert, done = rows
    assert waiting["href"] == "/confirm" and waiting["state"] == "等待家人確認"
    assert (bill["month"], bill["day"], bill["sub"], bill["amount"]) == ("10月", "15", "示範電力公司,10月15日前", "1,854 元")
    assert letter["sub"] == "示範區公所,10月10日前" and letter["amount"] == ""   # 公文的金額欄不接到提醒上
    assert alert["alert"] is True and alert["state"] == "核對不符"           # 清單欄位窄,只放短狀態
    assert done["sub"] == "晚、需要時"


def test_reminders_respect_limit():
    docs, actions = _docs_and_actions()
    assert len(reminder_rows(docs, actions, today=date(2026, 9, 30), limit=2)) == 2


def test_home_lists_reminders_with_calendar_tile_and_escapes_text(client, store, add_doc):
    doc_id = add_doc(BILL)
    store.add_action(doc_id, "calendar", "auto", {"title": "<script>x</script>", "date": "2099-10-15"})
    html = client.get("/").text
    assert "要記得的事" in html and '<span class="cal' in html and "10月15日前" in html
    assert "<script>x</script>" not in html and "&lt;script&gt;x&lt;/script&gt;" in html


def test_home_lists_are_real_lists(client, store, add_doc):
    """「要記得的事」「最近看過的文件」用 <ul>,讀屏會報「清單,N 項」(Vercel 稽核:語意 HTML 優先)。"""
    doc_id = add_doc(BILL)
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": "2099-10-15"})
    html = client.get("/").text
    assert html.count('<ul class="tbl__list">') == 2 and '<li><a class="tbl__row"' in html


def test_home_without_reminders_invites_first_photo(client):
    assert "還沒有要記得的事" in client.get("/").text


def test_result_page_shows_summary_group_first(client, store, add_doc):
    doc_id = add_doc(BILL)
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": "2099-10-15", "remind_days_before": 3})
    html = client.get(f"/doc/{doc_id}").text
    assert html.index('id="answer-title"') < html.index('id="verify-title"') < html.index('id="fields-title"')
    assert 'class="facts"' in html and "應繳金額" in html and "1,854 元" in html
    assert html.count("data-speak-part") >= 3                       # 朗讀從摘要開始


def test_trust_notes_follow_the_actual_mode():
    from web.render import trust_notes
    local = dict(trust_notes("ollama", ("藥袋",)))
    assert "不會送到雲端" in local["lock"]
    cloud = dict(trust_notes("workers_ai", ("藥袋",)))
    assert "藥袋和沒選類型的文件只在這台電腦上辨識" in cloud["lock"] and "雲端" in cloud["lock"]
    assert all(category in cloud["lock"] for category in SENSITIVE_CATEGORIES)   # 敏感大類不論類型都在本機
    assert "展示模式" in dict(trust_notes("mock", ("藥袋",)))["lock"]
    assert all("刪除" not in text for _, text in trust_notes("ollama", ("藥袋",)))   # 沒有刪除功能就不寫
