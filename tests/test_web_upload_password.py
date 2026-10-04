"""加密 PDF(PDF-PW):上傳後請家人輸入密碼,解開了照常辨識;密碼只用一次,不存、不記、不回顯。

加密 PDF 由 tests/samples.py 當場產生(合成、密碼是虛構的 PDF_PASSWORD);不連網、不呼叫模型。
"""
import json
import logging
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from samples import PDF_PASSWORD, encrypted_pdf, plain_pdf
from src.preprocess import pdf_needs_password
from src.providers import MockAnalyzer
from web.render import CONFIRM_PURGE_FIELD

_PASSWORD_URL = re.compile(r"/upload/password/([A-Za-z0-9_-]{32})")


class RecordingAnalyzer(MockAnalyzer):
    """記下收到的檔案、類型提示與 local_only,確認解開之後照家人原本的選擇辨識。"""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.calls: list[dict] = []

    def analyze(self, file_path, doc_type_hint=None, *, local_only=False):
        self.calls.append({"needs_password": pdf_needs_password(file_path), "hint": doc_type_hint,
                           "local_only": local_only})
        return super().analyze(file_path, doc_type_hint)


@pytest.fixture
def analyzer(cfg):
    return RecordingAnalyzer(cfg)


def _upload(client, content: bytes, **data):
    return client.post("/upload", files={"file_pick": ("帳單.pdf", content, "application/pdf")},
                       data={"doc_type": "不確定", **data}, follow_redirects=False)


def _locked(client, **data) -> str:
    """上傳一份加密 PDF,回傳輸入密碼那一頁的網址。"""
    r = _upload(client, encrypted_pdf(), **data)
    assert r.status_code == 303 and _PASSWORD_URL.fullmatch(r.headers["location"]), r.headers.get("location")
    return r.headers["location"]


def _pending(cfg) -> list:
    folder = cfg.paths.uploads_path / "pending"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def _try(client, url: str, password: str, **data):
    return client.post(url, data={"password": password, **data}, follow_redirects=False)


# ---- 轉到輸入密碼那一頁 ---------------------------------------------------------

def test_encrypted_pdf_asks_for_the_password_before_reading(cfg, client, store, analyzer):
    url = _locked(client)

    page = client.get(url)
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    html = page.text
    assert "這份 PDF 有密碼" in html and "不會存起來" in html
    field = re.search(r"<input[^>]*name=\"password\"[^>]*>", html).group(0)
    assert 'type="password"' in field and 'autocomplete="off"' in field and "required" in field
    assert "解開並辨識" in html and "先不要" in html
    # 還沒辨識、沒有文件;加密的檔原封不動等在 pending/
    assert analyzer.calls == [] and store.list_documents() == []
    assert len(_pending(cfg)) == 2 and all(pdf_needs_password(p) for p in (cfg.paths.uploads_path / "pending").glob("*.pdf"))


def test_plain_pdf_is_read_right_away(cfg, client, analyzer):
    r = _upload(client, plain_pdf())
    assert r.status_code == 303 and re.fullmatch(r"/doc/\d+", r.headers["location"])
    assert _pending(cfg) == [] and len(analyzer.calls) == 1


# ---- 密碼對了 -------------------------------------------------------------------

def test_right_password_continues_with_the_original_choices(cfg, client, store, analyzer):
    url = _locked(client, category="生活契約", doc_type="水電瓦斯費")

    r = _try(client, url, PDF_PASSWORD)

    assert r.status_code == 303 and re.fullmatch(r"/doc/\d+", r.headers["location"])
    assert analyzer.calls == [{"needs_password": False, "hint": "帳單", "local_only": False}]  # 拿到的是解開的檔
    (doc,) = store.list_documents()
    assert doc["category"] == "生活契約" and doc["doc_label"] == "水電瓦斯費"
    assert pdf_needs_password(Path(doc["target_path"])) is False     # 存下來的原件是解開的版本
    assert _pending(cfg) == []                                               # 加密的暫存檔不留
    assert client.get(url).status_code == 404                                # 同一個網址不能再用


def test_sensitive_category_stays_local_after_the_password(client, analyzer):
    url = _locked(client, category="醫療與保險", doc_type="不確定")
    _try(client, url, PDF_PASSWORD)
    assert [c["local_only"] for c in analyzer.calls] == [True]


def test_document_original_can_be_viewed_after_unlocking(client):
    r = _try(client, _locked(client), PDF_PASSWORD)
    original = client.get(r.headers["location"] + "/file")
    assert original.status_code == 200 and original.headers["content-type"] == "application/pdf"
    assert b"/Encrypt" not in original.content


# ---- 密碼不對、先不要、逾時 -------------------------------------------------------

