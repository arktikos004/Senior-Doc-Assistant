"""家人確認頁測試:只處理 confirm + pending,一次一件(使用者 11/5),確認/退回會改變行動狀態。"""
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.providers import MockAnalyzer
from src.store import Store
from test_web_correct import browser_form
from web.access import Identity
from web.app import CSRF_COOKIE, CSRF_FIELD, create_app
from web.render import STATIC_DIR, action_view

CSS = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
T3 = CSS.split("/* ---- T3 家人確認、待複核、更正 ---- */")[1].split("/* (T3 家人確認、待複核、更正的規則寫在這一行上面) */")[0]


def _result(doc_type="藥袋"):
    return {"doc_type": doc_type, "fields": {}}


def _as(cfg, store, role="elder"):
    """用某個家庭成員的身分開網頁;role=None 是不在名單裡、只能查看的人。"""
    email = f"{role or 'viewer'}@example.test"
    if role:
        store.set_member(email, role, "某人")
    store.set_setting(f"consent:{email}", "2026-10-28T08:00:00", actor=email)

    class Fixed:
        def resolve(self, request):
            return Identity(email, False)

    c = TestClient(create_app(cfg, analyzer=MockAnalyzer(cfg), identity_resolver=Fixed()))
    c.get("/")
    return c


def _main(html):
    """<main> 裡的內容:導覽列的連結不是這一頁帶去的。"""
    return html.split('<main id="main"', 1)[1].split("</main>", 1)[0]


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


def test_confirm_shows_one_pending_item_at_a_time(client, actions):
    """一次只看一件,順序照行動編號,整頁只有一顆「確認」;已確認的與自動的都不在裡面。"""
    html = client.get("/confirm").text
    assert "第 1 件，共 2 件" in html
    assert "示範藥甲" in html and "早" in html and "晚" in html
    assert "需要時" in html and "示範藥乙" in html
    assert "每次1顆" not in html                        # 確認頁只列關鍵內容
    assert "繳水費" not in html                         # 第二件還沒輪到
    assert "自動那一筆" not in html and "已經確認過的" not in html
    assert html.count('value="done"') == 1 and html.count("btn--primary") == 1
    assert f'action="/confirm/{actions["med"]}"' in html


def test_item_opens_that_one(client, actions):
    html = client.get(f"/confirm?item={actions['deadline']}").text
    assert "第 2 件，共 2 件" in html and "繳水費" in html and "2099年10月20日" in html and "示範藥甲" not in html
    assert f'action="/confirm/{actions["deadline"]}"' in html and "這件事可能已經有人處理了" not in html


@pytest.mark.parametrize("item", ["999", "abc", "", "99999999999999999999999"])
def test_item_that_is_not_waiting_says_so_and_shows_the_first(client, actions, item):
    html = client.get("/confirm", params={"item": item}).text
    assert "這件事可能已經有人處理了" in html and "第 1 件，共 2 件" in html and "示範藥甲" in html


@pytest.mark.parametrize("name", ["auto", "done"])
def test_item_that_was_already_handled_is_not_opened(client, actions, name):
    html = client.get(f"/confirm?item={actions[name]}").text
    assert "這件事可能已經有人處理了" in html and "自動那一筆" not in html and "已經確認過的" not in html
    assert "第 1 件，共 2 件" in html


def test_next_link_goes_through_every_item_and_back(client, actions):
    first = client.get("/confirm").text
    assert f'<a class="confirm__next" href="/confirm?item={actions["deadline"]}">看下一件</a>' in first
    last = client.get(f"/confirm?item={actions['deadline']}").text
    assert f'<a class="confirm__next" href="/confirm?item={actions["med"]}">回到第 1 件</a>' in last


def test_single_item_has_no_next_link(client, store, add_doc):
    store.add_action(add_doc(_result()), "medication_schedule", "confirm", {"items": [{"name": "示範藥甲"}]})
    html = client.get("/confirm").text
    assert "第 1 件，共 1 件" in html and "confirm__next" not in html


def test_deciding_moves_on_to_the_next_then_says_all_done(client, store, actions, flash_text):
    r = client.post(f"/confirm/{actions['med']}", data={"decision": "done"}, follow_redirects=False)
    assert r.headers["location"].startswith("/confirm?msg=")
    page = client.get(r.headers["location"]).text
    assert flash_text(page) == "已確認" and "第 1 件，共 1 件" in page and "繳水費" in page
    r = client.post(f"/confirm/{actions['deadline']}", data={"decision": "rejected", "confirm_reject": "1"},
                    follow_redirects=False)
    page = client.get(r.headers["location"]).text
    assert flash_text(page) == "已退回" and "都確認完了" in page and "第 1 件" not in page
    assert '<a class="btn btn--primary btn--lg" href="/">' in page and "回首頁</a>" in page
    assert '<form class="confirm__actions"' not in page


