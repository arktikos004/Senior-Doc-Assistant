"""看有(高齡家庭文書輔助)網頁介面(FastAPI)。

啟動(專案根目錄):
    uvicorn web.app:app --reload
    # 或   python web/app.py

頁面:
    /                    拍照或上傳文件(先選大類,再選這一類的哪一種)+ 最近看過的文件
    POST /upload         存檔(重產檔名)→ Pipeline(敏感大類與初賽不能自動判讀的文件只在本機辨識)→ 303 到結果頁
    /cabinet             文件櫃:四大類的份數與文件;?cat= 只看一類
    /doc/{id}            白話解說、朗讀、核對印章、行動、欄位、原件
    /doc/{id}/file       原件(只送 archive/review/failed/uploads 內的檔)
    /doc/{id}/correct    更正讀值(F7)與指定大類:GET 表單;POST 存檔 → 重新核對、重算行動 → 303 回結果頁
    POST /doc/{id}/reject  待複核的文件退回(品質不足)→ 303 回待複核
    POST /reminder/{id}  取消或恢復自動列入的期限提醒(F8)→ 303 回結果頁
    /confirm            家人確認:只列 tier=confirm 且 pending 的行動
    /review              待複核清單(讀 SQLite,每份連到更正頁)
    /settings            系統狀態、這台裝置的偏好、全家共用的設定(POST 存檔 → 303 回設定頁)、資料管理
    /settings/export     匯出全部紀錄(JSON 下載,不含影像)
    POST /settings/delete  刪除全部資料(文件、行動、更正與原件;設定保留)→ 303 回首頁
    /healthz             健康檢查(JSON),給開機腳本與監看用

安全範圍:本機只綁 127.0.0.1;對外上線時由 Cloudflare Tunnel + Access 負責登入
(見 docs/部署指南.md)。檔名一律不信任使用者輸入,樣板 autoescape;
所有回應帶安全標頭,除了 PDF 原件都帶 CSP(樣板沒有行內 JavaScript;瀏覽器內建的 PDF 閱讀器套上 CSP
會顯示不出來),所有 POST 都檢查 CSRF token(W2-B)。
"""
from __future__ import annotations

import json
import re
import secrets
import sys
from contextvars import ContextVar
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from markupsafe import Markup
from PIL import Image
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException as StarletteHTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import AppConfig, load_config  # noqa: E402
from src.models import CATEGORIES  # noqa: E402
from src.pipeline import Pipeline  # noqa: E402
from src.providers import create_analyzer  # noqa: E402
from src.models import ExtractionResult  # noqa: E402
from src.record_log import clear_records  # noqa: E402
from src.review import correct_document, correctable, reading_unchanged, reject_document  # noqa: E402
from src.settings import (  # noqa: E402
    PROVIDER,
    PURGED,
    THRESHOLD,
    cloud_ready,
    delete_data_files,
    effective_config,
    export_records,
    form_changes,
)
from src.store import Store  # noqa: E402
from web.render import (  # noqa: E402
    ASSIGNABLE_CATEGORIES,
    BRAND_FULL,
    KINDS_LEGEND,
    REMINDER_CANCEL,
    REMINDER_RESTORE,
    STATIC_DIR,
    UNSURE,
    action_view,
    cabinet_view,
    category_choices,
    correct_view,
    doc_row,
    doc_title,
    doc_view,
    fmt_when,
    is_auto_reminder,
    is_pdf,
    kind_choice,
    kind_choices,
    parse_correction,
    reminder_rows,
    render,
    review_row,
    settings_view,
    trust_notes,
)
from web.render import env as jinja_env  # noqa: E402

# 上傳大小上限:手機照片通常 2–6MB,15MB 足夠又能擋掉灌爆硬碟的請求
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
# multipart 表頭與其他欄位的額外位元組;Content-Length 超過「上限 + 這個值」就不必讀內容
_MULTIPART_SLACK = 64 * 1024
_CHUNK = 1024 * 1024

# 操作完成後轉址帶回的狀態訊息
MSG_CORRECTED = "已更正"
MSG_REJECTED = "已退回"
MSG_CONFIRMED = "已確認"
MSG_REMINDER_OFF = "已取消提醒"
MSG_REMINDER_ON = "已恢復提醒"
MSG_CATEGORY = "已更新類別"
MSG_SETTINGS_SAVED = "已儲存設定"
MSG_SETTINGS_UNCHANGED = "設定沒有變動"
MSG_PURGED = "已刪除全部資料"
MSG_PURGED_PARTLY = "資料已刪除,但有些照片檔刪不掉,請管理者檢查資料夾。"
# 只有這些狀態訊息可以經由 ?msg= 顯示;任意字串不回顯,避免被拿來偽造系統公告
_MESSAGES = frozenset({MSG_CORRECTED, MSG_REJECTED, MSG_CONFIRMED, MSG_REMINDER_OFF, MSG_REMINDER_ON,
                       MSG_CATEGORY, MSG_SETTINGS_SAVED, MSG_SETTINGS_UNCHANGED, MSG_PURGED, MSG_PURGED_PARTLY})
