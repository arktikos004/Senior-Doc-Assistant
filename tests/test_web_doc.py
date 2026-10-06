"""結果頁、原件路由與網頁提醒測試(資料直接寫進 tmp 的 SQLite,不呼叫模型)。"""
import os
import sys

import pytest

from samples import BILL, LETTER
from web.render import (
    conf_class,
    fallback_summary,
    field_rows,
    fmt_conf,
    fmt_pct,
    is_pdf,
    review_reason,
    verification_view,
)


def test_unknown_route_error_page_is_chinese(client):
    r = client.get("/no-such-page")
    assert r.status_code == 404 and "找不到這個頁面" in r.text and "Not Found" not in r.text


def test_unknown_doc_is_404_html(client):
    r = client.get("/doc/999")
    assert r.status_code == 404
    assert "找不到這份文件" in r.text and "<html" in r.text


def test_malformed_doc_id_is_chinese_page_not_json(client):
    """網址參數格式不對時 FastAPI 預設回英文 JSON;長輩要看到中文頁與下一步。"""
    r = client.get("/doc/abc")
    assert r.status_code == 422 and r.headers["content-type"].startswith("text/html")
    assert "送出的資料不完整" in r.text and "上一頁" in r.text
    assert '"detail"' not in r.text and "int_parsing" not in r.text


@pytest.mark.parametrize("doc_id", ["99999999999999999999", "-99999999999999999999", str(2 ** 63), "0"])
def test_doc_id_beyond_sqlite_range_is_404_not_500(client, doc_id):
    """超出 SQLite 整數範圍的編號當成找不到這份文件:不是 500,伺服器紀錄也不留 traceback(SEC-11)。"""
    for url in (f"/doc/{doc_id}", f"/doc/{doc_id}/file", f"/doc/{doc_id}/correct"):
        r = client.get(url)
        assert r.status_code == 404 and "找不到這份文件" in r.text, url
    assert client.post(f"/doc/{doc_id}/correct", data={"amount": "1"}).status_code == 404
    assert client.post(f"/doc/{doc_id}/reject").status_code == 404


def test_result_page_shows_summary_fields_and_speak_button(client, add_doc):
    doc_id = add_doc(BILL)
    html = client.get(f"/doc/{doc_id}").text
    assert "AI 白話解說" in html
    assert "這是電費帳單,要在10月15日前繳1854元。" in html
    assert "1,854 元" in html
    assert "2099年10月15日" in html                  # 繳費期限用長輩看得懂的寫法
    assert "繳費期限" in html and "示範電力公司" in html
    assert "data-speak" in html and "唸給我聽" in html
    assert "data-speak-unsupported" in html          # 瀏覽器不支援時的說明
    assert "不提供醫療建議" not in html


def test_missing_summary_falls_back_without_claiming_ai(client, add_doc):
    result = dict(BILL, plain_summary="")
    html = client.get(f"/doc/{add_doc(result)}").text
    assert 'id="summary"' in html
    assert "重點整理" in html and "AI 白話解說" not in html
    assert "這是一張帳單。" in html


def test_model_text_is_escaped(client, add_doc):
    result = dict(BILL, plain_summary='<img src=x onerror="alert(1)">', vendor="<b>商店</b>")
    html = client.get(f"/doc/{add_doc(result)}").text
    assert "<img src=x" not in html and "<b>商店</b>" not in html
    assert "&lt;img src=x" in html


def test_without_verification_says_unverified(client, add_doc):
    html = client.get(f"/doc/{add_doc(BILL)}").text
    assert "未核對" in html


def test_verification_badges_use_text_not_only_color(client, add_doc):
    result = dict(BILL, verification={
        "賣方統編檢查碼": {"status": "pass", "detail": "統編檢查碼正確", "fields": ["fields.seller_tax_id"]},
        "QR 總計額": {"status": "fail", "detail": "QR 金額 1850 與讀到的 1854 不同", "fields": ["amount"]},
        "一維條碼": {"status": "skip", "detail": "找不到一維條碼", "fields": []},
        "_coverage": {"amount": True},
    }, verified_confidence=0.3)
    html = client.get(f"/doc/{add_doc(result)}").text
    assert "check--pass" in html and "check--fail" in html and "check--skip" in html
    assert '<span class="check__status">通過</span>' in html      # 檢查碼只是規則
    assert '<span class="check__status">不符</span>' in html      # 與 QR 矛盾
    assert '<span class="check__status">無法核對</span>' in html
    assert "有 1 項不符" in html
    assert "QR 金額 1850 與讀到的 1854 不同" in html
    assert "_coverage" not in html
    assert "驗證信心 30%" in html
    assert "field-check--fail" in html               # 金額那一列標出「不符」


