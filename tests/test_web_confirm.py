"""家人確認頁測試:只列 confirm + pending,確認/退回會改變行動狀態。"""
import pytest


def _result(doc_type="藥袋"):
    return {"doc_type": doc_type, "fields": {}}


@pytest.fixture
def actions(store, add_doc):
    med_doc, bill_doc = add_doc(_result("藥袋")), add_doc(_result("帳單"))
    med = store.add_action(med_doc, "medication_schedule", "confirm", {"items": [
        {"name": "示範藥甲", "timing": ["早", "晚"], "prn": False, "dose_text": "每次1顆"},
        {"name": "示範藥乙", "timing": [], "prn": True},
    ]})
    deadline = store.add_action(bill_doc, "calendar", "confirm",
                                {"title": "繳水費", "date": "2099-10-20", "description": "", "remind_days_before": 1})
    auto = store.add_action(bill_doc, "calendar", "auto",
                            {"title": "自動那一筆", "date": "2099-10-21", "description": "", "remind_days_before": 1})
    done = store.add_action(bill_doc, "calendar", "confirm",
                            {"title": "已經確認過的", "date": "2099-10-22"}, status="done")
    return {"med": med, "deadline": deadline, "auto": auto, "done": done}


def test_confirm_lists_only_pending_confirm_actions(client, actions):
    html = client.get("/confirm").text
    assert "示範藥甲" in html and "早" in html and "晚" in html
    assert "需要時" in html and "示範藥乙" in html
    assert "每次1顆" not in html                        # 確認頁只列關鍵內容
    assert "繳水費" in html and "2099年10月20日" in html
    assert "自動那一筆" not in html
    assert "已經確認過的" not in html
    assert html.count('value="done"') == 2


def test_reject_asks_before_submitting(client, actions):
    """退回在系統內沒有復原:按鈕帶確認框的標題、說明與確定鍵,由 app.js 先問一次;「確認」不多問。"""
    html = client.get("/confirm").text
    assert html.count('data-confirm="退回後就不會生效。"') == html.count('value="rejected"') == 2
    assert html.count('data-confirm-ok="確定退回"') == 2
    assert 'data-confirm-title="確定要退回「服藥時間表」嗎?"' in html
    assert 'data-confirm-title="確定要退回「期限提醒」嗎?"' in html
    assert 'value="done" data-confirm' not in html


def test_nav_shows_pending_count(client, actions):
    html = client.get("/").text
    assert '2<span class="visually-hidden"> 件待確認' in html
    # 手機「選單」收起時也看得到待處理總數(家人確認 + 待複核),家人才不會漏看
    menu = html.split('<details class="menu">')[1].split("</summary>")[0]
    assert "選單" in menu and '2<span class="visually-hidden"> 件待處理' in menu


def test_confirm_marks_done(client, store, actions, flash_text, action_status):
    r = client.post(f"/confirm/{actions['med']}", data={"decision": "done"}, follow_redirects=False)
    assert r.status_code == 303
    assert action_status(store, actions["med"]) == "done"
    page = client.get(r.headers["location"]).text
    assert flash_text(page) == "已確認" and "示範藥甲" not in page


def test_reject_marks_rejected(client, store, actions, flash_text, action_status):
    r = client.post(f"/confirm/{actions['deadline']}", data={"decision": "rejected"}, follow_redirects=False)
    assert r.status_code == 303
    assert action_status(store, actions["deadline"]) == "rejected"
    assert flash_text(client.get(r.headers["location"]).text) == "已退回"


def test_invalid_decision_is_400(client, store, actions, action_status):
    assert client.post(f"/confirm/{actions['med']}", data={"decision": "pending"}).status_code == 400
    assert action_status(store, actions["med"]) == "pending"


def test_missing_decision_is_chinese_page(client, store, actions, action_status):
    r = client.post(f"/confirm/{actions['med']}", data={})
    assert r.status_code == 422 and "送出的資料不完整" in r.text and "Field required" not in r.text
    assert action_status(store, actions["med"]) == "pending"


def test_unknown_action_is_404(client, actions):
    assert client.post("/confirm/999", data={"decision": "done"}).status_code == 404


