"""結果頁第一屏的畫面資料(這是什麼 / 多少錢或要做什麼 / 什麼時候前)與首頁「要記得的事」的測試(不呼叫模型)。
第一屏長什麼樣、誰看到什麼在 tests/test_web_result_first.py。"""
from datetime import date

from samples import BILL, INVOICE, LETTER
from web.render import action_view, answer_view, date_parts, doc_lead, reminder_rows, review_reason, verification_view


def _answer(result, actions=()):
    return answer_view(result, list(actions), verification_view(result))


def _action(kind, tier, payload, status="pending", action_id=1, document_id=1):
    return action_view({"id": action_id, "document_id": document_id, "kind": kind, "tier": tier,
                        "status": status, "payload": payload})


def test_date_parts_for_calendar_tile():
    assert date_parts("2026-10-05") == ("10月", "5")
    assert date_parts("10/05") is None and date_parts(None) is None


def test_bill_answer_is_amount_then_deadline():
    cal = _action("calendar", "auto", {"title": "繳電費", "date": "2099-10-15", "remind_days_before": 3})
    a = _answer(BILL, [cal])
    amount, due = a["lines"]
    assert (amount["key"], amount["value"], amount["big"]) == ("amount", "1,854 元", True)
    assert (due["key"], due["value"], due["big"]) == ("fields.due_date", "10月15日前繳費", True)
    assert not amount["label"] and not due["label"]               # 值自己就說得清楚,不用欄名
    assert a["status"]["text"] == "已列入提醒"
    assert "ics_url" not in a                                     # 提醒只列在網頁上,不提供 .ics


def test_bill_kind_outside_fixed_table_stays_out_of_the_answer():
    result = dict(BILL, fields={"due_date": "2099-10-15", "bill_kind": "請立即匯款"})
    assert _answer(result)["lines"][1]["value"] == "10月15日前繳費"     # 動詞固定,文件文字不能當事項
    assert doc_lead({"result": result}) == "這是帳單"


def test_official_letter_lists_required_actions_and_deadline_basis():
    a = _answer(LETTER)
    issuer, due = a["lines"]
    assert (issuer["label"], issuer["value"]) == ("發文機關", "示範區公所")
    assert (due["value"], due["big"]) == ("10月10日前辦理", True)
    assert a["todos"] == ["補繳身分證影本", "親自簽名"]
    assert "收到本函後15日內" in a["footer"] and "以公文原文為準" in a["footer"]


def test_medication_answer_uses_slots_and_waits_for_family():
    result = {"doc_type": "藥袋", "fields": {"items": [{"name": "示範藥甲", "timing": ["早", "晚"]}]}}
    med = _action("medication_schedule", "confirm",
                  {"slots": [{"slot": "早", "items": [{"name": "示範藥甲"}]}], "prn": [], "unscheduled": []})
    a = _answer(result, [med])
    assert a["medication"]["slots"] == [("早", ["示範藥甲"])] and a["lines"] == []
    assert a["status"]["text"] == "等待家人確認"
    assert a["med_state"] == {"text": "等待家人確認", "class": "wait"}


def test_invoice_answer_is_vendor_date_and_amount():
    a = _answer(INVOICE)
    assert a["status"] is None and a["todos"] == []               # 沒有要做的事
    assert [(r["label"], r["value"]) for r in a["lines"]] == [
        ("商家", "示範超市"), ("日期", "2026年9月18日"), ("金額", "320 元")]


def test_failed_verification_is_the_check_line_and_marks_the_value():
    result = dict(INVOICE, verification={"QR 總計額": {"status": "fail", "detail": "金額不同", "fields": ["amount"]}})
    a = _answer(result)
    assert a["check"] == {"text": "讀到的金額和發票上的 QR Code 不一樣，請對照原件確認。", "tone": "manual"}
    assert (a["lines"][2]["note"], a["lines"][2]["note_class"]) == ("和 QR Code 不符", "manual")
    rule_only = dict(INVOICE, verification={"日期合理性": {"status": "fail", "detail": "日期太舊", "fields": ["date"]}})
    a = _answer(rule_only)
    assert a["check"] == {"text": "日期沒通過檢查，請對照原件。", "tone": "manual"}     # 規則沒過不說成「不符」
    assert a["lines"][1]["note"] == "沒通過檢查"