def test_rule_checks_are_not_shown_as_proven(client, add_doc):
    """只有格式與合理性規則(弱證據)時,不能寫成「相符」或「已核對」。"""
    result = dict(BILL, verification={
        "繳費期限": {"status": "pass", "detail": "格式正確且不早於出帳日", "fields": ["fields.due_date"]},
        "金額合理性": {"status": "pass", "detail": "金額在合理範圍內", "fields": ["amount"]},
    }, verified_confidence=0.85)
    html = client.get(f"/doc/{add_doc(result)}").text
    assert '<span class="check__status">通過</span>' in html
    assert "檢查通過 2 項" in html and "相符" not in html
    assert "沒有 QR Code 可以比對" in html and "不能證明讀對" in html
    assert "已檢查" in html and "已核對" not in html


def test_qr_match_is_the_only_thing_called_a_match(client, add_doc):
    result = dict(BILL, verification={
        "QR 總計額": {"status": "pass", "detail": "QR 記載總計 1854,與辨識金額相符", "fields": ["amount"]},
        "日期合理性": {"status": "pass", "detail": "日期合理", "fields": ["date"]},
    }, verified_confidence=0.9)
    html = client.get(f"/doc/{add_doc(result)}").text
    assert '<span class="check__status">相符</span>' in html and '<span class="check__status">通過</span>' in html
    assert "QR Code 相符 1 項" in html and "比對發票上的 QR Code" in html
    assert "與 QR 相符" in html                      # 金額那一列有獨立證據


def test_actions_are_labelled_by_tier(client, store, add_doc):
    doc_id = add_doc(BILL)
    store.add_action(doc_id, "calendar", "auto",
                     {"title": "繳電費", "date": "2099-10-15", "description": "", "remind_days_before": 1})
    store.add_action(doc_id, "medication_schedule", "confirm",
                     {"items": [{"name": "示範藥甲", "timing": ["早", "晚"], "prn": False}]})
    store.add_action(doc_id, "todo", "manual", {"title": "打電話詢問"})
    html = client.get(f"/doc/{doc_id}").text
    assert "已列入提醒" in html
    assert "等待家人確認" in html
    assert "需要人工處理" in html


def test_medication_doc_always_shows_disclaimer(client, add_doc):
    result = {"doc_type": "藥袋", "date": "2026-09-30", "vendor": "示範診所", "plain_summary": "",
              "fields": {"items": [{"name": "示範藥甲", "dose_text": "1顆", "frequency_text": "睡前",
                                    "timing": ["睡前"], "prn": False, "days": 7}]}}
    html = client.get(f"/doc/{add_doc(result)}").text
    assert "本系統只協助閱讀,不提供醫療建議;用藥請依醫師與藥師指示。" in html
    assert "示範藥甲" in html and "7 天" in html
    assert "醫療院所" in html                         # 藥袋的 vendor 顯示為醫療院所


def test_review_doc_explains_in_plain_words(client, add_doc):
    """轉人工的文件:狀態寫「等待複核」,原因用白話,不出現門檻數字。"""
    result = dict(BILL, verification={"QR 總計額": {"status": "fail", "detail": "不同", "fields": ["amount"]}},
                  verified_confidence=0.1)
    html = client.get(f"/doc/{add_doc(result, action='review', reason='驗證信心 0.10(與 QR 不符:總計額)低於門檻 0.80,需人工確認')}").text
    assert "等待複核" in html and "讀到的金額和發票上的 QR Code 不一樣" in html
    assert "低於門檻" not in html
    assert "已存檔" in client.get(f"/doc/{add_doc(BILL)}").text


def test_failed_doc_invites_retake(client, add_doc):
    doc_id = add_doc(None, action="failed", reason="AI 辨識失敗或回應無法解析")
    html = client.get(f"/doc/{doc_id}").text
    assert "這張讀不出來" in html and "重新拍一張" in html


# ---- 畫面資料的共用規則(render) ------------------------------------------------

