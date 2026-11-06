"""重複上傳(只提示、不自動刪):原件一模一樣,或兩份發票的號碼都經 QR Code 核對相符而且一樣。

合成資料;不連網、不呼叫真模型(MockAnalyzer),檔案都在 tmp_path。
"""
import hashlib
import io
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import FormClient
from src.duplicates import SAME_FILE, SAME_INVOICE, file_sha256, find_duplicate, verified_invoice_number
from src.pipeline import Pipeline
from src.providers import MockAnalyzer
from src.store import Store
from web.access import Identity
from web.app import create_app

NOTE = "這張可能已經上傳過"
QR_PASS = {"QR 字軌號碼": {"status": "pass", "detail": "QR 記載發票號碼 AB12345678，與辨識結果相符",
                          "fields": ["invoice_number"]}}


def _png(color="white") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "PNG")
    return buf.getvalue()


def _upload(client, content: bytes) -> int:
    r = client.post("/upload", files={"file": ("photo.png", content, "image/png")}, follow_redirects=False)
    assert r.status_code == 303
    return int(re.fullmatch(r"/doc/(\d+)", r.headers["location"]).group(1))


def _invoice(number="AB-12345678", verification=None):
    return {"doc_type": "發票", "date": "2026-10-10", "vendor": "合成示範超市", "amount": 244,
            "invoice_number": number, "confidence": 0.9,
            "verification": QR_PASS if verification is None else verification}


# ---- 判斷規則(純函式) ----

def test_only_qr_verified_invoice_numbers_count():
    assert verified_invoice_number(_invoice("ab-1234 5678")) == "AB12345678"     # 連字號、空白、大小寫一律正規化
    fail = {"QR 字軌號碼": {"status": "fail", "detail": "QR 記載…", "fields": ["invoice_number"]}}
    assert verified_invoice_number(_invoice(verification=fail)) is None
    assert verified_invoice_number(_invoice(verification={})) is None            # 讀不到 QR:只有 AI 讀值
    assert verified_invoice_number({**_invoice(), "doc_type": "收據"}) is None
    assert verified_invoice_number(None) is None


def test_find_duplicate_picks_the_earliest_other_copy():
    def doc(i, action="archive", result=None):
        return {"id": i, "created_at": f"2026-10-{10 + i:02d}T09:00:00", "action": action, "result": result}

    same = [doc(1), doc(3), doc(5)]
    assert find_duplicate(doc(3), same, []) == {"id": 1, "created_at": "2026-10-11T09:00:00",
                                                "reason": SAME_FILE, "earlier": True}
    assert find_duplicate(doc(1), same, [])["id"] == 3                           # 最早那份:連到緊接在後的
    assert find_duplicate(doc(1), same, [])["earlier"] is False
    assert find_duplicate(doc(1), [doc(1)], []) is None                         # 只有自己
    assert find_duplicate(doc(2), [doc(1, action="failed"), doc(2)], []) is None  # 讀不出來的不比
    assert find_duplicate(doc(2, action="failed"), same, []) is None


def test_same_invoice_number_needs_both_sides_verified():
    first = {"id": 1, "created_at": "2026-10-20T09:00:00", "action": "archive", "result": _invoice()}
    second = {"id": 2, "created_at": "2026-10-28T09:00:00", "action": "review", "result": _invoice("AB12345678")}
    unverified = {"id": 3, "created_at": "2026-10-28T10:00:00", "action": "archive",
                  "result": _invoice(verification={})}
    assert find_duplicate(second, [], [first, second, unverified])["reason"] == SAME_INVOICE
    assert find_duplicate(unverified, [], [first, second, unverified]) is None
    other = {"id": 4, "created_at": "2026-10-28T11:00:00", "action": "archive", "result": _invoice("AB12345679")}
    assert find_duplicate(other, [], [first, second, other]) is None
    # 同一份兩個理由都成立時寫「檔案一模一樣」
    assert find_duplicate(second, [first, second], [first, second])["reason"] == SAME_FILE


# ---- 原件雜湊:上傳時算、舊文件啟動時補 ----

def test_pipeline_records_the_hash_of_the_original(cfg, store):
    photo = cfg.paths.inbox / "photo.png"
    photo.write_bytes(_png())
    expected = hashlib.sha256(photo.read_bytes()).hexdigest()
    record = Pipeline(cfg, MockAnalyzer(cfg), store=store).process_file(photo)
    assert record["檔案雜湊"] == expected
    assert store.get_document(record["文件ID"])["file_sha256"] == expected