# 家人確認:表單值 → (行動狀態, 回到家人確認頁時的訊息)
_CONFIRM_DECISIONS = {"done": ("done", MSG_CONFIRMED), "rejected": ("rejected", MSG_REJECTED)}
# 取消/恢復提醒:表單值 → (行動狀態, 回到結果頁時的訊息)
_REMINDER_DECISIONS = {REMINDER_CANCEL: ("rejected", MSG_REMINDER_OFF), REMINDER_RESTORE: ("pending", MSG_REMINDER_ON)}

MSG_NO_FILE = "請先拍照,或選一個檔案。"
MSG_EMPTY = "這個檔案是空的,請重新拍一張。"
MSG_BAD_EXT = "只能上傳照片(JPG、PNG、WEBP、BMP、TIFF)或 PDF。"
MSG_MISMATCH = "檔案內容和副檔名對不上,請直接用相機拍一張,或選原始的照片檔。"
MSG_TOO_LARGE = "檔案超過 15MB。請改拍一張,或選較小的檔案。"

# 框架預設的英文錯誤訊息換成中文
_DEFAULT_ERRORS = {404: "找不到這個頁面", 405: "這個網址不能這樣使用"}
MSG_INVALID = "送出的資料不完整"        # 網址或表單欄位的格式不對(FastAPI 預設回英文 JSON)
MSG_SERVER_ERROR = "系統出了點問題"     # 沒預期到的例外;頁面不寫例外內容
# 原件可能是藥袋等特種個資:不讓瀏覽器或中間的代理快取(nosniff 等安全標頭由 security_headers 統一加)
_FILE_HEADERS = {"Cache-Control": "private, no-store"}

# 錯誤頁的下一步(說明, 按鈕網址, 按鈕文字),依狀態碼的預設:填錯的回上一頁改,已處理過的回家人確認,
# 其他(含沒帶 CSRF token 的 403)回首頁。個別錯誤要不同的下一步,改丟 PageError
_BACK_HINT = "請按瀏覽器的「上一頁」改好後再送出一次。"
_HOME_STEP = ("可以回首頁重新開始。", "/", "回首頁")
_NEXT_STEPS = {
    400: (_BACK_HINT, "/", "回首頁"),
    403: ("為了安全,系統沒有處理這次的操作(可能是頁面開太久了)。請回首頁重新操作一次。", "/", "回首頁"),
    409: ("這件事可能已經有人處理了,請回「家人確認」看最新狀態。", "/confirm", "回家人確認"),
    422: (_BACK_HINT, "/", "回首頁"),
}
# 按鈕圖示跟著目的地走,和導覽列同一組
_NEXT_ICONS = {"/": "home", "/confirm": "family", "/review": "tray"}


class PageError(HTTPException):
    """給長輩看的錯誤:除了訊息,也帶下一步(說明、按鈕連到哪、按鈕文字)。

    沒指定的部分沿用狀態碼的預設(_NEXT_STEPS),所以一般的 HTTPException 也有合適的下一步。
    """

    def __init__(self, status_code: int, detail: str, *, next_url: str | None = None,
                 next_label: str | None = None, hint: str | None = None) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.next_url, self.next_label, self.hint = next_url, next_label, hint


def _next_step(status: int, exc: Exception | None = None) -> dict[str, str]:
    """錯誤頁的下一步:PageError 指定的優先,其餘依狀態碼。"""
    hint, url, label = _NEXT_STEPS.get(status, _HOME_STEP)
    if isinstance(exc, PageError):
        hint, url, label = exc.hint or hint, exc.next_url or url, exc.next_label or label
    return {"hint": hint, "next_url": url, "next_label": label, "next_icon": _NEXT_ICONS.get(url, "chevron-right")}


# ---- 上線防護(W2-B) --------------------------------------------------------
# 每個回應都帶的安全標頭。CSP 只准用本站的檔案(上傳前的預覽縮圖是 blob:),也不准被別的網站嵌進框架;
# 樣板沒有行內 script 與 style 屬性,所以不需要 'unsafe-inline'
_CSP = "; ".join((
    "default-src 'self'",
    "img-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
))
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    # 只留相機(拍照上傳);麥克風、定位、付款等用不到的一律關掉
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=(), payment=(), usb=()",
}

# CSRF:每個瀏覽器一個隨機 token,存在 cookie(HttpOnly),樣板用 {{ csrf_input() }} 把同一個值放進表單;
# 會改資料的請求兩邊要相符(double-submit)。別的網站能讓瀏覽器送出表單,但讀不到本站的 cookie,填不出 token
CSRF_FIELD = "csrf_token"
CSRF_COOKIE = "csrf_token"               # 本機 http(開發、展示伺服器)
CSRF_COOKIE_HTTPS = "__Host-csrf_token"  # 經 Tunnel 的 https:__Host- 開頭的 cookie,同網域的其他子網域改不了
MSG_CSRF = "這次的操作沒有送出"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")    # secrets.token_urlsafe(32) 的格式
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# 這個請求的 token:middleware 設定、樣板的 csrf_input() 讀取;每個請求有自己的 context,互不干擾
_csrf_token: ContextVar[str] = ContextVar("csrf_token", default="")