def test_bool_is_not_a_number():
    """模型把 true/false 填進數字欄位時,不當成 1 或 0 顯示。"""
    assert fmt_conf(True) == "—" and fmt_pct(True) == "—" and conf_class(False) == ""
    assert fmt_conf(0.5) == "0.50" and conf_class(0.3) == "low" and conf_class(0.6) == "mid"
    assert fallback_summary({"doc_type": "收據", "amount": True}) == "這是一張收據。"


@pytest.mark.parametrize("spelling", ["due_date", "fields.due_date"])
def test_unreadable_accepts_both_field_spellings(spelling):
    """欄位名寫 'due_date' 或 'fields.due_date' 都是同一個欄位,讀不清與核對結果用同一條規則。"""
    result = {"doc_type": "帳單", "amount": 1854.0, "fields": {"due_date": "2099-10-15"}, "unreadable": [spelling]}
    rows = {r["key"]: r for r in field_rows(result)}
    assert rows["fields.due_date"]["missing"] == "讀不清楚,請看原件" and rows["amount"]["missing"] == ""
    assert review_reason(result) == "有必要的欄位沒讀到(繳費期限),請對照原件補上。"


def test_is_pdf_is_the_single_rule_for_originals(tmp_path):
    """結果頁、待複核、家人確認的縮圖都用同一個判斷;大寫副檔名、Path、沒有原件都要對。"""
    assert is_pdf("帳單.PDF") and is_pdf(tmp_path / "a.pdf")
    assert not is_pdf("a.png") and not is_pdf(None) and not is_pdf("pdf.png")


def test_malformed_verification_counts_as_unverified():
    """verification 不是 dict(模型或舊資料的格式錯誤)時當成還沒核對,頁面不會壞掉。"""
    for bad in (["QR 總計額"], "fail"):
        result = dict(BILL, verification=bad)
        assert verification_view(result)["overall"] == "none"
        assert all(not r.get("check") for r in field_rows(result))
        assert "沒有把握" in review_reason(result)


# ---- 原件路由 ---------------------------------------------------------------

def test_file_served_from_archive(cfg, client, add_doc):
    target = cfg.paths.archive / "帳單" / "a.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    doc_id = add_doc(BILL, target=target)
    r = client.get(f"/doc/{doc_id}/file")
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in r.headers["cache-control"]      # 原件可能是藥袋,不快取
    assert f'src="/doc/{doc_id}/file"' in client.get(f"/doc/{doc_id}").text


# 每種收得下的副檔名該回的型別(.WEBP:歸檔沿用原檔的副檔名,大小寫不一定)
_ORIGINAL_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
                   ".WEBP": "image/webp", ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
                   ".pdf": "application/pdf"}


@pytest.mark.parametrize("ext, expected", sorted(_ORIGINAL_TYPES.items()))
def test_original_content_type_does_not_depend_on_the_host(cfg, client, add_doc, monkeypatch, ext, expected):
    """原件的 Content-Type 由程式裡的固定對照表決定(SEC-06):主機的 mimetypes 不認得時(這台 Windows 沒有 .webp),
    不會變成 application/octet-stream,讓「放大看原件(另開新頁)」變成下載、在裝置留下一份照片。"""
    monkeypatch.setattr("starlette.responses.guess_type", lambda *args, **kwargs: (None, None))
    target = cfg.paths.archive / f"a{ext}"
    target.write_bytes(b"fake")
    r = client.get(f"/doc/{add_doc(BILL, target=target)}/file")
    assert r.status_code == 200 and r.headers["content-type"] == expected
    assert r.headers["content-disposition"] == "inline"          # 明寫在瀏覽器裡看,不是下載
    assert "no-store" in r.headers["cache-control"] and r.headers["x-content-type-options"] == "nosniff"


def test_every_supported_extension_has_a_fixed_content_type(cfg):
    """上傳白名單多收一種格式時,這裡會提醒對照表也要補(漏了就退回靠主機猜)。"""
    assert set(cfg.supported_extensions) == {ext.lower() for ext in _ORIGINAL_TYPES}


@pytest.mark.parametrize("folder", ["review", "failed", "uploads"])
def test_file_served_from_other_allowed_folders(cfg, client, add_doc, folder):
    base = cfg.paths.uploads_path if folder == "uploads" else getattr(cfg.paths, folder)
    target = base / "b.jpg"
    target.write_bytes(b"\xff\xd8\xff fake")
    assert client.get(f"/doc/{add_doc(BILL, target=target)}/file").status_code == 200


