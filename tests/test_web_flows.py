"""完整流程(FIN-HARDEN 第二輪):帳單、矛盾發票、藥袋從上傳走到處理完,每一步都看畫面與資料庫。
讀錯了也補救得回來,才算這一輪做到。文件是合成的,模型換成照劇本讀值的 analyzer。"""
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from src.parsing import to_result
from src.providers import MockAnalyzer
from test_review import _invoice, _invoice_png
from web.access import Identity
from web.app import CONFIRM_REJECT_FIELD, CONFIRMED, CSRF_COOKIE, CSRF_FIELD, create_app

TODAY = date.today()
DUE = (TODAY + timedelta(days=10)).isoformat()
LATER = (TODAY + timedelta(days=20)).isoformat()
BILL = {"doc_type": "帳單", "date": TODAY.isoformat(), "vendor": "合成電力公司", "amount": 1286, "confidence": 0.95,
        "plain_summary": "這是電費帳單。", "fields": {"due_date": DUE, "bill_kind": "電費"}}
MEDS = {"doc_type": "藥袋", "date": TODAY.isoformat(), "vendor": "合成診所", "confidence": 0.95, "plain_summary": "這是藥袋。",
        "fields": {"items": [{"name": "示範藥甲", "dose_text": "一天三次", "frequency_text": "飯後", "timing": []}]}}


class Reads(MockAnalyzer):
    """一律回傳 raw(照抄劇本);記下每次的類型提示。"""

    def __init__(self, cfg, raw):
        super().__init__(cfg)
        self.raw, self.hints = raw, []

    def analyze(self, file_path, doc_type_hint=None, *, local_only=False):
        self.hints.append(doc_type_hint)
        return to_result(dict(self.raw))


def _people(cfg, store, analyzer):
    """同一個 app,長輩與家人各開一個瀏覽器。"""
    class ByHeader:
        def resolve(self, request):
            return Identity(f"{request.headers['x-who']}@example.test", False)

    app = create_app(cfg, analyzer=analyzer, identity_resolver=ByHeader())
    people = {}
    for who in ("elder", "family"):
        store.set_member(f"{who}@example.test", who, {"elder": "媽媽", "family": "女兒"}[who])
        store.set_setting(f"consent:{who}@example.test", "2026-10-28T08:00:00", actor=f"{who}@example.test")
        c = TestClient(app, headers={"x-who": who})
        c.get("/")
        people[who] = c
    return people["elder"], people["family"]


def _post(c, url, **form):
    return c.post(url, data={CSRF_FIELD: c.cookies[CSRF_COOKIE], **form}, follow_redirects=False)