def test_deadline_line_gets_flap_digits_only_for_a_real_date():
    """期限那一行另外給翻牌的月、日與「前繳費」(v5 看板;牌只給眼睛看,原本的字留給讀屏與朗讀)。
    讀到的不是 YYYY-MM-DD、沒讀到或讀不清楚就不畫牌,照實寫字。"""
    amount, due = _answer(BILL)["lines"]
    assert due["value"] == "10月15日前繳費" and due["flap"] == {"month": 10, "day": 15, "verb": "前繳費"}
    assert amount["flap"] is None                                          # 金額不是期限
    assert _answer(LETTER)["lines"][1]["flap"] == {"month": 10, "day": 10, "verb": "前辦理"}
    words = dict(BILL, fields={"due_date": "下個月底", "bill_kind": "電費"})
    assert _answer(words)["lines"][1]["value"] == "下個月底前繳費" and _answer(words)["lines"][1]["flap"] is None
    assert _answer(dict(BILL, unreadable=["fields.due_date"]))["lines"][1]["flap"] is None


def test_medication_info_is_label_and_value_for_two_columns():
    """院所、日期、電話分成欄名與內容兩個值,樣板排成兩欄的 <dl>,內容對齊(使用者 11/6)。只照藥袋上印的字。"""
    from web.render import medication_view
    m = medication_view({"hospital": "示範診所", "dispensed_date": "2026-09-30", "pharmacist_phone": "02-0000-0000",
                         "items": []})
    assert m["info"] == [("醫療院所", "示範診所"), ("調劑日期", "2026年9月30日"), ("藥師電話", "02-0000-0000")]
    assert medication_view({"items": []})["info"] == []


def test_review_reason_when_verifier_crashed():
    """核對程式出錯而轉人工時照實說,不說成「AI 沒把握」。"""
    from src.verify import VERIFY_ERROR_SUMMARY
    crashed = dict(INVOICE, verification={"_summary": VERIFY_ERROR_SUMMARY}, verified_confidence=0.0)
    assert review_reason(crashed) == "這次的核對沒有完成（核對程式出錯），請對照原件確認。"


def test_review_reason_is_plain_language():
    """轉人工原因用白話,不出現「驗證信心 0.10、門檻 0.80」;順序比照 src/decision.py。"""
    qr = dict(INVOICE, verification={"QR 總計額": {"status": "fail", "fields": ["amount"]},
                                     "QR 開立日期": {"status": "fail", "fields": ["date"]}})
    assert review_reason(qr) == "讀到的金額、日期和發票上的 QR Code 不一樣，請對照原件確認。"
    rule = dict(INVOICE, verification={"日期合理性": {"status": "fail", "fields": ["date"]}})
    assert review_reason(rule) == "有 1 項檢查沒通過，請對照原件確認。"
    missing = {"doc_type": "帳單", "amount": 1854.0, "fields": {}, "unreadable": []}
    assert review_reason(missing) == "有必要的欄位沒讀到（繳費期限），請對照原件補上。"
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
         "payload": {"title": "公文期限：請補繳文件", "date": "2099-10-10"}},
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


