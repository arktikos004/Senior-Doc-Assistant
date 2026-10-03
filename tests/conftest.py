"""共用的測試工具(網頁測試 tests/test_web*.py 用;資料夾全部隔離在 tmp_path,不連網、不呼叫模型)。

cfg            mock 模式的設定
store          測試自己讀寫的資料庫(和 app 同一個 tmp 檔)
analyzer       MockAnalyzer;要換辨識結果的測試檔覆寫這個 fixture,client 就會用它
app            create_app(cfg, analyzer=analyzer)
client         像瀏覽器一樣送表單的 FormClient:每個 POST 自動帶上 CSRF token(W2-B)
form_client    FormClient 類別,要自己組 app 時用:form_client(create_app(cfg, analyzer=...))
add_doc        add_doc(result=None, *, action="archive", target=None, reason="") → 文件 ID
action_status  action_status(store, action_id) → 行動目前的狀態
png_bytes      png_bytes(pad=0) → 一張 8×8 的白色 PNG;pad 在尾端補零,湊檔案大小用
review_doc     review_doc(result=None, *, name=..., content=None) → 文件 ID:待複核文件(原檔在 review/,
               資料庫記成 review;待複核清單讀 SQLite,不看資料夾)
flash_text     flash_text(html) → 頁面上方狀態訊息(?msg=)的文字,沒有訊息就是 None
要測「沒帶 token / token 不符」就直接用 TestClient(app),或在 data 裡自己放 csrf_token。
合成樣本(帳單、公文、發票、期限提醒)放在 tests/samples.py。
"""
import io
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.config import AppConfig, PathsConfig
from src.providers import MockAnalyzer
from src.store import Store
from web.app import CSRF_FIELD, create_app

# 樣板 {{ csrf_input() }} 產生的隱藏欄位
_TOKEN_FIELD = re.compile(rf'name="{CSRF_FIELD}" value="([^"]+)"')
# base.html 的狀態訊息;頁面上還有朗讀、上傳的 role="status",只找 role="status" 永遠找得到
_FLASH = re.compile(r'<p class="msg" role="status">(.*?)</p>', re.S)
_TAGS = re.compile(r"<[^>]+>")


class FormClient(TestClient):
    """瀏覽器的流程是「開頁面 → 拿到 cookie 與表單裡的 token → 按送出」;這裡每次 POST 前先開首頁拿 token。

    data 裡已經有 csrf_token 就照用(測 token 不符時用)。
    """

    def post(self, url, *, data=None, **kwargs):
        data = dict(data or {})
        if CSRF_FIELD not in data:
            data[CSRF_FIELD] = self.csrf_token()
        return super().post(url, data=data, **kwargs)

    def csrf_token(self) -> str:
        found = _TOKEN_FIELD.search(self.get("/").text)
        assert found, "首頁的上傳表單沒有 CSRF 欄位"
        return found.group(1)


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    paths = PathsConfig(inbox=tmp_path / "inbox", archive=tmp_path / "archive", review=tmp_path / "review",
                        failed=tmp_path / "failed", logs=tmp_path / "logs")
    c = AppConfig(paths=paths, provider="mock")
    c.ensure_dirs()
    return c


@pytest.fixture
def store(cfg) -> Store:
    return Store(cfg.paths.db_path)


@pytest.fixture
def analyzer(cfg):
    return MockAnalyzer(cfg)


@pytest.fixture
def app(cfg, analyzer):
    return create_app(cfg, analyzer=analyzer)


@pytest.fixture
def client(app) -> FormClient:
    return FormClient(app)


@pytest.fixture
def form_client():
    """回傳 FormClient 類別:form_client(app) 建立一個會自動帶 CSRF token 的 client。"""
    return FormClient


@pytest.fixture
def add_doc(store):
    """回傳 add_doc(result=None, *, action="archive", target=None, reason=""):把一份文件直接寫進資料庫。"""
    def add(result=None, *, action="archive", target=None, reason=""):
        return store.add_document({
            "原始檔案": "20260930-101500-abcd1234.png", "動作": action, "原因": reason,
            "目標路徑": str(target) if target else None, "AI辨識結果": result, "錯誤": None,
        })
    return add


def _action_status(store: Store, action_id: int) -> str:
    return next(a["status"] for a in store.list_actions() if a["id"] == action_id)


@pytest.fixture
def action_status():
    """回傳 action_status(store, action_id):行動目前的狀態。"""
    return _action_status


def _png_bytes(pad: int = 0) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, "PNG")
    return buf.getvalue() + b"\0" * pad


@pytest.fixture
def png_bytes():
    """回傳 png_bytes(pad=0):一張真的 PNG(檔頭比對得過)。"""
    return _png_bytes


@pytest.fixture
def review_doc(cfg, add_doc):
    """回傳 review_doc(result=None, *, name=..., content=None) → 文件 ID:原檔放在 review/,資料庫記成待複核。"""
    def add(result: dict | None = None, *, name: str = "20261001-101500-abcd1234.png",
            content: bytes | None = None) -> int:
        path = cfg.paths.review / name
        path.write_bytes(content if content is not None else _png_bytes())
        return add_doc(result if result is not None else {"doc_type": "發票", "confidence": 0.5},
                       action="review", target=path, reason="驗證信心 0.50 低於門檻 0.80,需人工確認")
    return add


def _flash_text(html: str) -> str | None:
    found = _FLASH.search(html)
    return _TAGS.sub("", found.group(1)).strip() if found else None


@pytest.fixture
def flash_text():
    """回傳 flash_text(html):只取 <p class="msg" role="status"> 的文字(圖示去掉),要比對訊息本身。"""
    return _flash_text