def _upload(c, content, doc_type):
    r = c.post("/upload", files={"file": ("photo.png", content, "image/png")},
               data={CSRF_FIELD: c.cookies[CSRF_COOKIE], "doc_type": doc_type}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


def _remind(html):
    end = html.index('id="done-title"') if 'id="done-title"' in html else html.index('id="recent-title"')
    return re.sub(r'<details class="note">.*?</details>', "", html[html.index('id="remind-title"'):end], flags=re.S)


def _live(store, doc_id):
    return [a for a in store.list_actions(document_id=doc_id) if not a["superseded"]]


# ---- 帳單:讀到 → 列入提醒 → 家人更正期限 → 長輩已繳費 → 改回 → 取消 → 恢復 ------------

def test_bill_from_upload_to_paid_and_back(cfg, store, png_bytes):
    elder, family = _people(cfg, store, Reads(cfg, BILL))
    doc_id = _upload(elder, png_bytes(), "帳單")
    assert store.get_document(doc_id)["action"] == "archive"
    [first] = _live(store, doc_id)
    assert (first["kind"], first["tier"], first["status"]) == ("calendar", "auto", "pending")
    section = _remind(elder.get("/").text)
    assert "繳電費" in section and "合成電力公司" in section and "1,286 元" in section and "已繳費" in section

    # 家人發現期限讀錯,更正:舊提醒被取代,首頁換成新的日期
    r = _post(family, f"/doc/{doc_id}/correct", vendor="合成電力公司", date=TODAY.isoformat(), amount="1286",
              **{"fields.due_date": LATER, "fields.bill_kind": "電費"})
    assert r.status_code == 303
    [second] = _live(store, doc_id)
    assert second["id"] != first["id"] and second["payload"]["date"] == LATER and second["tier"] == "auto"
    assert _post(elder, f"/reminder/{first['id']}/done", decision="complete").status_code == 409   # 舊的不能再按

    # 長輩按已繳費:移到已完成,記下是誰
    assert _post(elder, f"/reminder/{second['id']}/done", decision="complete").status_code == 303
    home = elder.get("/").text
    assert "繳電費" not in _remind(home) and "媽媽" in home[home.index('id="done-title"'):]

    # 按錯了,從結果頁改回;再取消提醒,按錯了再恢復
    assert "改回未繳" in elder.get(f"/doc/{doc_id}").text
    assert _post(elder, f"/reminder/{second['id']}/done", decision="undo", back="doc").status_code == 303
    assert "繳電費" in _remind(elder.get("/").text)
    assert _post(elder, f"/reminder/{second['id']}", decision="cancel").status_code == 303
    assert "繳電費" not in _remind(elder.get("/").text)
    assert _post(elder, f"/reminder/{second['id']}", decision="restore").status_code == 303
    assert "繳電費" in _remind(elder.get("/").text)
    events = [e["event"] for e in store.list_audit_events(document_id=doc_id)]
    assert events.count("complete_reminder") == 1 and events.count("undo_complete") == 1 and "correct" in events


# ---- 矛盾發票:和 QR 不符 → 待複核 → 家人改成 QR 上的值 → 存檔 --------------------------

def test_invoice_contradicting_its_qr_goes_to_family_and_can_be_fixed(cfg, store):
    """印刷金額讀成 530,QR 記載 350:不能自動接受;改的值還是不符就留在待複核,改對了才存檔。"""
    elder, family = _people(cfg, store, Reads(cfg, _invoice()))
    doc_id = _upload(elder, _invoice_png(), "發票")
    doc = store.get_document(doc_id)
    assert doc["action"] == "review" and doc["result"]["verification"]["QR 總計額"]["status"] == "fail"
    assert "核對不符" in _remind(elder.get("/").text) and f'href="/doc/{doc_id}"' in _remind(elder.get("/").text)
    assert "有 1 項不符" in elder.get(f"/doc/{doc_id}").text
    assert elder.get(f"/doc/{doc_id}/correct").status_code == 403                 # 長輩不能更正讀值

    form = family.get(f"/doc/{doc_id}/correct").text
    assert "350" in form and "data-fill-value" in form                            # 寫出 QR 上的值,按了才填入
    base = dict(vendor="合成測試商店", date="2026-07-04", currency="NTD", invoice_number="ZX10293847")
    assert _post(family, f"/doc/{doc_id}/correct", amount="380", **base).status_code == 303
    assert store.get_document(doc_id)["action"] == "review"                       # 還是不符,留在待複核
    assert "核對不符" in _remind(family.get("/").text)

    assert _post(family, f"/doc/{doc_id}/correct", amount="350", **base).status_code == 303
    doc = store.get_document(doc_id)
    assert doc["action"] == "archive" and doc["result"]["verification"]["QR 總計額"]["status"] == "pass"
    assert "核對不符" not in _remind(family.get("/").text)
    assert _live(store, doc_id) == []                                             # 發票不產生提醒
    assert store.list_corrections(document_id=doc_id)[0]["verified"] is True


# ---- 藥袋:一律等家人確認 → 確認 → 更正後要再確認 → 長輩退回 ---------------------------

def test_medication_bag_always_waits_and_elder_can_still_reject(cfg, store, png_bytes):
    analyzer = Reads(cfg, MEDS)
    elder, family = _people(cfg, store, analyzer)
    doc_id = _upload(elder, png_bytes(), "藥袋")
    assert analyzer.hints == ["藥袋"]
    [schedule] = _live(store, doc_id)
    assert (schedule["kind"], schedule["tier"], schedule["status"]) == ("medication_schedule", "confirm", "pending")
    assert schedule["payload"]["slots"] == []                                     # 「一天三次」不換算成時段(Non-Goal 1)
    section = _remind(elder.get("/").text)
    # 長輩不能確認:這一列連到那份文件,不帶到按不了的家人確認頁
    assert "等待家人確認" in section and f'href="/doc/{doc_id}"' in section and 'href="/confirm"' not in section
    assert "已繳費" not in section
    assert f'href="/confirm?item={schedule["id"]}"' in _remind(family.get("/").text)

    # 長輩不能確認,家人確認後才生效
    assert _post(elder, f"/confirm/{schedule['id']}", decision="done").status_code == 403
    assert _post(family, f"/confirm/{schedule['id']}", decision="done").status_code == 303
    assert "家人已確認" in _remind(elder.get("/").text)

    # 家人更正藥品:新的時間表要再確認一次,舊的不再生效
    r = _post(family, f"/doc/{doc_id}/correct", vendor="合成診所", date=TODAY.isoformat(),
              **{"items-0-name": "示範藥甲", "items-0-usage": "一天三次，飯後", "items-0-days": "7", "items-0-timing": "早"})
    assert r.status_code == 303
    [again] = _live(store, doc_id)
    assert again["id"] != schedule["id"] and (again["tier"], again["status"]) == ("confirm", "pending")
    assert [s["slot"] for s in again["payload"]["slots"]] == ["早"]               # 只照家人勾的時段

    # 長輩任何行動都能退回(要先問一次)
    assert _post(elder, f"/confirm/{again['id']}", decision="rejected").status_code == 400
    assert _live(store, doc_id)[0]["status"] == "pending"
    r = _post(elder, f"/confirm/{again['id']}", decision="rejected", **{CONFIRM_REJECT_FIELD: CONFIRMED})
    assert r.status_code == 303 and _live(store, doc_id)[0]["status"] == "rejected"
    assert "服藥時間表" not in _remind(elder.get("/").text)
    [event] = [e for e in store.list_audit_events(document_id=doc_id) if e["event"] == "reject"]
    assert (event["actor"], event["role"]) == ("elder@example.test", "elder")


# ---- 公文:家人填收到日期,期限跟著移 ------------------------------------------------

@pytest.fixture
def letter(add_doc, store):
    """發文日 40 天前、「收到本函後15日內」:從發文日算已經過期,家人填收到日才知道還有幾天。"""
    issued = (TODAY - timedelta(days=40)).isoformat()
    due = (TODAY - timedelta(days=25)).isoformat()
    basis = f"以發文日期 {issued} 起算；實際收到日可能較晚，期限以公文原文為準。"
    doc_id = add_doc({"doc_type": "公文", "date": issued, "vendor": "合成區公所", "confidence": 0.95,
                      "fields": {"subject": "請補繳文件", "deadline_text": "收到本函後15日內", "deadline": due,
                                 "deadline_basis": basis}})
    store.add_action(doc_id, "calendar", "auto", {"title": "公文期限：請補繳文件", "date": due, "basis": basis})
    return doc_id, issued


def _letter_form(issued, received):
    return {"vendor": "合成區公所", "date": issued, "fields.received_date": received, "fields.subject": "請補繳文件",
            "fields.deadline_text": "收到本函後15日內"}


def test_family_fills_in_the_received_date_and_the_reminder_moves(client, store, letter):
    doc_id, issued = letter
    assert "逾期 25 天" in _remind(client.get("/").text)
    page = client.get(f"/doc/{doc_id}").text
    assert f"以發文日期 {issued} 起算" in page and "#f-fields-received_date" in page and "填收到日期" in page
    assert 'id="f-fields-received_date"' in client.get(f"/doc/{doc_id}/correct").text

    received = (TODAY - timedelta(days=5)).isoformat()
    r = client.post(f"/doc/{doc_id}/correct", data=_letter_form(issued, received), follow_redirects=False)
    assert r.status_code == 303
    [action] = _live(store, doc_id)
    assert action["payload"]["date"] == (TODAY + timedelta(days=10)).isoformat() and action["tier"] == "auto"
    assert "逾期" not in _remind(client.get("/").text)                             # 舊提醒被取代,不再逾期
    page = client.get(f"/doc/{doc_id}").text
    assert f"以家人填的收到日期 {received} 起算" in page and "收到日期" in page


@pytest.mark.parametrize("received, message", [
    ((TODAY + timedelta(days=1)).isoformat(), "收到日期不能晚於今天"),
    ((TODAY - timedelta(days=41)).isoformat(), "收到日期不能早於發文日期"),
    ("不是日期", "請填正確的日期"),
])
def test_impossible_received_dates_are_refused(client, store, letter, received, message):
    doc_id, issued = letter
    r = client.post(f"/doc/{doc_id}/correct", data=_letter_form(issued, received))
    assert r.status_code == 400 and message in r.text
    assert "received_date" not in store.get_document(doc_id)["result"]["fields"]


def test_received_date_stays_when_other_fields_are_corrected(client, store, letter):
    doc_id, issued = letter
    received = (TODAY - timedelta(days=5)).isoformat()
    client.post(f"/doc/{doc_id}/correct", data=_letter_form(issued, received))
    form = client.get(f"/doc/{doc_id}/correct").text
    assert f'value="{received}"' in form
    client.post(f"/doc/{doc_id}/correct", data=dict(_letter_form(issued, received), vendor="合成市公所"))
    assert store.get_document(doc_id)["result"]["fields"]["received_date"] == received


def test_only_bills_are_refused_the_received_date(cfg, store, tmp_path):
    from src.models import ExtractionResult
    from src.review import apply_changes
    with pytest.raises(ValueError):
        apply_changes(ExtractionResult(doc_type="帳單"), {"fields.received_date": "2026-10-01"})
    assert apply_changes(ExtractionResult(doc_type="公文"), {"fields.received_date": "2026-10-01"}).fields["received_date"]


def test_fixed_deadline_has_no_received_date_link(client, add_doc):
    doc_id = add_doc({"doc_type": "公文", "date": "2026-09-20", "vendor": "合成區公所",
                      "fields": {"subject": "請補繳文件", "deadline_text": "於115年10月31日前", "deadline": "2026-10-31"}})
    page = client.get(f"/doc/{doc_id}").text
    assert "於115年10月31日前" in page and "填收到日期" not in page


def test_elder_sees_the_basis_but_no_link_to_change_it(cfg, store, letter):
    doc_id, issued = letter

    class Elder:
        def resolve(self, request):
            return Identity("elder@example.test", False)

    store.set_member("elder@example.test", "elder", "媽媽")
    store.set_setting("consent:elder@example.test", "2026-10-28T08:00:00", actor="elder@example.test")
    page = TestClient(create_app(cfg, analyzer=MockAnalyzer(cfg), identity_resolver=Elder())).get(f"/doc/{doc_id}").text
    assert f"以發文日期 {issued} 起算" in page and "填收到日期" not in page


def test_confirm_card_shows_how_the_deadline_was_counted(client, store, add_doc):
    doc_id = add_doc({"doc_type": "公文", "vendor": "合成區公所", "fields": {"subject": "請補繳文件"}}, action="review")
    basis = "文件上沒有可讀的發文日期，以上傳日期 2026-10-01 起算；期限以公文原文為準。"
    store.add_action(doc_id, "calendar", "confirm", {"title": "公文期限：請補繳文件", "date": DUE, "basis": basis})
    html = client.get("/confirm").text
    assert basis in html and f'href="/doc/{doc_id}/correct#f-fields-received_date"' in html