# 部署新版時改成當天日期;/healthz 會回報,用來確認 GPU 電腦上跑的是哪一版
APP_VERSION = "2026.10.02"
_OLLAMA_PROBE_TIMEOUT = 2.0   # 秒;健康檢查不能被卡住的 Ollama 拖住

# 誰改的設定:Cloudflare Access 登入後轉過來的 email 標頭。只用來記錄,不當授權(本機直接開沒有這個標頭);
# 不像 email 的值(太長、有空白或控制字元)當作沒有
_ACTOR_HEADER = "Cf-Access-Authenticated-User-Email"
_EMAIL = re.compile(r"[^@\s\x00-\x1f\x7f]{1,64}@[^@\s\x00-\x1f\x7f]{1,189}")


def _actor(request: Request) -> str | None:
    value = request.headers.get(_ACTOR_HEADER, "").strip()
    return value if _EMAIL.fullmatch(value) else None


def _looks_like(ext: str, head: bytes) -> bool:
    """用檔頭確認內容真的是該格式,擋掉改副檔名的 HTML/程式檔。未知副檔名不擋。"""
    if ext in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext == ".bmp":
        return head.startswith(b"BM")
    if ext in (".tif", ".tiff"):
        return head[:4] in (b"II*\x00", b"MM\x00*")
    if ext == ".pdf":
        return b"%PDF-" in head[:1024]
    return True


# EXIF 方向 5–8:照片轉了 90 度,瀏覽器顯示時寬高對調
_ROTATED = {5, 6, 7, 8}


def _image_size(path: Path | None) -> tuple[int, int] | None:
    """原件顯示時的寬高(只讀檔頭),讓 <img> 先佔好位置,延遲載入時版面不跳;PDF 或讀不到就不給。"""
    if path is None or is_pdf(path):
        return None
    try:
        with Image.open(path) as im:
            width, height = im.size
            if im.getexif().get(0x0112) in _ROTATED:
                width, height = height, width
            return width, height
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None


def _new_upload_name(ext: str) -> str:
    """時間戳 + 隨機碼;完全不使用使用者提供的檔名。"""
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(4)}{ext}"


def _redirect_with_msg(path: str, msg: str) -> RedirectResponse:
    """POST 做完就 303 轉址並帶狀態訊息(重新整理不會重送表單)。訊息不在白名單內,頁面不會顯示,所以直接擋下。"""
    assert msg in _MESSAGES, msg
    return RedirectResponse(url=f"{path}?msg={quote(msg)}", status_code=303)


# ---- CSRF(W2-B) -------------------------------------------------------------

def csrf_input() -> Markup:
    """樣板用 {{ csrf_input() }}:帶這個瀏覽器 token 的隱藏欄位。每個 POST 表單都要放這一行。"""
    return Markup('<input type="hidden" name="{}" value="{}">').format(CSRF_FIELD, _csrf_token.get())


# 登記成 Jinja 全域。放在這裡而不是 web/render.py:token 由本模組的 middleware 產生,render 不能反過來 import app
jinja_env.globals.update(csrf_input=csrf_input)


def _csrf_cookie(request: Request) -> tuple[str, bool]:
    """(cookie 名稱, 要不要 Secure)。只有 https 能用 __Host- 名稱與 Secure,本機 http 的瀏覽器不收。"""
    secure = request.url.scheme == "https"
    return (CSRF_COOKIE_HTTPS if secure else CSRF_COOKIE), secure


async def check_csrf(request: Request) -> None:
    """整個 app 共用的 dependency:會改資料的請求沒帶 token、或和 cookie 不符,一律 403(新路由自動涵蓋)。

    FastAPI 呼叫 dependency 前已經解析好表單,這裡的 request.form() 拿到同一份,不會再讀一次 body;
    上傳超過大小的 413 由 limit_upload_size 在讀 body 之前就回,不受影響。
    """
    if request.method in _SAFE_METHODS:
        return
    cookie = request.cookies.get(_csrf_cookie(request)[0], "")
    sent = (await request.form()).get(CSRF_FIELD)
    if not (_TOKEN_RE.fullmatch(cookie) and isinstance(sent, str)
            and secrets.compare_digest(sent.encode(), cookie.encode())):
        raise HTTPException(status_code=403, detail=MSG_CSRF)


async def read_form(request: Request) -> FormData:
    """整份表單(更正頁的欄位依文件類型而定,藥品時段是多選,沒辦法逐一宣告成 Form 參數)。

    check_csrf 已經解析過,這裡拿到同一份;路由本身仍是一般函式,核對(可能要解 QR)在執行緒池裡跑。
    """
    return await request.form()