def test_file_outside_allowed_folders_is_404(cfg, client, add_doc, tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"top secret")
    doc_id = add_doc(BILL, target=secret)
    assert client.get(f"/doc/{doc_id}/file").status_code == 404
    html = client.get(f"/doc/{doc_id}").text
    assert f"/doc/{doc_id}/file" not in html and "原件已經移走" in html


def test_file_traversal_via_target_path_is_404(cfg, client, add_doc, tmp_path):
    (tmp_path / "secret.png").write_bytes(b"top secret")
    sneaky = cfg.paths.archive / ".." / "secret.png"
    assert client.get(f"/doc/{add_doc(BILL, target=sneaky)}/file").status_code == 404


def test_file_symlink_escaping_archive_is_404(cfg, client, add_doc, tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"top secret")
    link = cfg.paths.archive / "link.png"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("此平台不能建立符號連結")
    assert client.get(f"/doc/{add_doc(BILL, target=link)}/file").status_code == 404


def _link_folder(link, target) -> None:
    """建一個指到 target 的資料夾連結。Windows 用 junction:一般帳號就能建(符號連結要管理員權限,
    上面那個測試在這種電腦會被跳過),而且 junction 的 is_symlink() 是 False,只擋符號連結的程式擋不到它。
    其他平台用資料夾的符號連結;真的建不起來才跳過。"""
    try:
        if sys.platform == "win32":
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("此平台不能建立資料夾連結")


def test_file_through_a_folder_link_escaping_archive_is_404(cfg, client, add_doc, tmp_path):
    """archive/ 裡有一個指到外面的資料夾連結(Windows 的 junction):原件路由不經過它送出外面的檔,
    結果頁也不放原件;連結指到的若還在允許的資料夾裡(例如 archive 的子資料夾),照常送。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"top secret")
    _link_folder(cfg.paths.archive / "j-out", outside)
    if sys.platform == "win32":
        assert not (cfg.paths.archive / "j-out").is_symlink()         # junction 不算符號連結
    assert (cfg.paths.archive / "j-out" / "secret.png").read_bytes() == b"top secret"   # 連結是通的
    doc_id = add_doc(BILL, target=cfg.paths.archive / "j-out" / "secret.png")
    assert client.get(f"/doc/{doc_id}/file").status_code == 404
    html = client.get(f"/doc/{doc_id}").text
    assert f"/doc/{doc_id}/file" not in html and "原件已經移走" in html

    inside = cfg.paths.archive / "帳單"
    inside.mkdir()
    (inside / "a.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    _link_folder(cfg.paths.archive / "j-in", inside)
    assert client.get(f"/doc/{add_doc(BILL, target=cfg.paths.archive / 'j-in' / 'a.png')}/file").status_code == 200


def test_reject_does_not_move_a_file_reached_through_a_folder_link(cfg, client, store, add_doc, tmp_path):
    """待複核文件的原件位置經過指到外面的資料夾連結:退回時不把外面的檔搬進 failed/(原件當作不在)。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.png"
    secret.write_bytes(b"top secret")
    _link_folder(cfg.paths.review / "j-out", outside)
    doc_id = add_doc(BILL, action="review", target=cfg.paths.review / "j-out" / "secret.png")
    r = client.post(f"/doc/{doc_id}/reject", data={"confirm_reject": "1"}, follow_redirects=False)
    assert r.status_code == 303 and store.get_document(doc_id)["action"] == "failed"
    assert secret.read_bytes() == b"top secret" and not any(cfg.paths.failed.iterdir())


def test_file_in_logs_or_unsupported_type_is_404(cfg, client, add_doc):
    assert client.get(f"/doc/{add_doc(BILL, target=cfg.paths.db_path)}/file").status_code == 404
    sidecar = cfg.paths.review / "x.png.ai.json"
    sidecar.write_text("{}", encoding="utf-8")
    assert client.get(f"/doc/{add_doc(BILL, target=sidecar)}/file").status_code == 404
    assert client.get(f"/doc/{add_doc(BILL, target=None)}/file").status_code == 404


def _original_img(html):
    return html.split('<figure class="original">')[1].split("</figure>")[0]