def test_reject_asks_before_submitting(client, actions):
    """退回在系統內沒有復原:按鈕帶確認框的標題、說明與確定鍵,由 app.js 先問一次;「確認」不多問。"""
    html = client.get("/confirm").text
    assert html.count('data-confirm="退回後就不會生效。"') == html.count('value="rejected"') == 1
    assert 'data-confirm-ok="確定退回"' in html
    assert 'data-confirm-field="confirm_reject"' in html              # 確認後 app.js 補上伺服器要看的欄位
    assert 'data-confirm-title="確定要退回「服藥時間表」嗎？"' in html
    assert 'value="done" data-confirm' not in html
    reject = re.search(r'<button class="btn btn--secondary btn--lg"[^>]*value="rejected"', html, re.S)   # 退回是次要按鈕
    assert reject
    other = client.get(f"/confirm?item={actions['deadline']}").text
    assert 'data-confirm-title="確定要退回「期限提醒」嗎？"' in other


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
    r = client.post(f"/confirm/{actions['deadline']}", data={"decision": "rejected", "confirm_reject": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert action_status(store, actions["deadline"]) == "rejected"
    assert flash_text(client.get(r.headers["location"]).text) == "已退回"


def test_reject_without_confirmation_asks_on_a_page(client, store, actions, action_status):
    """沒有 JS(或 app.js 沒跑起來)就沒有確認框:伺服器沒收到確認欄位不會退回(SEC-02),回一頁確認頁再問一次,
    字和確認框一樣;按「確定退回」才生效,「先不要」回到同一件。「確認」不必多問。"""
    r = client.post(f"/confirm/{actions['med']}", data={"decision": "rejected"})
    assert r.status_code == 400 and action_status(store, actions["med"]) == "pending"
    assert "<h1>確定要退回「服藥時間表」嗎？</h1>" in r.text and "退回後就不會生效。" in r.text
    assert f'<a class="btn btn--secondary btn--lg" href="/confirm?item={actions["med"]}">先不要</a>' in r.text
    assert "確定退回</button>" in r.text and r.text.index(">先不要</a>") < r.text.index("確定退回</button>")
    assert '<a class="side__link" href="/confirm" aria-current="page">' in r.text
    form = browser_form(r.text, f"/confirm/{actions['med']}")
    assert {k: v for k, v in form.items() if k != CSRF_FIELD} == {"decision": "rejected", "confirm_reject": "1"}
    r = client.post(f"/confirm/{actions['med']}", data=form, follow_redirects=False)
    assert r.status_code == 303 and action_status(store, actions["med"]) == "rejected"
    r = client.post(f"/confirm/{actions['deadline']}", data={"decision": "done"}, follow_redirects=False)
    assert r.status_code == 303 and action_status(store, actions["deadline"]) == "done"


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


def test_two_family_members_deciding_at_once_only_one_counts(app, form_client, store, actions, action_status,
                                                          monkeypatch):
    """兩位家人同時按同一個事項(一個確認、一個退回):只有先到的算數,另一個看到「已經處理過了」(SEC-01)。

    在寫入狀態的地方放一道關卡:沒有鎖時兩個請求都已經讀到「還在等確認」,兩個都回成功,後寫的蓋掉先寫的。
    """
    clients = [form_client(app), form_client(app)]
    forms = [{"decision": decision, "confirm_reject": "1", CSRF_FIELD: c.csrf_token()}
             for c, decision in zip(clients, ("done", "rejected"))]
    gate, real = threading.Barrier(2), Store.set_action_status

    def gated(self, action_id, status, **kwargs):
        try:
            gate.wait(timeout=1.0)      # 有鎖時另一個請求進不來,等不到就自己往下走
        except threading.BrokenBarrierError:
            pass
        return real(self, action_id, status, **kwargs)

    monkeypatch.setattr(Store, "set_action_status", gated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sent = list(pool.map(lambda pair: pair[0].post(f"/confirm/{actions['med']}", data=pair[1], follow_redirects=False),
                             zip(clients, forms)))
    assert sorted(r.status_code for r in sent) == [303, 409]
    winner = forms[[r.status_code for r in sent].index(303)]["decision"]
    assert action_status(store, actions["med"]) == winner       # 狀態是回成功的那一位決定的
    assert "已經處理過了" in next(r.text for r in sent if r.status_code == 409)


def test_decided_action_shows_on_result_page(client, store, actions):
    client.post(f"/confirm/{actions['med']}", data={"decision": "done"})
    doc_id = next(a["document_id"] for a in store.list_actions() if a["id"] == actions["med"])
    assert "家人已確認" in client.get(f"/doc/{doc_id}").text


def test_confirm_links_review_queue(client, review_doc):
    review_doc()
    html = client.get("/confirm").text
    assert 'href="/review"' in html and "1 份文件等待複核" in html


def test_empty_confirm_page_says_all_done_and_links_home(client):
    html = client.get("/confirm").text
    assert "都確認完了" in html and "第 1 件" not in html and "回首頁</a>" in html
    assert "這件事可能已經有人處理了" not in html


def test_review_link_is_only_for_those_who_can_correct(cfg, store, review_doc):
    review_doc()
    assert 'href="/review"' not in _main(_as(cfg, store, "elder").get("/confirm").text)
    assert 'href="/review"' in _main(_as(cfg, store, "family").get("/confirm").text)


# ---- 長輩:照權限只能退回 --------------------------------------------------------------

def test_elder_can_only_reject_sees_the_wait_and_can_look_at_the_others(cfg, store, actions):
    main = _main(_as(cfg, store, "elder").get("/confirm").text)
    assert 'value="done"' not in main and "btn--primary" not in main
    assert 'value="rejected"' in main and "第 1 件，共 2 件" in main
    assert '<p class="confirm__wait"><span class="tile tile--wait">等待家人確認</span></p>' in main   # 黃色狀態牌:要家人處理
    assert f'href="/confirm?item={actions["deadline"]}">看下一件</a>' in main


def test_viewer_only_reads(cfg, store, actions):
    main = _main(_as(cfg, store, None).get("/confirm").text)
    assert "<form" not in main and "等待家人確認" in main and "示範藥甲" in main and "看下一件" in main


def test_the_elder_still_cannot_confirm_by_posting(cfg, store, actions, action_status):
    elder = _as(cfg, store, "elder")
    r = elder.post(f"/confirm/{actions['med']}", data={"decision": "done", CSRF_FIELD: elder.cookies[CSRF_COOKIE]})
    assert r.status_code == 403 and action_status(store, actions["med"]) == "pending"


# ---- 照舊:原件、等了幾天、聲明、起算說明 --------------------------------------------------

def test_the_one_item_keeps_everything_a_card_had(cfg, client, store, add_doc, png_bytes):
    photo = cfg.paths.archive / "bag.png"
    photo.write_bytes(png_bytes())
    doc_id = add_doc(_result(), target=photo)
    action_id = store.add_action(doc_id, "medication_schedule", "confirm", {"items": [{"name": "示範藥甲", "timing": ["早"]}]})
    with store._conn() as conn:
        conn.execute("UPDATE actions SET created_at = ? WHERE id = ?",
                     ((datetime.now() - timedelta(days=2)).isoformat(timespec="seconds"), action_id))
    html = client.get("/confirm").text
    assert "已等 2 天" in html and f'href="/doc/{doc_id}#original">看原件與全文' in html
    assert f'src="/doc/{doc_id}/file"' in html and "本系統只協助閱讀，不提供醫療建議" in html


def test_deadline_item_keeps_the_basis_note(client, store, add_doc):
    doc_id = add_doc(_result("公文"))
    store.add_action(doc_id, "calendar", "confirm", {"title": "公文期限", "date": "2099-10-31", "basis": "以發文日期起算；期限以公文原文為準。"})
    html = client.get("/confirm").text
    assert "以發文日期起算" in html and f'/doc/{doc_id}/correct#f-fields-received_date' in html


def test_result_page_link_opens_that_item(client, store, actions):
    doc_id = next(a["document_id"] for a in store.list_actions() if a["id"] == actions["deadline"])
    page = client.get(f"/doc/{doc_id}").text
    assert f'<a href="/confirm?item={actions["deadline"]}">去確認</a>' in page       # 和首頁同一個說法(v5)
    assert 'href="/confirm">去確認' not in page and "到「家人確認」處理" not in page


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
    "disclaimer": "本系統只協助閱讀，不提供醫療建議；用藥請依醫師與藥師指示。",
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
    assert "本系統只協助閱讀，不提供醫療建議" in html   # 卡片上也有聲明
    assert "每次1顆" not in html


def test_w1c_calendar_confirm_card_shows_title_and_date(client, store, add_doc):
    store.add_action(add_doc(_result("公文")), "calendar", "confirm", {
        "title": "公文期限：補繳文件", "date": "2099-10-31",
        "description": "收到本函後15日內\n本系統只提醒，不會替您付款或回覆。",
        "remind_days_before": 3,
    })
    html = client.get("/confirm").text
    assert "公文期限：補繳文件" in html and "2099年10月31日" in html


# ---- v5 車站時刻表(11/6):期限類是看板上的一列,「確認」「退回」一樣寬、隔開 ----------------------------

def _event(html):
    """確認頁期限類那一塊看板(到「確認」「退回」的表單前為止)。"""
    return _main(html).split('<div class="confirm__event board">', 1)[1].split("<form", 1)[0]


def test_flap_numbers_come_from_the_date():
    """翻牌要數字:月、日是整數(01、05 由翻牌補零);沒有日期就是空字串,不畫牌。"""
    pending = {"id": 1, "document_id": 1, "kind": "calendar", "tier": "confirm", "status": "pending"}
    view = action_view(dict(pending, payload={"title": "繳水費", "date": "2099-01-05"}))
    assert (view["month"], view["day"]) == (1, 5) and view["date_text"] == "2099年1月5日"
    undated = action_view(dict(pending, payload={"title": "繳水費"}))
    assert (undated["month"], undated["day"]) == ("", "")


def test_deadline_item_is_one_row_on_the_board(client, store, add_doc):
    """翻牌(只給眼睛看)、事項與日期全文(讀屏念這一句)、逾期是紅色狀態牌,排在同一塊深墨看板上。"""
    due = date.today() - timedelta(days=3)
    store.add_action(add_doc(_result("帳單")), "calendar", "confirm", {"title": "繳水費", "date": due.isoformat()})
    event = _event(client.get("/confirm").text)
    flap = re.search(r'<span class="flap" aria-hidden="true">(.*?)</span>\s*<div>', event, re.S)
    assert flap and re.findall(r'<span class="flap__d">(\d)</span>', flap.group(1)) == list(f"{due.month:02d}{due.day:02d}")
    assert event.index("</span>", flap.start()) < event.index('<p class="confirm__what">繳水費</p>')   # 牌在事項前面
    assert f'<p class="confirm__date">{due.year}年{due.month}月{due.day}日' in event
    assert '<span class="tile tile--manual confirm__late">逾期 3 天</span>' in event
    assert 'class="cal"' not in event


def test_deadline_without_a_date_has_no_flap(client, store, add_doc):
    store.add_action(add_doc(_result("公文")), "calendar", "confirm", {"title": "公文期限"})
    event = _event(client.get("/confirm").text)
    assert '<p class="confirm__what">公文期限</p>' in event and "flap" not in event and "confirm__date" not in event


def test_confirm_and_reject_are_the_same_width_and_kept_apart():
    """使用者 11/6:「欄位要對齊 統一大小」。「確認」「退回」一樣寬;退回沒有復原,和確認隔 3rem(手機 1.5rem),
    手機上兩顆各佔一半,只剩「退回」(長輩)也是同樣大。"""
    assert re.search(r"\.confirm__actions \{[^}]*gap: 3rem", T3)
    assert re.search(r"\.confirm__actions \.btn \{[^}]*width: 10\.5rem", T3)
    phone = T3.split("@media (max-width: 47.99rem)")[1]
    assert re.search(r"\.confirm__actions \{[^}]*gap: 1\.5rem", phone)
    assert re.search(r"\.confirm__actions \.btn \{[^}]*flex: 0 1 calc\(50% - \.75rem\)", phone)


def test_the_board_row_and_the_late_tile_use_the_shared_parts():
    """看板與狀態牌用共用的 .board、.tile(wave 0);確認頁只排版面:翻牌在左、字在右,窄的時候換行。"""
    row = re.search(r"\.confirm__event\.board \{[^}]*\}", T3).group(0)
    assert "display: flex" in row and "flex-wrap: wrap" in row
    assert ".confirm__late {" not in CSS.split("/* ---- 家人確認")[1].split("/* ---- 更正讀值與待複核")[0]   # 舊的紅字拿掉,改用 .tile