# ---- 健康檢查(W2-B) ----------------------------------------------------------

def _model_names(cfg: AppConfig) -> dict[str, str]:
    """實際會用到的模型。雲端模式下藥袋與未指定類型仍在本機辨識,所以兩個都列。"""
    if cfg.provider == "ollama":
        return {"local": cfg.ollama.model}
    if cfg.provider == "workers_ai":
        return {"cloud": cfg.workers_ai.model, "local": cfg.ollama.model}
    return {}


def _ollama_status(cfg: AppConfig, transport: httpx.BaseTransport | None = None) -> str:
    """reachable / unreachable / unused(mock 模式不連)。Ollama 在本機,不走系統代理。"""
    if cfg.provider == "mock":
        return "unused"
    try:
        with httpx.Client(transport=transport, timeout=_OLLAMA_PROBE_TIMEOUT, trust_env=False) as client:
            response = client.get(f"{cfg.ollama.host.rstrip('/')}/api/version")
    except (httpx.HTTPError, httpx.InvalidURL):
        return "unreachable"
    return "reachable" if response.status_code == 200 else "unreachable"


def create_app(cfg: AppConfig | None = None, analyzer=None,
               ollama_transport: httpx.BaseTransport | None = None) -> FastAPI:
    """建立 app;測試可注入隔離在 tmp 的設定與 MockAnalyzer。

    analyzer 預設依目前生效的設定(current_cfg)建立,在第一次上傳時才建立,
    這樣只開複核頁或匯入模組時不會去連模型;設定頁切換辨識模式後,下一次上傳會重建。
    ollama_transport 是 /healthz 檢查 Ollama 用的 httpx transport;測試注入 MockTransport,不連網。
    """
    cfg = cfg or load_config()
    cfg.ensure_dirs()
    # 不開 API 文件頁(/docs 等):它從 CDN 載入,違反 CSP 與離線原則,對外也不需要公開 API 結構。
    # 每個路由都先過 check_csrf(只檢查會改資料的請求),之後新增的表單路由不必各自記得
    app = FastAPI(title=BRAND_FULL, docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(check_csrf)])
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # 外層的 cfg 是 config.yaml 的設定;state["cfg"] 是最近一次讀到的生效設定(錯誤頁用,不再讀資料庫)
    state: dict[str, Any] = {"analyzer": None, "analyzer_for": None, "store": None, "cfg": cfg}

    def store() -> Store:
        if state["store"] is None:
            state["store"] = Store(cfg.paths.db_path)
        return state["store"]

    def current_cfg() -> AppConfig:
        """目前生效的設定:config.yaml 再套上設定頁存的值(每次讀 SQLite,設定一存就生效)。

        辨識模式、門檻、本機限定的類型看這裡;資料夾位置與格式設定頁改不到,直接用 cfg。
        """
        state["cfg"] = effective_config(cfg, store())
        return state["cfg"]

    def get_analyzer():
        """上傳用的 analyzer:測試注入的一律沿用;否則依目前的辨識模式建立,模式改了就重建。"""
        if analyzer is not None:
            return analyzer
        current = current_cfg()
        built_for = (current.provider, current.local_only_doc_types)
        if state["analyzer"] is None or state["analyzer_for"] != built_for:
            state["analyzer"], state["analyzer_for"] = create_analyzer(current), built_for
        return state["analyzer"]

    def trust_for(current: AppConfig) -> list[tuple[str, str]]:
        """每頁固定的安心說明:隱私那句依目前的辨識模式寫(本機/雲端分流/展示)。"""
        return trust_notes(current.provider, current.local_only_doc_types)

    def pending_confirms() -> list[dict[str, Any]]:
        return [a for a in store().list_actions(status="pending") if a["tier"] == "confirm"]

    def page(template: str, active: str = "", status_code: int = 200, **ctx: Any) -> HTMLResponse:
        """所有頁面共用:導覽列的待辦數量、只允許白名單內的狀態訊息。"""
        msg = ctx.pop("msg", None)
        nav = {"confirm": len(pending_confirms()), "review": store().count_documents(action="review")}
        html = render(template, nav=nav, active=active, trust=trust_for(current_cfg()),
                      msg=msg if msg in _MESSAGES else None, **ctx)
        return HTMLResponse(html, status_code=status_code)

    def cloud_mode() -> bool:
        """雲端模式(依設定頁目前生效的設定):畫面上才需要說明哪些文件只在本機處理;本機模式所有文件都在這台電腦辨識。"""
        return current_cfg().provider == "workers_ai"

    def home(status_code: int = 200, error: str | None = None, selected: str = UNSURE,
             category: str = UNSURE, msg=None):
        docs = store().list_documents(limit=30)
        recent = [doc_row(d) for d in docs[:8]]
        reminders = reminder_rows(docs, store().list_actions(), date.today())
        group = category if category in CATEGORIES else None
        return page(
            "home.html", active="home", status_code=status_code, msg=msg,
            # 重畫時(例如沒選檔)留住剛才的選擇;第二列只留伺服器收得下的值,和上傳時同一份白名單
            error=error, selected_type=kind_choice(group, selected) or UNSURE, selected_category=group or UNSURE,
            category_choices=category_choices(), kind_choices=kind_choices(), kinds_legend=KINDS_LEGEND,
            recent=recent, reminders=reminders, max_bytes=MAX_UPLOAD_BYTES, local_only=cloud_mode(),
        )

    def allowed_file(doc: dict[str, Any]) -> Path | None:
        """target_path 必須落在 archive/review/failed/uploads 內、是支援的格式、且存在。

        resolve() 會展開 ../ 與符號連結,所以指到資料夾外的連結也會被擋下。
        """
        target = doc.get("target_path")
        if not target:
            return None
        try:
            path = Path(target).resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        roots = [p.resolve() for p in (cfg.paths.archive, cfg.paths.review,
                                       cfg.paths.failed, cfg.paths.uploads_path)]
        if not any(path != root and path.is_relative_to(root) for root in roots):
            return None
        if path.suffix.lower() not in cfg.supported_extensions or not path.is_file():
            return None
        return path

    def get_doc(doc_id: int) -> dict[str, Any]:
        doc = store().get_document(doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="找不到這份文件")
        return doc

    def get_correctable(doc_id: int) -> dict[str, Any]:
        doc = get_doc(doc_id)
        if not correctable(doc):
            raise PageError(409, "這份文件沒有可以更正的讀值", hint="讀不出來的文件沒辦法更正,請回首頁重新拍一張。",
                            next_url="/", next_label="回首頁")
        return doc

    def correct_page(doc: dict[str, Any], values: dict[str, Any] | None = None,
                     errors: dict[str, str] | None = None, *, status_code: int = 200,
                     add_item: bool = False) -> HTMLResponse:
        path = allowed_file(doc)
        v = correct_view(doc, values, errors, file_url=f"/doc/{doc['id']}/file" if path else None,
                         file_size=_image_size(path), add_item=add_item)
        return page("correct.html", active="review" if doc.get("action") == "review" else "",
                    status_code=status_code, v=v)

    def get_action(action_id: int) -> dict[str, Any]:
        for a in store().list_actions():
            if a["id"] == action_id:
                return a
        raise HTTPException(status_code=404, detail="找不到這個待辦事項")

    def settings_page(status_code: int = 200, msg: str | None = None, values: dict[str, Any] | None = None,
                      errors: dict[str, str] | None = None) -> HTMLResponse:
        """設定頁;存檔失敗時帶著剛剛的選擇(values)與中文說明(errors)重畫。"""
        current = current_cfg()
        v = settings_view(current, default_threshold=cfg.auto_threshold, models=_model_names(current),
                          ollama=_ollama_status(current, ollama_transport), version=APP_VERSION,
                          changes=store().list_setting_changes(), cloud_ready=cloud_ready(cfg),
                          values=values, errors=errors)
        return page("settings.html", active="settings", status_code=status_code, msg=msg, v=v)

    def error_page(status: int, detail: str, exc: Exception | None = None,
                   headers: dict[str, str] | None = None) -> HTMLResponse:
        # 安心說明用最近一次讀到的生效設定,不再讀資料庫(錯誤可能正出在資料庫)
        html = render("error.html", nav={"confirm": 0, "review": 0}, active="", trust=trust_for(state["cfg"]),
                      status=status, detail=detail, msg=None, **_next_step(status, exc))
        return HTMLResponse(html, status_code=status, headers=headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        """錯誤也用一般頁面呈現,長輩不會看到 JSON。"""
        detail = exc.detail if isinstance(exc.detail, str) else "發生錯誤"
        if detail.isascii():
            detail = _DEFAULT_ERRORS.get(exc.status_code, "發生錯誤")
        # 導覽列的待辦數給 0、不去讀:錯誤可能正出在資料庫或複核資料夾,錯誤頁再讀一次只會跟著壞
        return error_page(exc.status_code, detail, exc, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        """網址或表單欄位格式不對(例如 /doc/abc、沒按「確認」或「退回」就送出):同樣的中文錯誤頁。"""
        return error_page(422, MSG_INVALID)

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception):
        """沒預期到的例外:中文錯誤頁,不寫例外內容(伺服器記錄裡有)。

        Starlette 把這個 handler 放在所有 middleware 外層,回應不經過 security_headers,標頭要自己帶。
        """
        headers = {**_SECURITY_HEADERS, "Content-Security-Policy": _CSP, "Cache-Control": "no-store"}
        return error_page(500, MSG_SERVER_ERROR, headers=headers)

    @app.middleware("http")
    async def limit_upload_size(request: Request, call_next):
        """Content-Length 已經超過上限就直接 413,不必先把整個檔案收下來。"""
        if request.method == "POST" and request.url.path == "/upload":
            length = request.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_UPLOAD_BYTES + _MULTIPART_SLACK:
                return home(status_code=413, error=MSG_TOO_LARGE)
        return await call_next(request)

    # 後加的 middleware 包在外層:security_headers → issue_csrf_token → limit_upload_size → 路由。
    # 所以 413 重畫的首頁也帶得到 token,413、403、404 等錯誤頁也都有安全標頭

    @app.middleware("http")
    async def issue_csrf_token(request: Request, call_next):
        """每個瀏覽器一個 CSRF token:沿用 cookie 裡的;沒有或格式不對就換一個新的,隨 HTML 頁面寫進 cookie。"""
        name, secure = _csrf_cookie(request)
        token = request.cookies.get(name, "")
        fresh = not _TOKEN_RE.fullmatch(token)
        if fresh:
            token = secrets.token_urlsafe(32)
        _csrf_token.set(token)
        response = await call_next(request)
        if fresh and response.headers.get("content-type", "").startswith("text/html"):
            response.set_cookie(name, token, httponly=True, samesite="lax", secure=secure)
        return response

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        """所有回應加安全標頭;HTML 頁面含個資,不讓瀏覽器或中間的代理存下來。"""
        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        # PDF 原件交給瀏覽器內建的閱讀器顯示;它用行內樣式與 plugin,套上頁面的 CSP 會顯示不出來
        if not content_type.startswith("application/pdf"):
            response.headers.setdefault("Content-Security-Policy", _CSP)
        if content_type.startswith("text/html"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # ---- 健康檢查(部署用) --------------------------------------------------

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """給開機腳本與監看用:網頁活著就回 200;Ollama 連不上時照常回應,標成 degraded。

        只列狀態、版本、provider 與模型名稱;不列路徑、主機位址、金鑰或其他設定值。
        provider 是目前生效的(設定頁可以切換辨識模式)。
        """
        current = current_cfg()
        ollama = _ollama_status(current, ollama_transport)
        return JSONResponse({
            "status": "degraded" if ollama == "unreachable" else "ok",
            "version": APP_VERSION,
            "provider": current.provider,
            "models": _model_names(current),
            "ollama": ollama,
        }, headers={"Cache-Control": "no-store"})

    # ---- 首頁與上傳 --------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(msg: str | None = None):
        return home(msg=msg)

    @app.post("/upload")
    def upload(
        file: UploadFile | None = File(None),
        file_pick: UploadFile | None = File(None),
        doc_type: str = Form(UNSURE),
        category: str = Form(UNSURE),
    ):
        # 兩個入口:相機(file)與相簿/電腦選檔(file_pick),用有選檔的那個
        chosen = next((f for f in (file, file_pick) if f is not None and f.filename), None)
        if chosen is None:
            return home(status_code=400, error=MSG_NO_FILE, selected=doc_type, category=category)
        # 只取副檔名做白名單比對;檔名本身完全不用
        ext = Path(chosen.filename.replace("\\", "/")).suffix.lower()
        if ext not in cfg.supported_extensions:
            return home(status_code=400, error=MSG_BAD_EXT, selected=doc_type, category=category)

        uploads = cfg.paths.uploads_path
        uploads.mkdir(parents=True, exist_ok=True)
        dest = uploads / _new_upload_name(ext)
        size, head = 0, b""
        with dest.open("xb") as out:
            while chunk := chosen.file.read(_CHUNK):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    break
                if len(head) < 1024:
                    head += chunk[: 1024 - len(head)]
                out.write(chunk)
        error = None
        if size > MAX_UPLOAD_BYTES:
            error, code = MSG_TOO_LARGE, 413
        elif size == 0:
            error, code = MSG_EMPTY, 400
        elif not _looks_like(ext, head):
            error, code = MSG_MISMATCH, 400
        if error:
            dest.unlink(missing_ok=True)
            return home(status_code=code, error=error, selected=doc_type, category=category)

        # 大類與第二列各自只收白名單值,其他當作沒選(kind_choice):選了大類只收那一類清單上的名稱,
        # 當作家人選的文件(label;類型提示、要不要只在本機、要不要一律轉人工由 Pipeline 依固定清單決定);
        # 沒選大類只收五種類型,當作類型提示。任何一邊要求本機(敏感大類、藥袋、不能自動判讀的名稱)就在本機辨識
        group = category if category in CATEGORIES else None
        name = kind_choice(group, doc_type)
        hint, label = (None, name) if group else (name, None)
        record = Pipeline(current_cfg(), get_analyzer(), store()).process_file(dest, hint, category=group, label=label)
        return RedirectResponse(url=f"/doc/{record['文件ID']}", status_code=303)

    # ---- 文件櫃(四大類) ----------------------------------------------------

    @app.get("/cabinet", response_class=HTMLResponse)
    def cabinet(cat: str | None = None):
        """四大類的份數與文件;?cat= 只看一類(四大類或未分類),其他值當作沒選,依大類分組列出全部。"""
        v = cabinet_view(store().list_documents(limit=None), cat, cloud=cloud_mode())
        return page("cabinet.html", active="cabinet", v=v)

    # ---- 結果頁 ------------------------------------------------------------

    @app.get("/doc/{doc_id}", response_class=HTMLResponse)
    def document(doc_id: int, msg: str | None = None):
        doc = get_doc(doc_id)
        path = allowed_file(doc)
        file_url = f"/doc/{doc_id}/file" if path else None
        # 被家人更正取代的舊行動不再列出(紀錄留在資料庫):這裡只放現在有效的提醒與服藥表
        actions = [action_view(a) for a in store().list_actions(document_id=doc_id) if not a["superseded"]]
        corrected = bool(store().list_corrections(document_id=doc_id, verified_only=False, limit=1))
        v = doc_view(doc, file_url=file_url, hint=doc.get("doc_type_hint"), actions=actions,
                     file_size=_image_size(path), corrected=corrected,
                     correct_url=f"/doc/{doc_id}/correct" if correctable(doc) else None)
        return page("doc.html", msg=msg, v=v, actions=actions)

    @app.get("/doc/{doc_id}/file")
    def document_file(doc_id: int):
        path = allowed_file(get_doc(doc_id))
        if path is None:
            raise HTTPException(status_code=404, detail="找不到原件")
        return FileResponse(path, headers=_FILE_HEADERS)

    # ---- 更正讀值(F7) -----------------------------------------------------

    @app.get("/doc/{doc_id}/correct", response_class=HTMLResponse)
    def correct_form(doc_id: int):
        return correct_page(get_correctable(doc_id))

    @app.post("/doc/{doc_id}/correct")
    def correct_save(doc_id: int, form: FormData = Depends(read_form)):
        """存檔並重新核對;填錯就回同一頁,已填的值留著、錯的欄位寫中文說明。

        「再加一種藥」也送到這裡:不存檔,只把表單多一種藥再畫一次(沒有 JS 也能用)。
        表單值一律當資料:只讀這個類型的欄位,種類與分級由程式決定(原則 3)。
        「這份文件屬於哪一類」也在這張表單:只收四大類與未分類,其他值不改類別;讀值存好後才寫入。
        """
        doc = get_correctable(doc_id)
        changes, values, errors = parse_correction(doc["result"], form)
        category = form.get("category")
        values["category"] = category       # 重畫表單時留著家人剛選的類別
        if form.get("add_item"):
            return correct_page(doc, values, add_item=True)
        if errors:
            return correct_page(doc, values, errors, status_code=400)
        if category in ASSIGNABLE_CATEGORIES and reading_unchanged(ExtractionResult.from_dict(doc["result"]), changes):
            # 只改了類別:不重新核對、不換行動(已取消的提醒不會變回生效,已確認的服藥表不必再確認)
            store().set_document_category(doc_id, category)
            return _redirect_with_msg(f"/doc/{doc_id}", MSG_CATEGORY)
        correct_document(current_cfg(), store(), doc_id, changes, original=allowed_file(doc))
        if category in ASSIGNABLE_CATEGORIES:
            store().set_document_category(doc_id, category)
        return _redirect_with_msg(f"/doc/{doc_id}", MSG_CORRECTED)

    @app.post("/doc/{doc_id}/reject")
    def reject(doc_id: int) -> RedirectResponse:
        """待複核的文件讀不了(例如影像品質太差):同一筆文件改成「讀不出來」,請長輩重新拍。"""
        doc = get_doc(doc_id)
        if doc.get("action") != "review":
            raise PageError(409, "這份文件不在待複核", next_url=f"/doc/{doc_id}", next_label="回結果頁",
                            hint="已經存檔或讀不出來的文件不能在這裡退回。")
        reject_document(current_cfg(), store(), doc_id, original=allowed_file(doc))
        return _redirect_with_msg("/review", MSG_REJECTED)

    # ---- 取消/恢復提醒(F8) ------------------------------------------------

    @app.post("/reminder/{action_id}")
    def reminder_toggle(action_id: int, decision: str = Form(...)) -> RedirectResponse:
        """自動列入的期限提醒可以取消,也能恢復(原則 4:自動只給可取消的行動)。

        已經是想要的狀態(例如連按兩次「取消提醒」)就照樣回結果頁,不當成錯誤;
        要家人確認的行動不走這裡,在「家人確認」確認或退回。
        """
        if decision not in _REMINDER_DECISIONS:
            raise HTTPException(status_code=400, detail="只能選「取消提醒」或「恢復提醒」")
        action = get_action(action_id)
        if action["superseded"]:
            # 家人更正後舊提醒已被新的取代:恢復它會冒出兩個期限(例如從更正前開著的舊頁面按)
            raise PageError(409, "這個提醒已經換成新的了", next_url=f"/doc/{action['document_id']}",
                            next_label="回結果頁", hint="家人更正過這份文件,舊的提醒不再使用。請回結果頁看最新的提醒。")
        if not is_auto_reminder(action):
            # 走到這裡只會是要家人確認的事項(取消/恢復本身可以重複按,不會衝突)
            raise PageError(409, "這個事項要在「家人確認」處理",
                            hint="要家人點頭的事項不能在這裡取消,請到「家人確認」確認或退回。")
        status, msg = _REMINDER_DECISIONS[decision]
        store().set_action_status(action_id, status)
        return _redirect_with_msg(f"/doc/{action['document_id']}", msg)

    # ---- 家人確認 ----------------------------------------------------------

    @app.get("/confirm", response_class=HTMLResponse)
    def confirm_list(msg: str | None = None):
        items = []
        for a in pending_confirms():
            doc = store().get_document(a["document_id"]) or {}
            view = action_view(a)
            view["doc_title"] = doc_title(doc) if doc else "文件"
            view["doc_when"] = fmt_when(doc.get("created_at")) if doc else ""
            # 卡片旁放原件照片方便對照;PDF 沒辦法當圖片顯示,只留「看原件與全文」連結
            path = allowed_file(doc) if doc else None
            view["thumb_url"] = (f"/doc/{a['document_id']}/file"
                                 if path is not None and not is_pdf(path) else None)
            view["thumb_size"] = _image_size(path) if view["thumb_url"] else None
            items.append(view)
        return page("confirm.html", active="confirm", msg=msg, items=items)

    @app.post("/confirm/{action_id}")
    def confirm_decide(action_id: int, decision: str = Form(...)) -> RedirectResponse:
        if decision not in _CONFIRM_DECISIONS:
            raise HTTPException(status_code=400, detail="只能選「確認」或「退回」")
        action = get_action(action_id)
        if action["tier"] != "confirm" or action["status"] != "pending":
            raise HTTPException(status_code=409, detail="這個事項不需要確認,或已經處理過了")
        status, msg = _CONFIRM_DECISIONS[decision]
        store().set_action_status(action_id, status)
        return _redirect_with_msg("/confirm", msg)

    # ---- 待複核 ------------------------------------------------------------

    @app.get("/review", response_class=HTMLResponse)
    def review_index(msg: str | None = None):
        """讀 SQLite(原則 8:不再看 review/ 資料夾與 sidecar);每份連到更正頁。"""
        items = [review_row(d) for d in store().list_documents(action="review", limit=None)]
        return page("review_index.html", active="review", msg=msg, items=items)

    # ---- 系統設定(SET) ----------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    def settings(msg: str | None = None):
        return settings_page(msg=msg)

    @app.post("/settings")
    def settings_save(request: Request, provider: str | None = Form(None),
                      auto_threshold: str | None = Form(None)):
        """存全家共用的設定(辨識模式、門檻)。不合規則就回同一頁寫中文說明,什麼都不存;
        有改的每一項都留紀錄(何時、改成什麼、誰改的)。辨識模式改了,下一次上傳會重建 analyzer。"""
        values = {PROVIDER: provider, THRESHOLD: auto_threshold}
        changes, errors = form_changes(values, cfg, current_cfg())
        if errors:
            return settings_page(status_code=400, values=values, errors=errors)
        actor = _actor(request)
        for key, value in changes.items():
            store().set_setting(key, value, actor=actor)
        return _redirect_with_msg("/settings", MSG_SETTINGS_SAVED if changes else MSG_SETTINGS_UNCHANGED)

    @app.get("/settings/export")
    def settings_export() -> Response:
        """匯出全部紀錄:文件與讀值、行動、更正、設定變更(JSON 下載,不含影像)。內容有個資,不讓瀏覽器或代理存下來。"""
        body = json.dumps(export_records(store(), cfg, version=APP_VERSION), ensure_ascii=False, indent=2)
        stamp = f"{datetime.now():%Y%m%d-%H%M}"
        # 中文檔名給認得 filename* 的瀏覽器,其他的用英數檔名
        disposition = (f'attachment; filename="records-{stamp}.json"; '
                       f"filename*=UTF-8''{quote(f'看有紀錄-{stamp}.json')}")
        return Response(body, media_type="application/json",
                        headers={"Content-Disposition": disposition, "Cache-Control": "no-store"})

    @app.post("/settings/delete")
    def settings_delete(request: Request) -> RedirectResponse:
        """刪除全部資料:文件、行動、更正(SQLite)、處理紀錄 records.jsonl 與四個資料夾裡的原件;
        設定與設定紀錄保留,刪除本身也記一筆。app.log 只記類型與數量,不含讀值,不刪。

        先清資料庫再刪檔案:檔案刪到一半卡住時,畫面上已經沒有指向它的文件;刪不掉的檔案會在訊息裡說。
        """
        store().purge_all()
        clear_records(cfg.paths.logs)
        _, failed = delete_data_files(cfg)
        store().set_setting(PURGED, datetime.now().isoformat(), actor=_actor(request))
        return _redirect_with_msg("/", MSG_PURGED_PARTLY if failed else MSG_PURGED)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    # 僅本機存取;對外一律經 Cloudflare Tunnel,不直接開放埠號
    uvicorn.run(app, host="127.0.0.1", port=8000)