def test_wrong_password_can_be_tried_again(cfg, client, store, analyzer):
    url = _locked(client)

    r = _try(client, url, "0000-wrong")

    assert r.status_code == 400
    assert "密碼不對" in r.text and "還可以再試 4 次" in r.text
    assert "0000-wrong" not in r.text                         # 打過的密碼不回顯
    assert analyzer.calls == [] and store.list_documents() == [] and len(_pending(cfg)) == 2
    assert _try(client, url, PDF_PASSWORD).status_code == 303   # 之後打對了照常


def test_five_wrong_passwords_delete_the_waiting_file(cfg, client, store):
    url = _locked(client)

    replies = [_try(client, url, f"wrong-{i}") for i in range(5)]

    assert [r.status_code for r in replies] == [400] * 5
    assert "還可以再試 1 次" in replies[3].text
    assert "密碼錯太多次" in replies[4].text and "重新上傳" in replies[4].text
    assert _pending(cfg) == [] and store.list_documents() == []
    assert _try(client, url, PDF_PASSWORD).status_code == 404   # 檔案已經刪了,密碼對也沒有東西可開


def test_not_now_deletes_the_waiting_file(cfg, client, store, flash_text):
    url = _locked(client)

    r = _try(client, url, "", decision="cancel")

    assert r.status_code == 303
    assert flash_text(client.get(r.headers["location"]).text) == "已取消上傳"
    assert _pending(cfg) == [] and store.list_documents() == []
    assert client.get(url).status_code == 404


def test_waiting_file_expires_after_30_minutes(cfg, client):
    url = _locked(client)
    meta = next((cfg.paths.uploads_path / "pending").glob("*.json"))
    info = json.loads(meta.read_text(encoding="utf-8"))
    info["created"] -= 30 * 60 + 1
    meta.write_text(json.dumps(info), encoding="utf-8")

    page = client.get(url)

    assert page.status_code == 404 and "重新上傳" in page.text
    assert _pending(cfg) == []
    assert _try(client, url, PDF_PASSWORD).status_code == 404


def test_old_waiting_files_are_swept_on_the_next_upload(cfg, client):
    _locked(client)
    for meta in (cfg.paths.uploads_path / "pending").glob("*.json"):
        info = json.loads(meta.read_text(encoding="utf-8"))
        info["created"] = time.time() - 31 * 60
        meta.write_text(json.dumps(info), encoding="utf-8")

    _upload(client, plain_pdf())

    assert _pending(cfg) == []


@pytest.mark.parametrize("token", ["x", "a" * 31, "a" * 33, "..%2F..%2Fsecret", "a" * 28 + "%2F.."])
def test_unknown_or_malformed_token_is_404(client, token):
    assert client.get(f"/upload/password/{token}").status_code == 404
    assert _try(client, f"/upload/password/{token}", PDF_PASSWORD).status_code == 404


# ---- 安全底線 -------------------------------------------------------------------

def test_password_form_needs_the_csrf_token(app, client, cfg):
    url = _locked(client)
    bare = TestClient(app)
    assert bare.post(url, data={"password": PDF_PASSWORD}, follow_redirects=False).status_code == 403
    assert len(_pending(cfg)) == 2


def test_password_is_not_stored_logged_or_shown(cfg, client, caplog):
    url = _locked(client)
    with caplog.at_level(logging.DEBUG):
        wrong = _try(client, url, "wrong-" + PDF_PASSWORD)
        meta_text = next((cfg.paths.uploads_path / "pending").glob("*.json")).read_text(encoding="utf-8")
        done = _try(client, url, PDF_PASSWORD)
        result_page = client.get(done.headers["location"]).text

    assert PDF_PASSWORD not in meta_text                                   # 等待期間的資訊檔
    assert PDF_PASSWORD not in wrong.text and PDF_PASSWORD not in result_page
    assert all(PDF_PASSWORD not in record.getMessage() for record in caplog.records)
    secret = PDF_PASSWORD.encode()
    kept = [p for p in cfg.paths.logs.rglob("*") if p.is_file()] + [p for folder in (
        cfg.paths.archive, cfg.paths.review, cfg.paths.failed, cfg.paths.uploads_path) for p in folder.rglob("*")
        if p.is_file()]
    assert kept and all(secret not in p.read_bytes() for p in kept)        # 資料庫、處理紀錄、原件旁的檔都沒有


def test_delete_all_also_removes_waiting_files(cfg, client):
    _locked(client)
    assert len(_pending(cfg)) == 2

    r = client.post("/settings/delete", data={CONFIRM_PURGE_FIELD: "1"}, follow_redirects=False)

    assert r.status_code == 303 and _pending(cfg) == []
