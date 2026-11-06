"""家人用的小表單套表單規範(使用者 11/5):家庭成員、統一發票對獎、設定頁的門檻、文件櫃的搜尋。
浮動標籤與離開欄位才檢查由 _field.html 的 ff 和 app.js 提供,這裡確認每個欄位都接上了。身分都是合成的,不呼叫模型。"""
import re

import pytest
from fastapi.testclient import TestClient

from src.lottery import normalize_number
from src.providers import MockAnalyzer
from web.access import Identity
from web.app import CSRF_COOKIE, CSRF_FIELD, create_app
from web.render import LOTTERY_NUMBER_PATTERN, STATIC_DIR

PERIOD = "115-09"
FORM = {"period": PERIOD, "special": "12345678", "grand": "87654321", "first1": "11112222", "first2": "33334444",
        "first3": "55556666", "claim_by": "2027-02-28"}


def _as(cfg, store, role="family", name="女兒"):
    email = f"{role}@example.test"
    store.set_member(email, role, name)
    store.set_setting(f"consent:{email}", "2026-10-28T08:00:00", actor=email)

    class Fixed:
        def resolve(self, request):
            return Identity(email, False)

    c = TestClient(create_app(cfg, analyzer=MockAnalyzer(cfg), identity_resolver=Fixed()))
    c.get("/")
    return c


def _post(c, url, **form):
    return c.post(url, data={CSRF_FIELD: c.cookies[CSRF_COOKIE], **form}, follow_redirects=False)


def _field(html, input_id):
    """ff 巨集畫的那一格:從 <div class="ff 到下一個欄位之前,含輸入框、標籤、灰字與錯誤。"""
    at = html.index(f'id="{input_id}"')
    start = [m.start() for m in re.finditer(r'<div class="ff[ "]', html[:at])][-1]
    return html[start:html.index("</p>", html.index(f'id="{input_id}-error"', at))]


def _input(html, input_id):
    return re.search(rf'<input[^>]*\bid="{input_id}"[^>]*>', html, re.S).group(0)


def _add_form(html):
    return html[html.index('id="add-member-title"'):]


def _problem_free(html):
    assert not re.search(r"<script(?![^>]*\bsrc=)|\son[a-z]+\s*=|\sstyle\s*=|javascript:", html, re.I)


# ---- 家庭成員 ---------------------------------------------------------------------

def test_new_member_form_follows_the_rules(cfg, store):
    html = _as(cfg, store).get("/settings/members").text
    email = _input(html, "member-email")
    assert 'type="email"' in email and "required" in email and 'placeholder=" "' in email and "ff__input" in email
    assert 'data-error="請輸入完整的電子郵件，例如 name@example.com"' in email
    assert 'aria-describedby="member-email-error"' in email
    # 大部分必填:只在稱呼標選填,不標星號
    add = _add_form(html)
    assert '<span class="ff__opt">（選填）</span>' in _field(html, "member-name") and "ff__opt" not in _field(html, "member-email")
    assert "ff__req" not in add
    assert 'data-error' not in _input(html, "member-name")
    _problem_free(html)


def test_role_is_two_flat_radios_on_every_form(cfg, store):
    client = _as(cfg, store)
    store.set_member("elder@example.test", "elder", "媽媽")
    html = client.get("/settings/members").text
    assert "<select" not in html
    assert html.count("<legend>角色</legend>") == 3                                 # 兩位成員 + 加入成員
    checked = re.findall(r'name="role" value="(\w+)" checked', html)
    assert checked == ["elder", "family", "family"] or checked == ["family", "elder", "family"]
    labels = re.findall(r'name="role" value="\w+"[^>]*><span>([^<]+)</span>', html)
    assert labels == ["家人", "長輩"] * 3


def test_member_save_still_works_with_the_radios(cfg, store):
    client = _as(cfg, store)
    r = _post(client, "/settings/members", email="elder@example.test", role="elder", display_name="媽媽")
    assert r.status_code == 303
    assert store.get_member("elder@example.test")["role"] == "elder"