def test_original_reserves_its_size(cfg, client, add_doc):
    """原件 <img> 延遲載入並帶寬高(版面不跳);EXIF 轉 90 度的照片寬高對調;讀不到尺寸就不帶。"""
    from PIL import Image
    upright = cfg.paths.archive / "upright.png"
    Image.new("RGB", (30, 40), "white").save(upright)
    img = _original_img(client.get(f"/doc/{add_doc(BILL, target=upright)}").text)
    assert 'loading="lazy"' in img and 'width="30" height="40"' in img
    rotated = cfg.paths.archive / "rotated.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6                                  # 手機直拍常見:檔案存成橫的,顯示時轉 90 度
    Image.new("RGB", (40, 30), "white").save(rotated, exif=exif)
    assert 'width="30" height="40"' in _original_img(client.get(f"/doc/{add_doc(BILL, target=rotated)}").text)
    broken = cfg.paths.archive / "broken.png"
    broken.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    img = _original_img(client.get(f"/doc/{add_doc(BILL, target=broken)}").text)
    assert 'src="/doc/' in img and "width=" not in img


def test_pdf_original_offers_open_link(cfg, client, add_doc):
    target = cfg.paths.archive / "c.pdf"
    target.write_bytes(b"%PDF-1.4 fake")
    html = client.get(f"/doc/{add_doc(BILL, target=target)}").text
    assert "另開原檔" in html


# ---- 提醒只列在網頁上(2026-10-01 拿掉 .ics 下載)---------------------------

def test_reminder_stays_on_the_web_without_calendar_file(client, store, add_doc):
    doc_id = add_doc(BILL)
    aid = store.add_action(doc_id, "calendar", "auto",
                           {"title": "繳電費", "date": "2099-10-15", "description": "", "remind_days_before": 3})
    assert client.get(f"/doc/{doc_id}/action/{aid}.ics").status_code == 404
    page, home = client.get(f"/doc/{doc_id}").text, client.get("/").text
    for html in (page, home):
        assert "已列入提醒" in html
        assert "行事曆" not in html and f"/action/{aid}.ics" not in html   # 不宣稱、也不提供加入行事曆
    assert "到期前會列在首頁「要記得的事」" in page
    assert "提前 3 天提醒" not in page                                    # 沒有東西會提前通知,就不寫
    assert "繳電費" in home and "10月15日前" in home


def test_w1c_calendar_description_lines_are_separate(client, store, add_doc):
    doc_id = add_doc(BILL)
    store.add_action(doc_id, "calendar", "auto", {
        "title": "繳電費", "date": "2099-10-15",
        "description": "電費 1,854 元\n<b>繳費期限</b> 2099-10-15\n本系統只提醒,不會替您付款或回覆。",
        "remind_days_before": 3,
    })
    html = client.get(f"/doc/{doc_id}").text
    assert '<p class="todo-item__line">電費 1,854 元</p>' in html
    assert '<p class="todo-item__line">本系統只提醒,不會替您付款或回覆。</p>' in html
    assert "&lt;b&gt;繳費期限&lt;/b&gt;" in html


def test_official_letter_shows_computed_and_original_deadline(client, add_doc):
    html = client.get(f"/doc/{add_doc(LETTER)}").text
    assert "期限(推算)" in html and "2099年10月10日" in html
    assert "期限原文" in html and "收到本函後15日內" in html
    assert "發文機關" in html and "示範區公所" in html
    assert "<li>補繳身分證影本</li>" in html
    assert "主旨:請補繳文件" in html                # 沒有 AI 解說時的重點整理


def test_medication_action_on_result_page_uses_slots(client, store, add_doc):
    item = {"name": "示範藥甲", "dose_text": "每次1顆", "frequency_text": "一天三次",
            "timing": [], "prn": False, "days": 7}
    payload = {"title": "示範診所的服藥時間表", "hospital": "示範診所", "slots": [],
               "prn": [], "unscheduled": [item], "items": [item],
               "disclaimer": "本系統只協助閱讀,不提供醫療建議;用藥請依醫師與藥師指示。"}
    result = {"doc_type": "藥袋", "date": "2026-09-30", "vendor": "示範診所", "plain_summary": "",
              "fields": {"items": [item]}}
    doc_id = add_doc(result)
    store.add_action(doc_id, "medication_schedule", "confirm", payload)
    html = client.get(f"/doc/{doc_id}").text
    assert "等待家人確認" in html and "示範診所的服藥時間表" in html
    assert "時段請依藥袋或詢問藥師" in html
    assert html.count("本系統只協助閱讀,不提供醫療建議") == 1   # 結果頁只放一次固定聲明