def test_auto_or_decided_actions_cannot_be_confirmed(client, store, actions, action_status):
    r = client.post(f"/confirm/{actions['auto']}", data={"decision": "rejected"})
    assert r.status_code == 409
    assert "可能已經有人處理了" in r.text and 'href="/confirm"' in r.text   # 錯誤頁帶下一步
    assert action_status(store, actions["auto"]) == "pending"
    assert client.post(f"/confirm/{actions['done']}", data={"decision": "rejected"}).status_code == 409
    assert action_status(store, actions["done"]) == "done"


def test_decided_action_shows_on_result_page(client, store, actions):
    client.post(f"/confirm/{actions['med']}", data={"decision": "done"})
    doc_id = next(a["document_id"] for a in store.list_actions() if a["id"] == actions["med"])
    assert "家人已確認" in client.get(f"/doc/{doc_id}").text


def test_confirm_links_review_queue(client, review_doc):
    review_doc()
    html = client.get("/confirm").text
    assert 'href="/review"' in html and "1 份文件等待複核" in html


def test_empty_confirm_page(client):
    assert "目前沒有要確認的事項" in client.get("/confirm").text


def test_unknown_payload_keys_degrade_gracefully(client, store, add_doc):
    doc_id = add_doc(_result())
    store.add_action(doc_id, "medication_schedule", "confirm", {
        "schedule": {"早上": ["示範藥丙"], "睡前": [{"name": "示範藥丁"}]},
        "prn": ["示範藥戊"],
        "新欄位": "<script>x</script>",
    })
    html = client.get("/confirm").text
    assert "示範藥丙" in html and "示範藥丁" in html and "示範藥戊" in html
    assert "新欄位" in html and "&lt;script&gt;x&lt;/script&gt;" in html


def _item(name, timing=(), prn=False, freq="一天兩次"):
    return {"name": name, "dose_text": "每次1顆", "frequency_text": freq,
            "timing": list(timing), "prn": prn, "days": 7}


# W1-C(src/actions)實際產生的服藥時間表 payload 形狀
W1C_MEDICATION = {
    "title": "示範診所的服藥時間表",
    "hospital": "示範診所",
    "dispensed_date": "2026-09-30",
    "slots": [
        {"slot": "早", "items": [_item("示範藥甲", ["早", "晚"])]},
        {"slot": "晚", "items": [_item("示範藥甲", ["早", "晚"])]},
        {"slot": "睡前", "items": [_item("示範藥丙", ["睡前"])]},
    ],
    "prn": [_item("示範藥乙", prn=True, freq="需要時")],
    "unscheduled": [_item("示範藥丁", freq="一天三次")],
    "items": [_item("示範藥甲", ["早", "晚"]), _item("示範藥乙", prn=True),
              _item("示範藥丙", ["睡前"]), _item("示範藥丁")],
    "pharmacist_phone": "02-0000-0000",
    "disclaimer": "本系統只協助閱讀,不提供醫療建議;用藥請依醫師與藥師指示。",
}


def test_w1c_medication_payload_on_confirm_card(client, store, add_doc):
    store.add_action(add_doc(_result()), "medication_schedule", "confirm", W1C_MEDICATION)
    html = client.get("/confirm").text
    assert "示範診所" in html and "2026年9月30日" in html
    for name in ("示範藥甲", "示範藥乙", "示範藥丙", "示範藥丁"):
        assert name in html
    assert html.count("示範藥甲") == 2               # 早、晚各一次;items 不重複列
    assert "未標時段" in html and "時段請依藥袋或詢問藥師" in html
    assert "需要時" in html
    assert "本系統只協助閱讀,不提供醫療建議" in html   # 卡片上也有聲明
    assert "每次1顆" not in html


def test_w1c_calendar_confirm_card_shows_title_and_date(client, store, add_doc):
    store.add_action(add_doc(_result("公文")), "calendar", "confirm", {
        "title": "公文期限:補繳文件", "date": "2099-10-31",
        "description": "收到本函後15日內\n本系統只提醒,不會替您付款或回覆。",
        "remind_days_before": 3,
    })
    html = client.get("/confirm").text
    assert "公文期限:補繳文件" in html and "2099年10月31日" in html