def test_bad_email_is_said_right_under_the_field_and_keeps_what_was_typed(cfg, store):
    client = _as(cfg, store)
    r = _post(client, "/settings/members", email="not-an-email", role="elder", display_name="爸爸")
    assert r.status_code == 400
    field = _field(r.text, "member-email")
    assert "請輸入完整的電子郵件，例如 name@example.com" in field and "ff--error" in field
    email = _input(r.text, "member-email")
    assert 'value="not-an-email"' in email and 'aria-invalid="true"' in email and "autofocus" in email
    assert 'value="爸爸"' in _input(r.text, "member-name")
    assert 'name="role" value="elder" checked' in _add_form(r.text)                 # 剛選的角色留著
    assert '<p class="alert"' not in r.text                                         # 不在頁面最上面再寫一次


def test_empty_email_is_refused_the_same_way(cfg, store):
    r = _post(_as(cfg, store), "/settings/members", email="", role="family", display_name="")
    assert r.status_code == 400 and "請輸入完整的電子郵件" in _field(r.text, "member-email")


def test_name_too_long_is_said_under_the_name(cfg, store):
    r = _post(_as(cfg, store), "/settings/members", email="new@example.test", role="family", display_name="長" * 41)
    assert r.status_code == 400 and "稱呼請使用 40 字以內的文字。" in _field(r.text, "member-name")
    assert "稱呼請使用" not in _field(r.text, "member-email")


def test_keeping_one_family_member_is_said_in_that_members_card(cfg, store):
    client = _as(cfg, store)
    r = _post(client, "/settings/members", email="family@example.test", role="elder", display_name="女兒")
    assert r.status_code == 400 and store.get_member("family@example.test")["role"] == "family"
    card = r.text[r.text.index("<article"):r.text.index("</article>")]
    assert "至少保留一位家人" in card and "至少保留一位家人" not in _add_form(r.text)
    assert 'id="role-error-1" role="alert"' in card and 'aria-describedby="role-error-1"' in card
    assert 'value="family" checked autofocus' in card
    assert 'value="family@example.test"' not in _input(r.text, "member-email")      # 不把成員的資料填進「加入」


def test_removing_the_last_family_member_is_said_in_the_card(cfg, store):
    client = _as(cfg, store)
    r = _post(client, "/settings/members/remove", email="family@example.test", confirmed="1")
    assert r.status_code == 400 and store.get_member("family@example.test") is not None
    assert "至少保留一位家人" in r.text[r.text.index("<article"):r.text.index("</article>")]


def test_only_add_member_is_primary(cfg, store):
    main = _as(cfg, store).get("/settings/members").text.split('<main id="main"', 1)[1]
    assert main.count("btn--primary") == 1 and "加入成員</button>" in main


# ---- 統一發票對獎 ------------------------------------------------------------------

def test_numbers_have_floating_labels_digits_help_and_a_blur_check(cfg, store):
    html = _as(cfg, store).get(f"/lottery?period={PERIOD}").text
    for name in ("special", "grand", "first1", "first2", "first3"):
        field, box = _field(html, f"lot-{name}"), _input(html, f"lot-{name}")
        assert "ff__label" in field and 'placeholder=" "' in box and "ff--fixed" not in field
        assert f'<p class="ff__help" id="lot-{name}-help">8 位數字</p>' in field
        assert "required" in box and 'data-error="請輸入公告上的 8 位數字"' in box
        assert f'pattern="{LOTTERY_NUMBER_PATTERN}"' in box
        assert f'aria-describedby="lot-{name}-help lot-{name}-error"' in box
    _problem_free(html)


@pytest.mark.parametrize("text", ["12345678", "１２３４５６７８", "1234 5678", "1234-5678", " 12345678 ", "1234", "123456789",
                                  "abcdefgh", "1234567a", "", "1-2-3-4-5-6-7-8"])
def test_blur_check_accepts_what_the_server_accepts(text):
    """瀏覽器的 pattern 和伺服器的 normalize_number 不能不一致:過不了的字,送出也一定過不了,反過來也一樣。"""
    assert bool(re.fullmatch(LOTTERY_NUMBER_PATTERN, text)) == (normalize_number(text) is not None)