def test_old_documents_get_a_hash_when_the_site_starts(cfg, store, add_doc):
    original = cfg.paths.archive / "20261001-old.png"
    original.write_bytes(_png("black"))
    old = add_doc({"doc_type": "收據", "confidence": 0.9}, target=original)
    gone = add_doc({"doc_type": "收據", "confidence": 0.9}, target=cfg.paths.archive / "missing.png")
    kept = store.add_document({"原始檔案": "x.png", "動作": "archive", "目標路徑": str(original),
                               "AI辨識結果": {"doc_type": "收據"}, "檔案雜湊": "f" * 64})
    app = create_app(cfg, analyzer=MockAnalyzer(cfg))
    assert store.get_document(old)["file_sha256"] is None                       # 只建 app 還不補
    with TestClient(app):                                                        # 網頁啟動時補
        pass
    assert store.get_document(old)["file_sha256"] == file_sha256(original)
    assert store.get_document(gone)["file_sha256"] is None                      # 原件不在:跳過,下次再試
    assert store.get_document(kept)["file_sha256"] == "f" * 64                  # 上傳時算好的不覆寫


def test_old_database_gets_the_hash_column(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
                     "source_file TEXT NOT NULL, action TEXT NOT NULL, reason TEXT, target_path TEXT, doc_type TEXT, "
                     "date TEXT, vendor TEXT, amount REAL, currency TEXT, result_json TEXT, origin TEXT, error TEXT)")
    Store(db)
    with sqlite3.connect(db) as conn:
        assert "file_sha256" in {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
        assert "idx_documents_sha" in {r[1] for r in conn.execute("PRAGMA index_list(documents)")}


# ---- 結果頁 ----

def test_same_photo_uploaded_twice_points_to_the_other_copy(client, store):
    first = _upload(client, _png())
    second = _upload(client, _png())
    assert store.get_document(first)["file_sha256"] == store.get_document(second)["file_sha256"]

    later = client.get(f"/doc/{second}").text
    assert NOTE in later and "上傳的那一份，上傳的檔案一模一樣。" in later
    assert f'href="/doc/{first}"' in later and "看之前那一份" in later
    earlier = client.get(f"/doc/{first}").text
    assert "又上傳了一份" in earlier and f'href="/doc/{second}"' in earlier and "看之後那一份" in earlier
    assert store.count_documents() == 2                                         # 不會自動刪


def test_different_photos_have_no_note(client):
    first = _upload(client, _png("white"))
    second = _upload(client, _png("black"))
    assert NOTE not in client.get(f"/doc/{first}").text
    assert NOTE not in client.get(f"/doc/{second}").text


def test_same_qr_verified_invoice_number_shows_the_note(client, add_doc):
    first = add_doc(_invoice("AB-12345678"))
    second = add_doc(_invoice("AB12345678"), action="review", reason="驗證信心 0.50 低於門檻 0.80，需人工確認")
    html = client.get(f"/doc/{second}").text
    assert NOTE in html and "發票號碼一樣（兩份都經過 QR Code 核對）" in html and f'href="/doc/{first}"' in html


def test_unverified_invoice_numbers_are_not_compared(client, add_doc):
    first = add_doc(_invoice())
    second = add_doc(_invoice(verification={}))                                 # 讀到同一個號碼,但沒經 QR 核對
    assert NOTE not in client.get(f"/doc/{first}").text
    assert NOTE not in client.get(f"/doc/{second}").text


class _As:
    def __init__(self, email):
        self.email = email

    def resolve(self, request):
        return Identity(self.email, False)


@pytest.mark.parametrize("role", ["elder", None])
def test_elders_and_viewers_see_the_note_without_a_delete_button(cfg, store, role):
    family = FormClient(create_app(cfg, analyzer=MockAnalyzer(cfg)))
    first, second = _upload(family, _png()), _upload(family, _png())
    email = "someone@example.test"
    if role:
        store.set_member(email, role)
        store.set_setting(f"consent:{email}", "2026-10-28T08:00:00", actor=email)
    html = TestClient(create_app(cfg, analyzer=MockAnalyzer(cfg), identity_resolver=_As(email))).get(
        f"/doc/{second}").text
    assert NOTE in html and f'href="/doc/{first}"' in html
    assert f'action="/doc/{second}/delete"' not in html
