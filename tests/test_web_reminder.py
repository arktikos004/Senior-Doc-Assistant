"""取消/恢復提醒的測試:自動列入的期限提醒能在結果頁取消,按錯可以恢復。"""
import pytest

from samples import BILL, LETTER, REMINDER
from web.render import action_view, answer_view, verification_view


def _answer(result, *raw_actions):
    return answer_view(result, [action_view(a) for a in raw_actions], verification_view(result))


def _raw(tier="auto", status="pending", kind="calendar", action_id=7):
    return {"id": action_id, "document_id": 1, "kind": kind, "tier": tier, "status": status, "payload": REMINDER}


# ---- 畫面資料(render) -------------------------------------------------------

def test_auto_reminder_offers_cancel():
    status = _answer(BILL, _raw())["status"]
    assert status["text"] == "已列入提醒" and status["sub"] == "到期前會列在首頁「要記得的事」"
    assert status["toggle"] == {"id": 7, "decision": "cancel", "label": "取消提醒", "icon": "x"}


def test_cancelled_reminder_offers_restore():
    status = _answer(BILL, _raw(status="rejected"))["status"]
    assert (status["text"], status["class"]) == ("已取消提醒", "off")
    assert status["sub"] == "首頁「要記得的事」不會再列出"
    assert status["toggle"]["decision"] == "restore" and status["toggle"]["label"] == "恢復提醒"


@pytest.mark.parametrize("raw", [
    _raw(tier="confirm"),                                   # 等家人確認:到「家人確認」處理
    _raw(tier="confirm", status="done"),                    # 家人已確認:不提供取消或恢復
])
def test_reminders_needing_family_have_no_toggle(raw):
    assert _answer(BILL, raw)["status"]["toggle"] is None


def test_bill_deadline_says_it_was_read_by_ai():
    """期限是 AI 讀的,提醒旁邊要請長輩對照帳單;公文維持自己的推算說明。"""
    assert _answer(BILL)["footer"] == "期限由 AI 讀取,請對照帳單。"
    assert _answer(dict(BILL, fields={"bill_kind": "電費"}, amount=None))["footer"] == ""   # 沒讀到期限就不說
    assert "以公文原文為準" in _answer(LETTER)["footer"]


# ---- 網頁流程 -----------------------------------------------------------------

@pytest.fixture
def doc_id(add_doc):
    """一份已存檔的電費帳單;client 是共用的 FormClient,POST 自動帶 CSRF token(W2-B)。"""
    return add_doc(BILL)


def test_cancel_then_restore_round_trip(client, store, doc_id, action_status, flash_text):
    aid = store.add_action(doc_id, "calendar", "auto", REMINDER)
    page = client.get(f"/doc/{doc_id}").text
    assert f'action="/reminder/{aid}"' in page and 'value="cancel"' in page and "取消提醒" in page
    assert "data-confirm" not in page.split('action="/reminder/')[1].split("</form>")[0]   # 不跳確認框

    r = client.post(f"/reminder/{aid}", data={"decision": "cancel"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"/doc/{doc_id}?msg=")
    assert action_status(store, aid) == "rejected"
    page = client.get(r.headers["location"]).text
    assert flash_text(page) == "已取消提醒" and 'value="restore"' in page
    assert "繳電費" not in client.get("/").text.split('id="remind-title"')[1].split("</section>")[0]

    r = client.post(f"/reminder/{aid}", data={"decision": "restore"}, follow_redirects=False)
    assert r.status_code == 303 and action_status(store, aid) == "pending"
    assert flash_text(client.get(r.headers["location"]).text) == "已恢復提醒"
    assert "繳電費" in client.get("/").text.split('id="remind-title"')[1].split("</section>")[0]


def test_pressing_twice_keeps_the_same_state(client, store, doc_id, action_status):
    """連按兩次(或重新整理後再送)不會出錯,也不會反過來恢復。"""
    aid = store.add_action(doc_id, "calendar", "auto", REMINDER)
    for _ in range(2):
        assert client.post(f"/reminder/{aid}", data={"decision": "cancel"}, follow_redirects=False).status_code == 303
    assert action_status(store, aid) == "rejected"


def test_only_auto_reminders_can_be_toggled(client, store, doc_id, action_status):
    confirm = store.add_action(doc_id, "calendar", "confirm", REMINDER)
    med = store.add_action(doc_id, "medication_schedule", "confirm", {"items": []})
    for aid in (confirm, med):
        r = client.post(f"/reminder/{aid}", data={"decision": "cancel"})
        assert r.status_code == 409 and 'href="/confirm"' in r.text      # 要家人確認的事回「家人確認」處理
        assert "不能在這裡取消" in r.text and "可能已經有人處理了" not in r.text
        assert action_status(store, aid) == "pending"


def test_bad_requests_are_rejected(client, store, doc_id, action_status):
    aid = store.add_action(doc_id, "calendar", "auto", REMINDER)
    assert client.post(f"/reminder/{aid}", data={"decision": "done"}).status_code == 400
    assert client.post("/reminder/999", data={"decision": "cancel"}).status_code == 404
    assert action_status(store, aid) == "pending"


def test_result_page_only_echoes_known_messages(client, doc_id, flash_text):
    html = client.get(f"/doc/{doc_id}?msg=請立刻匯款").text
    assert "請立刻匯款" not in html and flash_text(html) is None