def test_claim_date_label_stays_on_top(cfg, store):
    html = _as(cfg, store).get(f"/lottery?period={PERIOD}").text
    field, box = _field(html, "lot-claim"), _input(html, "lot-claim")
    assert '<div class="ff ff--fixed' in field and 'type="date"' in box and 'name="claim_by"' in box
    assert 'data-error="請照公告選領獎期限的日期"' in box and "required" in box and "ff__help" not in field


def test_wrong_numbers_are_said_under_each_field_and_stay_filled(cfg, store):
    client = _as(cfg, store)
    r = _post(client, "/lottery", **{**FORM, "special": "1234", "claim_by": ""})
    assert r.status_code == 400
    special = _field(r.text, "lot-special")
    assert "ff--error" in special and "請輸入公告上的 8 位數字" in special and 'aria-invalid="true"' in _input(r.text, "lot-special")
    assert 'value="1234"' in _input(r.text, "lot-special")
    assert "ff--error" not in _field(r.text, "lot-grand") and 'value="87654321"' in _input(r.text, "lot-grand")
    assert "請照公告選領獎期限的日期" in _field(r.text, "lot-claim")


def test_full_width_numbers_are_still_accepted(cfg, store):
    client = _as(cfg, store)
    r = _post(client, "/lottery", **{**FORM, "special": "１２３４ ５６７８"})
    assert r.status_code == 303


def test_elder_reads_the_numbers_without_rules_or_requirements(cfg, store):
    _post(_as(cfg, store), "/lottery", **FORM)
    html = _as(cfg, store, "elder", "媽媽").get(f"/lottery?period={PERIOD}").text
    box = _input(html, "lot-special")
    assert "readonly" in box and "required" not in box and "data-error" not in box and 'value="12345678"' in box
    assert "ff__help" not in html and "readonly" in _input(html, "lot-claim") and ">對獎</button>" not in html


# ---- 設定頁的門檻 ------------------------------------------------------------------

def test_threshold_radios_save_like_the_select_did(client, store):
    html = client.get("/settings").text
    assert "<legend>自動存檔門檻</legend>" in html and 'id="auto-threshold"' not in html
    assert client.post("/settings", data={"auto_threshold": "0.90"}, follow_redirects=False).status_code == 303
    assert store.get_settings()["auto_threshold"] == "0.90"
    assert 'name="auto_threshold" value="0.90" checked' in client.get("/settings").text


def test_an_unusual_current_threshold_is_still_listed(client, store):
    client.post("/settings", data={"auto_threshold": "0.87"})
    html = client.get("/settings").text
    assert 'name="auto_threshold" value="0.87" checked><span>87%</span>' in html
    assert html.count('name="auto_threshold"') == 5


def test_other_setting_errors_stay_on_top(client, store):
    r = client.post("/settings", data={"provider": "沒有這個", "auto_threshold": "0.5"})
    assert r.status_code == 400
    assert '<p class="field-error" role="alert">' in r.text and 'id="threshold-error"' in r.text


# ---- 文件櫃的搜尋 ------------------------------------------------------------------

def test_search_box_has_a_floating_label(client):
    html = client.get("/cabinet").text
    field, box = _field(html, "cabinet-q"), _input(html, "cabinet-q")
    assert '<label class="ff__label" for="cabinet-q">找文件' in field and 'placeholder=" "' in box
    assert 'type="search"' in box and 'placeholder="找文件"' not in html and "required" not in box
    assert "ff__opt" not in field and "ff__req" not in field                         # 搜尋不是要填的資料,不標必填或選填


def test_search_keeps_the_words_and_still_works(client, add_doc):
    add_doc({"doc_type": "帳單", "vendor": "示範電力公司", "fields": {}})
    html = client.get("/cabinet", params={"q": "電力"}).text
    assert 'value="電力"' in _input(html, "cabinet-q") and "找「電力」" in html


# ---- 樣式只寫在自己的段落 ----------------------------------------------------------

def test_small_links_and_radios_keep_their_touch_area():
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    section = css[css.index("/* ---- 確認頁與家人用的小表單 ---- */"):css.index("/* (確認頁與小表單的規則寫在這一行上面) */")]
    block = section[section.index(".confirm__next"):]
    assert "min-height: 2.75rem" in block[:block.index("}")]
    chip = re.search(r"^\.chip \{[^}]*\}", css, re.M).group(0)
    assert "min-height: 2.75rem" in chip