def test_reminders_are_grouped_overdue_soon_family_then_the_rest():
    """分組照六點的第 1 點:已逾期、快到期(7 天內)、需要家人協助,其他放最後;這裡的期限都很遠,沒有快到期。"""
    docs, actions = _docs_and_actions()
    rows = reminder_rows(docs, actions, today=date(2026, 9, 30))
    assert [r["group"] for r in rows] == ["overdue", "family", "family", "other", "other", "other"]
    assert [r["title"] for r in rows] == ["去年的帳單", "示範診所的服藥時間表", "發票", "公文期限：請補繳文件", "繳電費", "已確認的服藥表"]
    overdue, waiting, alert, letter, bill, done = rows
    assert overdue["note"] == "逾期 350 天" and overdue["note_class"] == "manual"
    assert waiting["href"] == "/confirm?item=5" and waiting["link"] == "去確認" and waiting["note"] == "等待家人確認"
    assert (bill["when"], bill["sub"], bill["amount"], bill["note"]) == ("2099/10/15 前", "示範電力公司", "1,854 元", "")   # 沒事的不寫狀態
    # v5 時刻表(11/6):標題只寫事項與金額,期限另外給月、日畫成翻牌;不是今年的另外給年份,讀屏照舊念 when
    assert bill["headline"] == "電費 1,854 元" and bill["link"] == "看文件"
    assert (bill["due_year"], bill["due_month"], bill["due_day"]) == (2099, 10, 15)
    assert letter["sub"] == "示範區公所" and letter["amount"] == ""           # 公文的金額欄不接到提醒上
    assert letter["headline"] == "公文：請補繳文件"
    assert (waiting["due_year"], waiting["due_month"], waiting["due_day"]) == ("", "", "")   # 沒有日期:空白的暗牌
    assert alert["note"] == "核對不符" and alert["note_class"] == "manual"    # 清單欄位窄,只放短狀態
    assert done["sub"] == "晚、需要時" and done["note"] == "家人已確認"


def test_home_lists_reminders_and_escapes_text(client, store, add_doc):
    doc_id = add_doc(BILL)
    store.add_action(doc_id, "calendar", "auto", {"title": "<script>x</script>", "date": "2099-10-15"})
    html = client.get("/").text
    assert "要記得的事" in html and "2099/10/15 前" in html
    assert "<script>x</script>" not in html and "&lt;script&gt;x&lt;/script&gt;" in html


def test_home_lists_are_real_lists(client, store, add_doc):
    """「要記得的事」「最近看過的文件」用 <ul>,讀屏會報「清單,N 項」(Vercel 稽核:語意 HTML 優先)。"""
    doc_id = add_doc(BILL)
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": "2099-10-15"})
    html = client.get("/").text
    assert html.count('<ul class="rlist">') == 1 and '<li class="ritem">' in html
    assert html.count('<ul class="tbl__list">') == 1 and '<li><a class="tbl__row"' in html


def test_home_without_reminders_invites_first_photo(client):
    assert "還沒有要記得的事" in client.get("/").text


def test_result_page_answers_first_then_status_then_details(client, store, add_doc):
    doc_id = add_doc(dict(BILL, verification={"金額合理性": {"status": "pass", "detail": "金額合理", "fields": ["amount"]}}))
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": "2099-10-15", "remind_days_before": 3})
    html = client.get(f"/doc/{doc_id}").text
    assert (html.index('id="answer-title"') < html.index('id="status-title"') < html.index('id="verify-title"')
            < html.index('id="fields-title"'))
    assert 'class="first"' in html and "1,854 元" in html
    assert html.count("data-speak-part") >= 3                       # 標題、金額、期限、核對那一句


def test_trust_notes_follow_the_actual_mode():
    from web.render import trust_notes
    local = dict(trust_notes("ollama", ("藥袋",)))
    assert "不會送到雲端" in local["lock"]
    cloud = dict(trust_notes("workers_ai", ("藥袋", "收據")))
    assert cloud["lock"].startswith("藥袋、發票／收據，以及選「不確定」的文件只在這台電腦上辨識；帳單、公文會交給雲端模型讀取。")             # 只看上傳時選的類型
    assert dict(trust_notes("workers_ai", ("藥袋", "公文")))["lock"].startswith(   # 照實際設定寫,依選項順序
        "公文、藥袋，以及選「不確定」的文件只在這台電腦上辨識；帳單、發票／收據會交給雲端模型讀取。")
    assert "不會送到雲端" in dict(trust_notes("workers_ai", ("藥袋", "收據", "帳單", "公文")))["lock"]
    assert "展示模式" in dict(trust_notes("mock", ("藥袋",)))["lock"]
    assert all("刪除" not in text for _, text in trust_notes("ollama", ("藥袋",)))   # 沒有刪除功能就不寫
