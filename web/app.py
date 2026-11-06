"""看有(高齡家庭文書輔助)網頁介面(FastAPI)。

啟動(專案根目錄):
    uvicorn web.app:app --reload
    # 或   python web/app.py

頁面:
    /                    家人要處理的事 + 拍照或上傳文件(只選類型)+ 要記得的事 + 最近看過的文件
    POST /upload         存檔(重產檔名)→ Pipeline(藥袋、發票／收據與沒選類型的文件只在本機辨識)→ 303 到結果頁
    POST /upload/check   選好照片後的拍照檢查(縮小的照片只在記憶體裡算,不存檔):偵測到模糊或缺角才回一句提示
    /cabinet             文件櫃:四大類的份數與文件;?cat= 只看一類
    /doc/{id}            白話解說、朗讀、核對印章、行動、欄位、原件
    /doc/{id}/file       原件(只送 archive/review/failed/uploads 內的檔)
    /doc/{id}/retype     家人改文件類型:GET 選類型;POST 照新類型在本機重讀 → 重新核對 → 舊行動標成已取代
    /doc/{id}/correct    更正讀值(F7)與指定大類:GET 表單;POST 存檔 → 重新核對、重算行動 → 303 回結果頁
    POST /doc/{id}/reject  待複核的文件退回(品質不足)→ 303 回待複核
    /reminders          「要記得的事」的全部列(首頁只放前幾筆)
    /help               使用手冊首頁:搜尋手冊(?q=)、主題清單;畫面上的說明都集中在這裡(docs/使用手冊.md)
    /help/{編號}        使用手冊的一篇(一個大節):導覽路徑、更新日期、「在此頁面上」、上一篇與下一篇
    POST /rejected/{id}/seen  退回通知按「已讀」收掉(只記操作紀錄)
    POST /reminder/{id}  取消或恢復自動列入的期限提醒(F8)→ 303 回結果頁
    /confirm            家人確認:tier=confirm 且 pending 的行動,一次一件(?item= 打開指定的那一件)
    /review              待複核清單(讀 SQLite,每份連到更正頁)
    /settings            系統狀態、這台裝置的偏好、全家共用的設定(POST 存檔 → 303 回設定頁)、資料管理
    /settings/export     匯出全部紀錄(JSON 下載,不含影像)
    POST /settings/delete  刪除全部資料(文件、行動、更正與原件;設定保留)→ 303 回首頁
    /healthz             健康檢查(JSON),給開機腳本與監看用

安全範圍:本機只綁 127.0.0.1;對外上線時由 Cloudflare Tunnel + Access 負責登入
(見 docs/部署指南.md)。檔名一律不信任使用者輸入,樣板 autoescape;
所有回應帶安全標頭,除了 PDF 原件都帶 CSP(樣板沒有行內 JavaScript;瀏覽器內建的 PDF 閱讀器套上 CSP
會顯示不出來),所有 POST 都檢查 CSRF token(W2-B),body 有大小上限(超過回 413,不先整份收下)。
要先確認的動作(切到雲端備援、刪除全部資料、兩處退回)伺服器要收到確認欄位才做:有 JS 時確認框按
「確定…」後由 app.js 補上;沒有 JS 就回一頁確認頁(ask.html)再問一次,不會一按就生效。
改同一份文件的 POST(更正、退回、取消/恢復提醒、家人確認、刪除全部資料)共用一把鎖,一次只做一個。
"""
from __future__ import annotations

import io
import json
import logging
import re
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from markupsafe import Markup
from PIL import Image, ImageOps
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.concurrency import run_in_threadpool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import AppConfig, load_config  # noqa: E402
from src.duplicates import file_sha256, find_duplicate, verified_invoice_number  # noqa: E402
from src.lottery import SETTING_PREFIX as LOTTERY_PREFIX  # noqa: E402
from src.lottery import (  # noqa: E402
    Numbers,
    is_lottery_reminder,
    latest_drawn,
    match,
    parse_form as parse_lottery_form,
    parse_period,
    period_of,
    qr_buyer_has_tax_id,
    reminder_action,
)
from src.models import CATEGORIES, DOC_TYPES  # noqa: E402
from src.pipeline import (  # noqa: E402
    Pipeline,
    Received,
    Redo,
    error_kind,
    interrupt_document,
    model_unavailable,
    receive_document,
    recover_documents,
    start_redo,
)
from src.preprocess import decrypt_pdf, pdf_needs_password, prepare_image, strip_metadata  # noqa: E402
from src.providers import create_analyzer  # noqa: E402
from src.models import ExtractionResult  # noqa: E402
from src.photo_check import photo_hint  # noqa: E402
from src.record_log import clear_records  # noqa: E402
from src.review import (  # noqa: E402
    correct_document,
    correctable,
    delete_document,
    reading_unchanged,
    reject_document,
    retype_document,
)
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
from src.store import CONSENT_PREFIX, Store  # noqa: E402
from web.access import AccessIdentityResolver, Identity, IdentityError  # noqa: E402
from web.jobs import JobBoard  # noqa: E402
from web.manual import SEARCH_MAX as MANUAL_SEARCH_MAX, load_manual, search_manual  # noqa: E402
from web.roles import Principal, principal_for, member_values, audit_rows  # noqa: E402
from web.render import (  # noqa: E402
    ASSIGNABLE_CATEGORIES,
    BRAND_FULL,
    CONFIRM_CLOUD_FIELD,
    CONFIRM_PURGE_FIELD,
    CONFIRM_DELETE_FIELD,
    CONFIRM_REJECT_FIELD,
    DELETE_DOC_ASK,
    HOME_REMINDERS,
    REJECT_EVENTS,
    PURGE_ASK,
    REJECT_DOC_ASK,
    DONE_COMPLETE,
    DONE_UNDO,
    REMINDER_CANCEL,
    REMINDER_RESTORE,
    STATIC_DIR,
    UNSURE,
    action_view,
    ai_reading,
    cabinet_view,
    cancelled_rows,
    change_note,
    change_param,
    completed_rows,
    cloud_ask,
    cloud_privacy,
    doc_category,
    correct_view,
    doc_row,
    doc_title,
    doc_view,
    fmt_when,
    group_rows,
    is_auto_reminder,
    is_pdf,
    kind_choice,
    lottery_view,
    overdue_days,
    overdue_text,
    waited_days,
    parse_correction,
    progress_view,
    reject_action_ask,
    rejected_rows,
    reminder_rows,
    render,
    retype_view,
    review_row,
    settings_view,
    trust_notes,
    type_choices,
    upload_label,
)
from web.render import env as jinja_env  # noqa: E402

log = logging.getLogger(__name__)

# 上傳大小上限:手機照片通常 2–6MB,15MB 足夠又能擋掉灌爆硬碟的請求
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
# multipart 表頭與其他欄位的額外位元組;Content-Length 超過「上限 + 這個值」就不必讀內容
_MULTIPART_SLACK = 64 * 1024
_CHUNK = 1024 * 1024
# 上傳以外的表單只有文字欄位(更正讀值、確認、設定)。更正表單收得下的最大內容是 30 種藥、藥名與用法各 500 字,
# 網址編碼後一個中文字 9 個位元組,大約 280KB;上限抓 1MB,超過就不收
MAX_FORM_BYTES = 1024 * 1024

# 操作完成後轉址帶回的狀態訊息
MSG_CORRECTED = "已更正"
MSG_REJECTED = "已退回"
MSG_CONFIRMED = "已確認"
MSG_REMINDER_OFF = "已取消提醒"
MSG_REMINDER_ON = "已恢復提醒"
MSG_CATEGORY = "已更新類別"
MSG_NO_CHANGE = "沒有改動"
MSG_SETTINGS_SAVED = "已儲存設定"
MSG_SETTINGS_UNCHANGED = "設定沒有變動"
MSG_PURGED = "已刪除全部資料"
MSG_PURGED_PARTLY = "資料已刪除，但有些照片檔刪不掉，請管理者檢查資料夾。"
# 只有這些狀態訊息可以經由 ?msg= 顯示;任意字串不回顯,避免被拿來偽造系統公告
MSG_UPLOAD_CANCELLED = "已取消上傳"
MSG_DOC_DELETED = "已刪除這份文件"
MSG_REMINDER_DONE = "已記下，移到「已完成」"
MSG_REMINDER_UNDONE = "已改回，放回「要記得的事」"
MEMBER_EMAIL_ERROR = "請輸入完整的電子郵件，例如 name@example.com"
MSG_LOTTERY_SAVED = "已存好這一期的號碼"
MSG_LOTTERY_REMINDED = "已加進「要記得的事」"
MSG_LOTTERY_ALREADY = "這張已經加過了"
MSG_SEEN = "已收起"
ALL_EVENTS = 10000   # 一次取全部操作紀錄或更正紀錄時的上限(一個家庭用不到這麼多)
_MESSAGES = frozenset({MSG_SEEN, MSG_CORRECTED, MSG_REJECTED, MSG_CONFIRMED, MSG_REMINDER_OFF, MSG_REMINDER_ON,
                       MSG_CATEGORY, MSG_NO_CHANGE, MSG_SETTINGS_SAVED, MSG_SETTINGS_UNCHANGED, MSG_PURGED, MSG_PURGED_PARTLY,
                       MSG_UPLOAD_CANCELLED, MSG_DOC_DELETED, MSG_REMINDER_DONE, MSG_REMINDER_UNDONE,
                       MSG_LOTTERY_SAVED, MSG_LOTTERY_REMINDED, MSG_LOTTERY_ALREADY})
MSG_NO_PRIZE = "這張發票沒有可以領的獎"
# 刪除全部資料沒有全部做完時的錯誤頁標題(說明裡寫哪些已刪、哪些沒刪,下一步回設定頁再按一次)
MSG_PURGE_NOT_STARTED = "資料沒有刪除"
MSG_PURGE_STUCK = "有些資料還沒刪掉"
# 資料使用說明不攔的路徑(說明頁本身、健康檢查、樣式與圖示)
_NOTICE_FREE = ("/data-use", "/healthz", "/static/")


def _safe_next(value: str) -> str:
    """看完說明要回去的頁面:只收這個網站裡的路徑(/ 開頭、不是 //、沒有反斜線),其他一律回首頁。"""
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


# 確認欄位的值:確認框按「確定…」後 app.js 補的隱藏欄位、沒有 JS 時勾選的欄位、確認頁的隱藏欄位都送這個值;
# 要先確認的動作(切到雲端備援、刪除全部資料、退回)沒收到就不做
CONFIRMED = "1"
# 家人確認:表單值 → (行動狀態, 回到家人確認頁時的訊息)
_CONFIRM_DECISIONS = {"done": ("done", MSG_CONFIRMED), "rejected": ("rejected", MSG_REJECTED)}
# 取消/恢復提醒:表單值 → (行動狀態, 回到結果頁時的訊息)
_REMINDER_DECISIONS = {REMINDER_CANCEL: ("rejected", MSG_REMINDER_OFF), REMINDER_RESTORE: ("pending", MSG_REMINDER_ON)}

MSG_NO_FILE = "請先拍照，或選一個檔案。"
MSG_EMPTY = "這個檔案是空的，請重新拍一張。"
MSG_BAD_EXT = "只能上傳照片（JPG、PNG、WEBP、BMP、TIFF）或 PDF。"
MSG_MISMATCH = "檔案內容和副檔名對不上，請直接用相機拍一張，或選原始的照片檔。"
MSG_TOO_LARGE = "檔案超過 15MB。請改拍一張，或選較小的檔案。"
MSG_FORM_TOO_LARGE = "送出的資料太多了"

# 加密 PDF(PDF-PW):上傳後先放進 uploads/pending/ 等家人輸入密碼。旁邊的 .json 只記建立時間、試了幾次、
# 家人選的大類與名稱——沒有密碼。密碼(常是身分證字號或生日)只用來解開這一份,不存、不記、不回顯
PENDING_DIR = "pending"
PENDING_TTL = 30 * 60          # 秒;放超過就刪,網址也跟著失效
MAX_PASSWORD_TRIES = 5         # 錯這麼多次就把暫存檔刪掉,請家人重新上傳
_PENDING_TOKEN = re.compile(r"[A-Za-z0-9_-]{32}")   # secrets.token_urlsafe(24) 的格式
MSG_PDF_WRONG = "密碼不對，還可以再試 {left} 次。"
MSG_PDF_GONE = "密碼錯太多次，這份檔案已經刪掉了。請回首頁重新上傳。"
MSG_PDF_MISSING = "找不到這份等待輸入密碼的檔案"

# 上傳後在背景辨識(UX-3,web/jobs.py):這麼多秒內做完(展示模式、模擬讀值)就直接打開結果,不經過進度頁
QUICK_RESULT_SECONDS = 1.0
MSG_JOB_MISSING = "找不到這次上傳的進度"
MSG_JOB_FAILED = "這份文件處理到一半出了問題"
MSG_NO_RETRY = "這份文件不用重新辨識"
MSG_RETRY_NOT_STARTED = "這次沒有開始重新辨識"
_RETRY_NOT_STARTED_HINT = "照片還在，文件也沒有變。請等一下再按一次「重新辨識」；一直不行的話，請家人看一下這台電腦。"
MSG_RETYPE_FAILED = "這次沒有讀成，類型沒有改"
MSG_RETYPE_NO_ORIGINAL = "原件已經不在，不能重讀"
MSG_RETYPE_CHOOSE = "請選一個類型。"
_JOB_MISSING_HINT = ("可能已經處理好一陣子了，或這台電腦剛重新啟動。文件都在文件櫃裡；"
                     "寫著「還沒讀」的，打開按「重新辨識」就好，不用重新上傳。")
_JOB_FAILED_HINT = "這份沒有存成文件。請回首頁再上傳一次；一直不行的話，請家人看一下這台電腦。"
# 收件時已經建好文件(src.pipeline.receive_document):照片還在,重新上傳會多一份
_JOB_KEPT_HINT = "照片已經收好了，不用重新上傳。請打開這份文件，等一下再按「重新辨識」；一直不行的話，請家人看一下這台電腦。"
_RETYPE_FAILED_HINT = "原本的讀值和提醒都還在。請等一下再試一次；一直不行的話，請看一下這台電腦的模型有沒有開。"

# 框架預設的英文錯誤訊息換成中文
_DEFAULT_ERRORS = {404: "找不到這個頁面", 405: "這個網址不能這樣使用"}
MSG_INVALID = "送出的資料不完整"        # 網址或表單欄位的格式不對(FastAPI 預設回英文 JSON)
MSG_SERVER_ERROR = "系統出了點問題"     # 沒預期到的例外;頁面不寫例外內容
# 原件可能是藥袋等特種個資:不讓瀏覽器或中間的代理快取(nosniff 等安全標頭由 security_headers 統一加);
# 明寫 inline:原件是在瀏覽器裡看的,不是下載
_FILE_HEADERS = {"Cache-Control": "private, no-store", "Content-Disposition": "inline"}
# 原件的 Content-Type 依副檔名查這張固定的表,不靠主機的 mimetypes:Windows 會讀登錄檔,有的電腦不認得 .webp,
# 回 application/octet-stream 時「放大看原件(另開新頁)」會變成下載,照片就留在裝置上。
# 收得下的副檔名(AppConfig.supported_extensions)都要列在這裡
_MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff", ".pdf": "application/pdf",
}
# SQLite 的整數最大值;網址裡的文件編號超過它就是找不到(404)
_MAX_DOC_ID = 2 ** 63 - 1

# 錯誤頁的下一步(說明, 按鈕網址, 按鈕文字),依狀態碼的預設:填錯的回上一頁改,已處理過的回家人確認,
# 其他(含沒帶 CSRF token 的 403)回首頁。個別錯誤要不同的下一步,改丟 PageError
_BACK_HINT = "請按瀏覽器的「上一頁」改好後再送出一次。"
_HOME_STEP = ("可以回首頁重新開始。", "/", "回首頁")
_NEXT_STEPS = {
    400: (_BACK_HINT, "/", "回首頁"),
    403: ("為了安全，系統沒有處理這次的操作（可能是頁面開太久了）。請回首頁重新操作一次。", "/", "回首頁"),
    409: ("這件事可能已經有人處理了，請回「家人確認」看最新狀態。", "/confirm", "回家人確認"),
    422: (_BACK_HINT, "/", "回首頁"),
}
# 按鈕圖示跟著目的地走,和導覽列同一組
_NEXT_ICONS = {"/": "home", "/confirm": "family", "/review": "tray", "/settings": "gear"}


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


# ---- POST 的 body 大小上限 ---------------------------------------------------
# 每個 POST 都有上限,而且在收完整份之前就擋:沒有上限的話,任何表單路由都能被塞進超大的 body
# (檔案部分會先寫進暫存檔),收完才輪到檢查 CSRF。

def _body_limit(path: str) -> int:
    """這個路徑的 POST 最多收幾個位元組:上傳是檔案上限加 multipart 的表頭,其他表單是 MAX_FORM_BYTES。"""
    return MAX_UPLOAD_BYTES + _MULTIPART_SLACK if path == "/upload" else MAX_FORM_BYTES


def _too_large(path: str) -> PageError:
    """body 超過上限的 413(中文說明 + 下一步)。"""
    if path == "/upload":
        return PageError(413, MSG_TOO_LARGE, hint="請回首頁改拍一張，或選較小的檔案。")
    return PageError(413, MSG_FORM_TOO_LARGE,
                     hint="這張表單收不下這麼多內容，所以沒有處理。請按瀏覽器的「上一頁」，把內容改短一點再送出。")


class _BodyLimit:
    """邊收邊數 POST 的 body,超過上限就丟 413,不把整份收完(純 ASGI middleware,包住 receive)。

    Content-Length 已經超過上限的,limit_body_size 在收任何東西之前就回 413;這裡擋的是沒有 Content-Length
    (分塊傳送)的請求。不用 411 拒收這種請求:它本身合法,大小正常的照常處理。
    要放在最內層(最先 add_middleware),例外才會直接回到讀表單的地方、照一般的錯誤頁處理;
    隔著 @app.middleware 的話例外會被包成一組,FastAPI 當成表單解析失敗,回的是 400。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        received = 0

        async def counted() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > _body_limit(scope["path"]):
                    raise _too_large(scope["path"])
            return message

        await self.app(scope, counted, send)


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
APP_VERSION = "2026.10.27"
_OLLAMA_PROBE_TIMEOUT = 2.0   # 秒;健康檢查不能被卡住的 Ollama 拖住

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


def _thumbnail(path: Path) -> bytes | None:
    """進度頁的小縮圖(JPEG,只放在記憶體,做完就丟);PDF 或讀不了就沒有。原件已經去過拍攝資訊(strip_metadata)。"""
    if is_pdf(path):
        return None
    try:
        with Image.open(path) as im:
            im.draft("RGB", (240, 320))   # JPEG 直接用縮小的倍率解碼,大照片也很快
            small = ImageOps.exif_transpose(im)
            small.thumbnail((240, 320))
            out = io.BytesIO()
            small.convert("RGB").save(out, "JPEG", quality=70)
            return out.getvalue()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None


def _new_upload_name(ext: str) -> str:
    """時間戳 + 隨機碼;完全不使用使用者提供的檔名。"""
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(4)}{ext}"


def _read_pending(meta: Path) -> dict[str, Any] | None:
    """等待輸入密碼的資訊檔;不存在、壞掉或欄位不對都回 None(當作沒有這份)。"""
    try:
        info = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = (isinstance(info, dict) and isinstance(info.get("created"), (int, float))
          and isinstance(info.get("tries"), int)
          and all(isinstance(info.get(k), (str, type(None))) for k in ("category", "name")))
    return info if ok else None


def _redirect_with_msg(path: str, msg: str) -> RedirectResponse:
    """POST 做完就 303 轉址並帶狀態訊息(重新整理不會重送表單)。訊息不在白名單內,頁面不會顯示,所以直接擋下。"""
    assert msg in _MESSAGES, msg
    return RedirectResponse(url=f"{path}{'&' if '?' in path else '?'}msg={quote(msg)}", status_code=303)


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
    body 超過大小的 413 由 limit_body_size 在讀 body 之前就回(沒有 Content-Length 的由 _BodyLimit 邊收邊擋),不受影響。
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


def _ollama_probe(cfg: AppConfig, transport: httpx.BaseTransport | None = None) -> tuple[str, bool | None]:
    """(reachable / unreachable / unused, 設定的模型是不是已經載入顯示記憶體);mock 模式不連。

    問 Ollama 的 /api/ps(列出已載入的模型):連得上就是 reachable;模型已載入,第一張文件就不必等載入。
    看不懂回覆時「有沒有載入」是 None。Ollama 在本機,不走系統代理。
    """
    if cfg.provider == "mock":
        return "unused", None
    try:
        with httpx.Client(transport=transport, timeout=_OLLAMA_PROBE_TIMEOUT, trust_env=False) as client:
            response = client.get(f"{cfg.ollama.host.rstrip('/')}/api/ps")
    except (httpx.HTTPError, httpx.InvalidURL):
        return "unreachable", None
    if response.status_code != 200:
        return "unreachable", None
    try:
        loaded = {_model_tag(m.get("name") or m.get("model")) for m in response.json().get("models") or []}
    except (ValueError, AttributeError, TypeError):
        return "reachable", None
    return "reachable", _model_tag(cfg.ollama.model) in loaded


def _model_tag(name: Any) -> str:
    """模型名稱沒寫標籤的就是 :latest(gemma4 與 gemma4:latest 是同一個)。"""
    text = str(name or "")
    return text if ":" in text else f"{text}:latest"


def _ollama_status(cfg: AppConfig, transport: httpx.BaseTransport | None = None) -> str:
    """reachable / unreachable / unused(mock 模式不連);設定頁用。"""
    return _ollama_probe(cfg, transport)[0]


def create_app(cfg: AppConfig | None = None, analyzer=None,
               ollama_transport: httpx.BaseTransport | None = None, identity_resolver=None) -> FastAPI:
    """建立 app;測試可注入隔離在 tmp 的設定與 MockAnalyzer。

    analyzer 預設依目前生效的設定(current_cfg)建立,在第一次上傳時才建立,
    這樣只開複核頁或匯入模組時不會去連模型;設定頁切換辨識模式後,下一次上傳會重建。
    ollama_transport 是 /healthz 檢查 Ollama 用的 httpx transport;測試注入 MockTransport,不連網。
    """
    cfg = cfg or load_config()
    cfg.ensure_dirs()
    resolver = identity_resolver if identity_resolver is not None else AccessIdentityResolver()
    identity_context: ContextVar[Identity | None] = ContextVar("identity", default=None)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 網頁啟動時(uvicorn;測試要用 with TestClient(app)):先接回上次中斷的文件,再補算舊文件的原件雜湊;
        # 只匯入這個模組不碰資料庫
        await run_in_threadpool(recover_interrupted)
        await run_in_threadpool(backfill_hashes)
        yield

    # 不開 API 文件頁(/docs 等):它從 CDN 載入,違反 CSP 與離線原則,對外也不需要公開 API 結構。
    # 每個路由都先過 check_csrf(只檢查會改資料的請求),之後新增的表單路由不必各自記得
    app = FastAPI(title=BRAND_FULL, docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(check_csrf)], lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # 外層的 cfg 是 config.yaml 的設定;state["cfg"] 是最近一次讀到的生效設定(錯誤頁用,不再讀資料庫)
    # state["analyzer"] 是(依哪個設定建的, analyzer)一起存的一組:分成兩個值各寫一次的話,兩次寫入之間
    # 別的上傳可能讀到新的 analyzer 配舊的鍵
    state: dict[str, Any] = {"analyzer": None, "store": None, "cfg": cfg}
    analyzer_lock = threading.Lock()
    # 改文件的路由(更正、退回、取消/恢復提醒、家人確認、刪除全部資料)一次只做一個,而且拿到鎖之後才讀文件:
    # 「讀文件 → 算原件位置 → 搬原件、寫回」中間被另一個請求插進來,兩邊會拿同一個舊位置去搬
    # (連按兩下「儲存更正並重新核對」,後寫的那個把已經不存在的路徑寫回資料庫)。
    # 部署是單一 uvicorn 行程、家庭用量小,行程內一把鎖就夠;改成多個 worker 時要換成資料庫層的鎖
    doc_lock = threading.Lock()

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

    def principal() -> Principal:
        identity = identity_context.get()
        if identity is None:
            raise HTTPException(403, "無法確認登入身分。")
        return principal_for(identity, store())

    def require(permission: str) -> Principal:
        who = principal()
        if not who.permissions.get(permission, False):
            raise HTTPException(403, "這項操作需要家人協助。" if who.role == "elder" else "您目前只能查看，請家人設定家庭成員。")
        return who

    def actor_fields(who: Principal) -> dict[str, str]:
        return dict(actor=who.actor, role=who.role)

    def get_analyzer():
        """上傳用的 analyzer:測試注入的一律沿用;否則依目前的辨識模式建立,模式改了就重建。

        「比較 → 重建 → 寫入」整段鎖住:同時進來的上傳不會各建一個,拿到的 analyzer 也一定是照自己讀到的設定建的。
        """
        if analyzer is not None:
            return analyzer
        current = current_cfg()
        built_for = (current.provider, current.local_only_doc_types)
        with analyzer_lock:
            cached = state["analyzer"]
            if cached is None or cached[0] != built_for:
                cached = state["analyzer"] = (built_for, create_analyzer(current))
            return cached[1]

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
                      msg=msg if msg in _MESSAGES else None,
                      identity=principal().view, permissions=principal().permissions, **ctx)
        return HTMLResponse(html, status_code=status_code)

    def member_names() -> dict[str, str]:
        """登入 email → 家庭成員的稱呼(清單上寫「誰按的」用)。"""
        return {m["email"]: m.get("display_name") or "" for m in store().list_members()}

    def notices_for(docs: list[dict[str, Any]], actions: list[dict[str, Any]], names: dict[str, str]) -> list[dict[str, Any]]:
        """被退回的文件與事項(別人退回的才列給現在看的人,見 render.rejected_rows)。"""
        events = store().list_audit_events(event=REJECT_EVENTS, limit=ALL_EVENTS)
        return rejected_rows(docs, actions, events, date.today(), names, viewer=principal().actor)

    def cloud_mode() -> bool:
        """雲端模式(依設定頁目前生效的設定):畫面上才需要說明哪些文件只在本機處理;本機模式所有文件都在這台電腦辨識。"""
        return current_cfg().provider == "workers_ai"

    def reminders_for(docs: list[dict[str, Any]], actions: list[dict[str, Any]], names: dict[str, str]) -> list[dict[str, Any]]:
        """「要記得的事」全部的列;不能確認的人(長輩)等家人確認的列連到那份文件(render.reminder_rows)。"""
        return reminder_rows(docs, actions, date.today(), notices=notices_for(docs, actions, names),
                             can_confirm=principal().permissions["can_confirm"])

    def home(status_code: int = 200, error: str | None = None, selected: str = UNSURE, msg=None):
        docs = store().list_documents(limit=None)      # 行動是全部的,舊文件的提醒也要查得到商家與金額
        recent = [doc_row(d) for d in docs[:8]]
        actions, names, today = store().list_actions(), member_names(), date.today()
        reminders = reminders_for(docs, actions, names)   # 首頁只放前幾筆
        # 只取「按了完成」這一種:其他操作紀錄再多也不會把還在 7 天內的擠掉
        completed = completed_rows(docs, actions, store().list_audit_events(event="complete_reminder", limit=200),
                                   today, names)
        current = current_cfg()
        return page(
            "home.html", active="home", status_code=status_code, msg=msg,
            # 重畫時(例如檔案太大)留住剛才選的類型;只留伺服器收得下的值,和上傳時同一份白名單
            error=error, selected_type=kind_choice(None, selected) or UNSURE, type_choices=type_choices(),
            recent=recent, remind_groups=group_rows(reminders[:HOME_REMINDERS]),
            reminders_shown=min(len(reminders), HOME_REMINDERS), reminders_total=len(reminders),
            completed=completed, max_bytes=MAX_UPLOAD_BYTES,
            # 雲端模式才寫哪些類型只在本機:本機模式所有文件都在這台電腦,特別寫出來反而像別的會送出去
            upload_note=cloud_privacy(current.local_only_doc_types) if current.provider == "workers_ai" else "",
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

    def recover_interrupted() -> None:
        """接回上次中斷的文件(src.pipeline.recover_documents):還在處理中的改成「還沒讀」、搬到一半的原件認回原位。
        和改文件的路由同一把鎖;這個行程裡還在做的背景工作不算中斷。出錯只記 log:服務照樣開起來,家人才看得到文件。"""
        try:
            with doc_lock:
                recover_documents(cfg, store(), busy=jobs.busy_docs())
        except Exception as exc:
            log.error("開機恢復失敗:%s", error_kind(exc))

    def backfill_hashes() -> int:
        """原件雜湊欄位加上之前就有的文件:原件還在的補算雜湊,重複上傳提示才找得到舊的那份。

        讀不到的跳過(下次啟動再試),回傳補了幾份;紀錄只寫文件編號與例外種類。
        """
        filled = 0
        for doc in store().documents_missing_hash():
            path = allowed_file(doc)
            if path is None:
                continue
            try:
                store().set_file_hash(doc["id"], file_sha256(path))
            except OSError as exc:
                log.warning("補算原件雜湊失敗:文件 %s(%s)", doc["id"], type(exc).__name__)
                continue
            filled += 1
        if filled:
            log.info("補上 %d 份文件的原件雜湊", filled)
        return filled

    def duplicate_of(doc: dict[str, Any]) -> dict[str, Any] | None:
        """「這張可能已經上傳過」:原件雜湊相同,或兩份發票的號碼都經 QR 核對相符而且一樣(src/duplicates.py)。"""
        same_file = store().documents_with_hash(doc["file_sha256"]) if doc.get("file_sha256") else []
        invoices = (store().list_documents(doc_type="發票", limit=None)
                    if verified_invoice_number(doc.get("result")) else [])
        return find_duplicate(doc, same_file, invoices)

    def get_doc(doc_id: int) -> dict[str, Any]:
        # 編號超出 SQLite 整數範圍時 sqlite3 會丟 OverflowError(變成 500);不可能有這份文件,不必查
        doc = store().get_document(doc_id) if 0 < doc_id <= _MAX_DOC_ID else None
        if doc is None:
            raise HTTPException(status_code=404, detail="找不到這份文件")
        return doc

    def get_correctable(doc_id: int) -> dict[str, Any]:
        doc = get_doc(doc_id)
        if not correctable(doc):
            raise PageError(409, "這份文件沒有可以更正的讀值", hint="讀不出來的文件沒辦法更正，請回首頁重新拍一張。",
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

    jobs = JobBoard()   # 上傳後的背景辨識:一次一張、照順序(web/jobs.py)

    def start_job(dest: Path, group: str | None, name: str | None, *, started: float,
                  redo: Redo | None = None) -> RedirectResponse:
        """收下的上傳檔(加密 PDF 已經解開)先建一筆「處理中」的文件,再交給背景辨識,轉到進度頁;
        QUICK_RESULT_SECONDS 內做完就直接到結果頁。

        group、name 是過了白名單的大類與名稱:選了大類,名稱當作家人選的文件(label);沒選大類,名稱就是類型提示。
        設定與 analyzer 照上傳當下的辨識模式在這裡取好,背景只管照做。started 是這個請求開始處理的時間
        (time.monotonic),「收到照片」那一步的秒數從這裡算到縮圖做好。redo:重新辨識既有的文件(start_redo 已改成處理中)。
        收件就建文件(src.pipeline.receive_document):排隊中或讀到一半服務重開,文件也在文件櫃裡,開機時改成「還沒讀」。
        建不成(資料庫寫不進去)就把收下的檔刪掉、照實說沒存成,請家人重新上傳,不會留下沒有紀錄的檔。
        背景讀到一半出錯,文件改成「還沒讀」(interrupt_document),照片還在就能重新辨識。
        """
        hint, label = (None, name) if group else (name, None)
        current = current_cfg()
        pipeline = Pipeline(current, get_analyzer(), store())
        update: Redo | Received
        if redo is not None:
            update = redo
        else:
            try:
                update = Received(receive_document(store(), dest, hint, category=group, label=label))
            except Exception as exc:
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
                log.error("收件時寫不進資料庫:%s", error_kind(exc))
                raise PageError(500, MSG_JOB_FAILED, next_url="/", next_label="回首頁", hint=_JOB_FAILED_HINT) from None

        def work(report: Callable[[str], None]) -> int:
            try:
                return pipeline.process_file(dest, hint, category=group, label=label, on_stage=report,
                                             redo=update)["文件ID"]
            except Exception:
                interrupt_document(cfg, store(), update.doc_id)
                raise

        return run_job(work, dest, provider=current.provider, label=upload_label(name), started=started,
                       for_doc=update.doc_id)

    def run_job(work: Callable[[Callable[[str], None]], int], dest: Path, *, provider: str, label: str,
                started: float, failed_doc: int | None = None, for_doc: int | None = None) -> RedirectResponse:
        """交給背景做,轉到進度頁;QUICK_RESULT_SECONDS 內做完就直接到結果頁。for_doc 是這個工作處理的文件(開機恢復不動它)。"""
        photo = _thumbnail(dest)
        job = jobs.submit(work, owner=principal().actor, provider=provider, is_pdf=is_pdf(dest), label=label,
                          photo=photo, received=time.monotonic() - started, failed_doc=failed_doc, for_doc=for_doc)
        if not jobs.wait(job, QUICK_RESULT_SECONDS):
            return RedirectResponse(url=f"/upload/progress/{job.token}", status_code=303)
        if job.doc_id is None:
            raise job_failed(job)
        return RedirectResponse(url=f"/doc/{job.doc_id}", status_code=303)

    def job_failed(job) -> PageError:
        if job.failed_doc is not None:      # 改類型:文件與提醒都沒有動,不叫人重新上傳
            return PageError(500, MSG_RETYPE_FAILED, next_url=f"/doc/{job.failed_doc}", next_label="回到這份文件",
                             hint=_RETYPE_FAILED_HINT)
        if job.for_doc is not None:         # 收件時就建好文件了:回到那一份按「重新辨識」,重新上傳會多一份
            return PageError(500, MSG_JOB_FAILED, next_url=f"/doc/{job.for_doc}", next_label="回到這份文件",
                             hint=_JOB_KEPT_HINT)
        return PageError(500, MSG_JOB_FAILED, next_url="/", next_label="回首頁", hint=_JOB_FAILED_HINT)

    def get_job(token: str):
        """本人上傳、還記得的那一份;token 格式不對、過期、別人上傳的都當作找不到。"""
        job = jobs.get(token, principal().actor) if _PENDING_TOKEN.fullmatch(token) else None
        if job is None:
            raise PageError(404, MSG_JOB_MISSING, next_url="/cabinet", next_label="到文件櫃找找", hint=_JOB_MISSING_HINT)
        return job

    pending_lock = threading.Lock()

    def pending_files(token: str) -> tuple[Path, Path]:
        """等待輸入密碼的檔案與資訊檔;token 只會是 _PENDING_TOKEN 的格式(呼叫前已檢查,或由本程式產生)。"""
        folder = cfg.paths.uploads_path / PENDING_DIR
        return folder / f"{token}.pdf", folder / f"{token}.json"

    def drop_pending(token: str) -> None:
        for path in pending_files(token):
            path.unlink(missing_ok=True)

    def load_pending(token: str) -> dict[str, Any] | None:
        """token 對應的資訊;格式不對、不存在、資訊檔壞了或放超過 PENDING_TTL(順便刪掉)都回 None。"""
        if not _PENDING_TOKEN.fullmatch(token):
            return None
        pdf, meta = pending_files(token)
        info = _read_pending(meta)
        if info is None or not pdf.is_file() or time.time() - info["created"] > PENDING_TTL:
            drop_pending(token)
            return None
        return info

    def sweep_pending() -> None:
        """刪掉放超過 PENDING_TTL 的暫存檔,以及資訊檔不見的孤兒(每次上傳、開密碼頁時順便做)。"""
        folder = cfg.paths.uploads_path / PENDING_DIR
        if not folder.is_dir():
            return
        for path in folder.iterdir():
            token = path.stem
            if not _PENDING_TOKEN.fullmatch(token):
                continue
            info = _read_pending(folder / f"{token}.json")
            if info is None or time.time() - info["created"] > PENDING_TTL:
                drop_pending(token)

    def password_page(token: str, *, error: str | None = None, gone: bool = False,
                      status_code: int = 200) -> HTMLResponse:
        return page("upload_password.html", active="home", status_code=status_code,
                    v={"token": token, "error": error, "gone": gone})

    def _pending_missing() -> PageError:
        return PageError(404, MSG_PDF_MISSING, next_url="/", next_label="回首頁重新上傳",
                         hint="可能已經超過 30 分鐘、密碼錯太多次，或已經處理過了。請回首頁重新上傳一次。")

    def ask_first(ask: dict[str, str], *, action: str, back_url: str, active: str = "",
                  fields: dict[str, str] | None = None) -> HTMLResponse:
        """要先確認的動作沒帶確認欄位(沒有 JS,或 app.js 沒跑起來,確認框沒問到):不做,回確認頁(400)再問一次。

        ask 是 render 給的那一份(標題、說明、確定鍵、確認欄位的名稱),和確認框同樣的字;fields 是剛剛那張表單
        要原樣帶回的欄位(已經驗證過的值)。按「確定…」連同確認欄位再送一次才生效,「先不要」回 back_url。
        """
        carried = {**(fields or {}), ask["field"]: CONFIRMED}
        return page("ask.html", active=active, status_code=400,
                    ask={**ask, "action": action, "fields": carried, "back_url": back_url})

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

    # 沒有 Content-Length 的 POST 邊收邊數(見 _BodyLimit);最先加,在最內層
    app.add_middleware(_BodyLimit)

    @app.middleware("http")
    async def limit_body_size(request: Request, call_next):
        """Content-Length 已經超過上限就直接 413,不必先把整份收下來:上傳重畫首頁,其他表單回錯誤頁。"""
        path = request.url.path
        if request.method == "POST":
            length = request.headers.get("content-length", "")
            if length.isdigit() and int(length) > _body_limit(path):
                if path == "/upload":
                    return home(status_code=413, error=MSG_TOO_LARGE)
                exc = _too_large(path)
                return error_page(exc.status_code, exc.detail, exc)
        return await call_next(request)

    # 後加的 middleware 包在外層:security_headers → issue_csrf_token → limit_body_size → _BodyLimit → 路由。
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
    async def authenticate(request: Request, call_next):
        # 身分驗證先於 body-limit 重畫與所有路由；公鑰 I/O 不阻塞事件迴圈。
        try:
            identity = await run_in_threadpool(resolver.resolve, request)
        except IdentityError as exc:
            return error_page(403, str(exc))
        token = identity_context.set(identity)
        try:
            if request.method == "GET" and await run_in_threadpool(needs_notice, request.url.path):
                return RedirectResponse(f"/data-use?next={quote(request.url.path, safe='/')}", status_code=303)
            return await call_next(request)
        finally:
            identity_context.reset(token)

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

    def needs_notice(path: str = "") -> bool:
        """從外面登入的家庭成員還沒看過資料使用說明:頁面先轉到說明頁(靜態檔、健康檢查、說明頁本身不攔)。
        在這台電腦直接開的不攔(管理的人在設定);名單外的登入者只能看、不記同意。"""
        identity = identity_context.get()
        if identity is None or identity.local or path.startswith(_NOTICE_FREE):
            return False
        who = principal()
        return who.role in ("family", "elder") and f"{CONSENT_PREFIX}{who.actor}" not in store().get_settings()

    @app.get("/data-use", response_class=HTMLResponse)
    def data_use(next_url: str = Query("/", alias="next")):
        """資料使用說明:存了什麼、放在哪裡、會不會送出去、誰看得到、怎麼刪;第一次使用時下面多一顆「我看過了」。"""
        return page("data_use.html", active="settings", needs_ack=needs_notice(), next_url=_safe_next(next_url))

    @app.post("/data-use")
    def data_use_ack(next_url: str = Form("/", alias="next")) -> RedirectResponse:
        """記下誰、什麼時候看過說明(設定變更紀錄也留一筆),再回原本要去的頁面。"""
        if needs_notice():
            who = principal()
            store().set_setting(f"{CONSENT_PREFIX}{who.actor}", datetime.now().isoformat(timespec="seconds"),
                                actor=who.actor)
        return RedirectResponse(_safe_next(next_url), status_code=303)

    # ---- 健康檢查(部署用) --------------------------------------------------

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """給開機腳本與監看用:網頁活著就回 200;Ollama 連不上或資料庫寫不進去時照常回應,標成 degraded。

        只列狀態、版本、provider、模型名稱、模型有沒有載入(model_loaded,不適用時 null)與資料庫寫不寫得進去;
        不列路徑、主機位址、金鑰或其他設定值。provider 是目前生效的(設定頁可以切換辨識模式)。
        """
        current = current_cfg()
        ollama, loaded = _ollama_probe(current, ollama_transport)
        database = "ok" if store().writable() else "error"
        return JSONResponse({
            "status": "degraded" if ollama == "unreachable" or database != "ok" else "ok",
            "version": APP_VERSION,
            "provider": current.provider,
            "models": _model_names(current),
            "ollama": ollama,
            "model_loaded": loaded,
            "database": database,
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
        require("can_upload")
        started = time.monotonic()
        sweep_pending()
        # 兩個入口:相機(file)與相簿/電腦選檔(file_pick),用有選檔的那個
        chosen = next((f for f in (file, file_pick) if f is not None and f.filename), None)
        if chosen is None:
            return home(status_code=400, error=MSG_NO_FILE, selected=doc_type)
        # 只取副檔名做白名單比對;檔名本身完全不用
        ext = Path(chosen.filename.replace("\\", "/")).suffix.lower()
        if ext not in cfg.supported_extensions:
            return home(status_code=400, error=MSG_BAD_EXT, selected=doc_type)

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
            return home(status_code=code, error=error, selected=doc_type)
        # 存起來的原件不留拍攝地點與裝置(結果頁、匯出與這台電腦都看得到原件);送模型的那份另外再去一次
        strip_metadata(dest)

        # 類型只收首頁選項的值(kind_choice),其他當作沒選、只在本機辨識;藥袋與發票／收據一律只在本機。
        # 大類欄位只有還開著舊版首頁(先選大類)的表單才會送:照舊只收白名單,選了大類只收那一類清單上的名稱,
        # 當作家人選的文件(label;類型提示、要不要只在本機、要不要一律轉人工由 Pipeline 依固定清單決定)
        group = category if category in CATEGORIES else None
        name = kind_choice(group, doc_type)
        if ext == ".pdf" and pdf_needs_password(dest):
            # 加密的 PDF:先不辨識,原檔(還是加密的)放進 pending/,請家人到下一頁輸入密碼
            token = secrets.token_urlsafe(24)
            pdf, meta = pending_files(token)
            pdf.parent.mkdir(parents=True, exist_ok=True)
            dest.replace(pdf)
            meta.write_text(json.dumps({"created": time.time(), "tries": 0, "category": group, "name": name},
                                       ensure_ascii=False), encoding="utf-8")
            return RedirectResponse(url=f"/upload/password/{token}", status_code=303)
        return start_job(dest, group, name, started=started)

    @app.post("/upload/check")
    def upload_check(photo: UploadFile | None = File(None)) -> JSONResponse:
        """選好照片後的拍照檢查(src/photo_check.py):app.js 送縮小的照片,回 {"hint": 提示};沒偵測到是 null。

        權限與 CSRF 和 /upload 一樣。照片只在記憶體裡算:表單上限 1MB,Starlette 的上傳暫存不到 1MB 不寫磁碟;
        不存檔、不寫紀錄、不交給模型(雲端模式也一樣)。
        """
        require("can_upload")
        data = photo.file.read(MAX_FORM_BYTES + 1) if photo is not None else b""
        hint = photo_hint(data) if 0 < len(data) <= MAX_FORM_BYTES else None
        return JSONResponse({"hint": hint}, headers={"Cache-Control": "no-store"})

    # ---- 上傳後的進度(UX-3):四步照伺服器真的做完的亮;沒有 JS 時頁面每 3 秒重新整理 ----

    def progress_for(job) -> dict[str, Any]:
        """做完了 → {"next": 結果頁};處理失敗 → 錯誤頁;還在做 → 進度頁的畫面資料。"""
        snap = jobs.snapshot(job)
        if snap["failed"]:
            raise job_failed(job)
        if snap["doc_id"] is not None:
            return {"next": f"/doc/{snap['doc_id']}"}
        return progress_view(snap, token=job.token, provider=job.provider, is_pdf=job.is_pdf, label=job.label,
                             has_photo=job.photo is not None)

    @app.get("/upload/progress/{token}", response_class=HTMLResponse)
    def upload_progress(token: str) -> Response:
        require("can_upload")
        v = progress_for(get_job(token))
        if "next" in v:
            return RedirectResponse(url=v["next"], status_code=303)
        response = page("progress.html", active="home", v=v)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/upload/progress/{token}/status")
    def upload_progress_status(token: str) -> JSONResponse:
        """進度頁的 app.js 每秒問一次;只回步驟狀態與文字(和頁面同一份),做完了回結果頁的網址。"""
        require("can_upload")
        v = progress_for(get_job(token))
        body = {"next": v["next"]} if "next" in v else {
            "next": None, "say": v["say"],
            "steps": [{k: s[k] for k in ("id", "state", "state_text", "hint", "time")} for s in v["steps"]]}
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @app.get("/upload/progress/{token}/photo")
    def upload_progress_photo(token: str) -> Response:
        require("can_upload")
        photo = get_job(token).photo
        if photo is None:
            raise HTTPException(status_code=404, detail="找不到這張照片")
        return Response(photo, media_type="image/jpeg", headers=_FILE_HEADERS)

    # ---- 加密 PDF:輸入密碼(PDF-PW) ------------------------------------------

    @app.get("/upload/password/{token}", response_class=HTMLResponse)
    def pdf_password_form(token: str):
        require("can_upload")
        sweep_pending()
        if load_pending(token) is None:
            raise _pending_missing()
        return password_page(token)

    @app.post("/upload/password/{token}")
    def pdf_password_try(token: str, password: str = Form(""), decision: str = Form("unlock")) -> Response:
        """密碼對了就解開、照家人原本選的大類與名稱辨識;不對就再問(還剩幾次);錯太多次或按「先不要」就刪掉暫存檔。

        密碼只交給 decrypt_pdf 用一次:不寫進資訊檔、紀錄、錯誤頁,重畫的表單密碼欄是空的。
        """
        require("can_upload")
        started = time.monotonic()
        with pending_lock:   # 同一份的兩個請求不會同時讀寫「試了幾次」,也不會各解開一次
            info = load_pending(token)
            if info is None:
                raise _pending_missing()
            pdf, meta = pending_files(token)
            if decision == "cancel":
                drop_pending(token)
                return _redirect_with_msg("/", MSG_UPLOAD_CANCELLED)
            dest = cfg.paths.uploads_path / _new_upload_name(".pdf")
            if not decrypt_pdf(pdf, password, dest):
                tries = info["tries"] + 1
                if tries >= MAX_PASSWORD_TRIES:
                    drop_pending(token)
                    return password_page(token, error=MSG_PDF_GONE, gone=True, status_code=400)
                meta.write_text(json.dumps({**info, "tries": tries}, ensure_ascii=False), encoding="utf-8")
                return password_page(token, error=MSG_PDF_WRONG.format(left=MAX_PASSWORD_TRIES - tries),
                                     status_code=400)
            drop_pending(token)
        # 資訊檔在伺服器上,照樣再過一次白名單(和上傳時同一份)
        group = info["category"] if info["category"] in CATEGORIES else None
        return start_job(dest, group, kind_choice(group, info["name"] or UNSURE), started=started)

    # ---- 文件櫃(四大類) ----------------------------------------------------

    @app.get("/cabinet", response_class=HTMLResponse)
    def cabinet(cat: str | None = None, q: str = ""):
        """四大類的份數與文件;?cat= 只看一類(四大類或未分類),其他值當作沒選,依大類分組列出全部。
        ?q= 找文件:每個詞都要出現,只在這台電腦的資料庫裡找(render.search_documents)。"""
        v = cabinet_view(store().list_documents(limit=None), cat, cloud=cloud_mode(), query=q)
        return page("cabinet.html", active="cabinet", v=v)

    # ---- 統一發票對獎 --------------------------------------------------------
    # 家人照公告輸入號碼(存在設定裡,改的紀錄寫進設定變更);只比對號碼經 QR Code 核對相符的發票。
    # 領獎提醒由伺服器用存好的號碼重新比對才加,表單只帶文件編號與期別(src/lottery.py)

    def invoice_period(doc: dict[str, Any]) -> str | None:
        """發票屬於哪一期(看讀到的開立日期);讀不到日期回 None。"""
        try:
            return period_of(date.fromisoformat(str((doc.get("result") or {}).get("date") or "")))
        except ValueError:
            return None

    def lottery_page(period: str | None, *, values: Any = None, errors: dict[str, str] | None = None,
                     status_code: int = 200, msg: str | None = None) -> HTMLResponse:
        """對獎頁;沒指定(或格式不對)的期別用今天以前最近開過獎的那一期。"""
        groups: dict[str, list[dict[str, Any]]] = {}
        for doc in store().list_documents(doc_type="發票", limit=None):
            key = invoice_period(doc) if doc.get("action") != "failed" else None
            if key:
                groups.setdefault(key, []).append(doc)
        if parse_period(period) is None:
            period = latest_drawn(date.today())
        numbers = Numbers.from_json(store().get_settings().get(LOTTERY_PREFIX + period))
        reminded = {a["document_id"] for a in store().list_actions()
                    if not a["superseded"] and is_lottery_reminder(a, period)}
        who = principal()
        v = lottery_view(period, [(key, len(groups.get(key, []))) for key in sorted(set(groups) | {period}, reverse=True)],
                         groups.get(period, []), numbers, can_edit=who.permissions["can_manage"],
                         can_remind=who.permissions["can_remind"], reminded=reminded, values=values, errors=errors)
        return page("lottery.html", active="cabinet", status_code=status_code, msg=msg, v=v)

    @app.get("/lottery", response_class=HTMLResponse)
    def lottery(period: str | None = None, msg: str | None = None):
        return lottery_page(period, msg=msg)

    @app.post("/lottery")
    def lottery_save(form: FormData = Depends(read_form)) -> Response:
        """存一期的號碼與領獎期限(只有家人);格式不對回同一頁,留住剛填的值、寫出哪一欄不對。"""
        who = require("can_manage")
        period = form.get("period")
        if not isinstance(period, str) or parse_period(period) is None:
            raise PageError(400, MSG_INVALID, next_url="/lottery", next_label="回對獎")
        numbers, errors = parse_lottery_form(form, period)
        if numbers is None:
            return lottery_page(period, values=form, errors=errors, status_code=400)
        store().set_setting(LOTTERY_PREFIX + period, numbers.to_json(), actor=who.actor)
        return _redirect_with_msg(f"/lottery?period={period}", MSG_LOTTERY_SAVED)

    @app.post("/lottery/remind")
    def lottery_remind(doc_id: int = Form(...), period: str = Form(...)) -> Response:
        """可能中獎的發票加一筆領獎提醒(auto,可以在發票的結果頁取消);同一張同一期只加一次。

        表單只有文件編號與期別:獎項由伺服器用存好的號碼和經 QR 核對相符的發票號碼重新比對,送假的獎項沒有用;
        QR 記載有買方統編(打統編,不能兌獎)或看不出來的也不加。
        """
        back = f"/lottery?period={period}" if parse_period(period) else "/lottery"
        with doc_lock:      # 「有沒有加過」到寫入之間,不讓同一張的另一次按鈕插進來
            who = require("can_remind")
            doc = get_doc(doc_id)
            numbers = Numbers.from_json(store().get_settings().get(LOTTERY_PREFIX + period)) if back != "/lottery" else None
            number = verified_invoice_number(doc.get("result")) if doc.get("action") != "failed" else None
            if qr_buyer_has_tax_id(doc.get("result")) is not False:
                number = None
            hit = match(number, numbers) if numbers and number and invoice_period(doc) == period else None
            if hit is None:
                raise PageError(409, MSG_NO_PRIZE, next_url=back, next_label="回對獎",
                                hint="號碼可能改過了，或這張發票的號碼沒有經過 QR Code 核對、有打統編。"
                                     "請回對獎頁再看一次。")
            if any(is_lottery_reminder(a, period) for a in store().list_actions(document_id=doc_id)
                   if not a["superseded"]):
                return _redirect_with_msg(back, MSG_LOTTERY_ALREADY)
            action = reminder_action(number, hit, numbers, period)
            action_id = store().add_action(doc_id, action["kind"], action["tier"], action["payload"])
            store().add_audit_event("lottery_reminder", document_id=doc_id, action_id=action_id,
                                    detail={"period": period, "prize": hit.prize.name}, **actor_fields(who))
        return _redirect_with_msg(back, MSG_LOTTERY_REMINDED)

    # ---- 結果頁 ------------------------------------------------------------

    @app.get("/doc/{doc_id}", response_class=HTMLResponse)
    def document(doc_id: int, msg: str | None = None, changed: str | None = None):
        doc = get_doc(doc_id)
        path = allowed_file(doc)
        file_url = f"/doc/{doc_id}/file" if path else None
        # 被家人更正取代的舊行動不再列出(紀錄留在資料庫):這裡只放現在有效的提醒與服藥表
        actions = [action_view(a) for a in store().list_actions(document_id=doc_id) if not a["superseded"]]
        corrections = store().list_corrections(document_id=doc_id, verified_only=False, limit=ALL_EVENTS)
        retypes = store().list_audit_events(document_id=doc_id, event="retype", limit=1)
        v = doc_view(doc, file_url=file_url, hint=doc.get("doc_type_hint"), actions=actions,
                     file_size=_image_size(path), corrected=bool(corrections), duplicate=duplicate_of(doc),
                     original=ai_reading(doc, corrections, retypes),
                     prepared_url=f"/doc/{doc_id}/prepared" if path else None,
                     correct_url=f"/doc/{doc_id}/correct" if correctable(doc) and principal().permissions["can_correct"] else None,
                     retry_url=f"/doc/{doc_id}/retry" if path and principal().permissions["can_upload"] else None,
                     retype_url=(f"/doc/{doc_id}/retype" if path and correctable(doc)
                                 and principal().permissions["can_correct"] else None),
                     # 「處理狀態」要寫:等家人確認等了幾天、誰哪天標示已繳費或已辦理
                     records=store().list_actions(document_id=doc_id), names=member_names(), today=date.today(),
                     events=store().list_audit_events(document_id=doc_id, event="complete_reminder", limit=ALL_EVENTS))
        for action in actions:
            action["reject_ask"] = reject_action_ask(action["kind_label"])
        # 更正完接下一份(參考 Paperless 的「Save & next」):只給連結,不直接跳走,重新核對的結果要先給家人看
        next_review = None
        if msg == MSG_CORRECTED and principal().permissions["can_correct"]:
            waiting = [d for d in store().list_documents(action="review", limit=None) if d["id"] != doc_id]
            if waiting:
                next_review = {"href": f"/doc/{waiting[0]['id']}/correct", "count": len(waiting)}
        return page("doc.html", msg=msg, v=v, actions=actions, next_review=next_review,
                    changed=change_note(doc.get("result"), changed) if msg == MSG_CORRECTED else "",
                    audit_events=audit_rows(store().list_audit_events(document_id=doc_id)),
                    reject_document_url=f"/doc/{doc_id}/reject" if doc.get("action") == "review" else None,
                    reject_document_ask=REJECT_DOC_ASK, delete_document_url=f"/doc/{doc_id}/delete",
                    delete_document_ask=DELETE_DOC_ASK)

    @app.post("/doc/{doc_id}/retry")
    def retry_document(doc_id: int) -> Response:
        """模型沒回應而沒讀成的文件:原件放回上傳時的位置(檔名不變),照上傳時的選擇再讀一次,更新同一份文件。

        只有「模型沒回應」的失敗才能重讀(照片讀不出來的要重拍);已經在重讀(改成處理中了)、連按兩下就不再做。
        搬檔與改成處理中由 src.pipeline.start_redo 做:寫不進資料庫就把原件搬回去,中途重開由開機恢復接回來。
        """
        who = require("can_upload")
        started = time.monotonic()
        with doc_lock:
            doc = get_doc(doc_id)
            original = allowed_file(doc)
            if doc.get("action") != "failed" or not model_unavailable(doc.get("error")) or original is None:
                raise PageError(409, MSG_NO_RETRY, next_url=f"/doc/{doc_id}", next_label="回到這份文件",
                                hint="可能已經在重新辨識，或已經讀好了。")
            try:
                dest = start_redo(cfg, store(), doc_id, original)
            except (OSError, sqlite3.Error) as exc:     # 原件正被開著、資料庫忙碌:原件已經搬回去,文件沒變
                log.error("重新辨識沒有開始:文件 %s(%s)", doc_id, error_kind(exc))
                raise PageError(503, MSG_RETRY_NOT_STARTED, next_url=f"/doc/{doc_id}", next_label="回到這份文件",
                                hint=_RETRY_NOT_STARTED_HINT) from None
        # 上傳時的選擇照資料庫的紀錄,一樣只收白名單值:大類是四大類之一才算選了
        group = doc.get("category") if doc.get("category") in CATEGORIES else None
        name = doc.get("doc_label") if group else kind_choice(None, doc.get("doc_type_hint") or UNSURE)
        return start_job(dest, group, name, started=started, redo=Redo(doc_id, **actor_fields(who)))

    @app.get("/doc/{doc_id}/file")
    def document_file(doc_id: int):
        path = allowed_file(get_doc(doc_id))
        if path is None:
            raise HTTPException(status_code=404, detail="找不到原件")
        # 表上沒有的副檔名(config.yaml 另外加的)才交給主機猜
        return FileResponse(path, headers=_FILE_HEADERS, media_type=_MEDIA_TYPES.get(path.suffix.lower()))

    @app.get("/doc/{doc_id}/prepared")
    def document_prepared(doc_id: int):
        """系統送給 AI 讀的影像:用目前的設定對原件重跑一次前處理(src/preprocess.py),不存檔。

        看得到原件的人才看得到(同 /doc/{id}/file)。雲端讀的文件照雲端的設定產生(沒有細長單據的面積上限)。
        """
        doc = get_doc(doc_id)
        path = allowed_file(doc)
        if path is None:
            raise HTTPException(status_code=404, detail="找不到原件")
        options = cfg.preprocess
        if str((doc.get("result") or {}).get("source_model") or "").startswith("workers_ai:"):
            options = replace(options, tall_pixels=False)
        try:
            prepared = prepare_image(path, options=options)
        except Exception as exc:    # 原件壞了、PDF 還有密碼:沒有影像可以給,原件照樣看得到
            log.warning("處理後的影像產生失敗:#%s(%s)", doc_id, type(exc).__name__)
            raise HTTPException(status_code=404, detail="產生不了處理後的影像") from None
        return Response(prepared.data, media_type=prepared.mime_type, headers=_FILE_HEADERS)

    # ---- 改文件類型(FIN-HARDEN 第二輪) ---------------------------------------

    def retype_page(doc: dict[str, Any], *, error: str = "", status_code: int = 200) -> HTMLResponse:
        path = allowed_file(doc)
        v = retype_view(doc, file_url=f"/doc/{doc['id']}/file" if path else None, file_size=_image_size(path), error=error)
        return page("retype.html", status_code=status_code, v=v)

    @app.get("/doc/{doc_id}/retype", response_class=HTMLResponse)
    def retype_form(doc_id: int):
        require("can_correct")
        return retype_page(get_correctable(doc_id))

    @app.post("/doc/{doc_id}/retype")
    def retype_save(doc_id: int, doc_type: str = Form("")) -> Response:
        """家人改文件類型,照新類型在本機重讀:和更正表單分開,那裡一律不收類型(F5 的注入測例鎖住)。

        類型只收固定清單(DOC_TYPES);重讀在背景排隊,模型沒回應時文件與提醒都不動(review.retype_document)。
        """
        started = time.monotonic()
        with doc_lock:
            who = require("can_correct")
            doc = get_correctable(doc_id)
            if doc_type not in DOC_TYPES or doc_type == doc["result"].get("doc_type"):
                return retype_page(doc, error=MSG_RETYPE_CHOOSE, status_code=400)
            original = allowed_file(doc)
            if original is None:
                raise PageError(409, MSG_RETYPE_NO_ORIGINAL, next_url=f"/doc/{doc_id}", next_label="回到這份文件",
                                hint="要改類型得重讀原件；原件不在的話，請重新拍一張上傳。")
        current, analyzer, db = current_cfg(), get_analyzer(), store()

        def work(report: Callable[[str], None]) -> int:
            retype_document(current, db, analyzer, doc_id, doc_type, original, on_stage=report, **actor_fields(who))
            return doc_id

        # 重讀一律在本機:進度頁寫「在這台電腦讀字」,雲端模式也一樣
        reader = "ollama" if current.provider == "workers_ai" else current.provider
        return run_job(work, original, provider=reader, label=doc_type, started=started, failed_doc=doc_id)

    # ---- 更正讀值(F7) -----------------------------------------------------

    @app.get("/doc/{doc_id}/correct", response_class=HTMLResponse)
    def correct_form(doc_id: int):
        require("can_correct")
        return correct_page(get_correctable(doc_id))

    @app.post("/doc/{doc_id}/correct")
    def correct_save(doc_id: int, form: FormData = Depends(read_form)):
        """儲存更正並重新核對;填錯就回同一頁,已填的值留著、錯的欄位寫中文說明。

        「再加一種藥」也送到這裡:不存檔,只把表單多一種藥再畫一次(沒有 JS 也能用)。
        表單值一律當資料:只讀這個類型的欄位,種類與分級由程式決定(原則 3)。
        「這份文件屬於哪一類」也在這張表單:只收四大類與未分類,其他值不改類別;讀值存好後才寫入。
        連按兩下時,第二個請求等第一個做完才讀文件:讀值已經和表單一樣,只當成改類別,不會再搬一次原件。
        存好後回結果頁,網址帶 ?changed=(改了哪些欄位、舊提醒有沒有被取代;只有固定的鍵,見 render.change_note)。
        """
        with doc_lock:
            who = require("can_correct")
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
                if category == doc_category(doc):       # 什麼都沒改就按儲存:照實說,不留紀錄
                    return _redirect_with_msg(f"/doc/{doc_id}", MSG_NO_CHANGE)
                store().set_document_category(doc_id, category, **actor_fields(who))
                return _redirect_with_msg(f"/doc/{doc_id}", MSG_CATEGORY)
            # 更正會換掉這份文件還有效的提醒與服藥時間表:之前有才寫「舊提醒已取代」
            before = [a for a in store().list_actions(document_id=doc_id) if not a["superseded"]]
            correct_document(current_cfg(), store(), doc_id, changes, original=allowed_file(doc), **actor_fields(who))
            if category in ASSIGNABLE_CATEGORIES:
                store().set_document_category(doc_id, category, **actor_fields(who))
            after = [a for a in store().list_actions(document_id=doc_id) if not a["superseded"]]
            done = change_param(doc, changes, category, before=len(before), after=len(after))
            redirect = _redirect_with_msg(f"/doc/{doc_id}", MSG_CORRECTED)
            if done:   # 接在 msg 後面;結果頁只認固定的鍵
                redirect.headers["location"] += f"&changed={quote(done)}"
            return redirect

    @app.post("/doc/{doc_id}/reject")
    def reject(doc_id: int, confirmed: str | None = Form(None, alias=CONFIRM_REJECT_FIELD)) -> Response:
        """待複核的文件讀不了(例如影像品質太差):同一筆文件改成「讀不出來」,請長輩重新拍。
        退回在系統內沒有復原,要先確認:沒帶確認欄位就不退回,回確認頁再問一次。"""
        with doc_lock:
            who = require("can_reject")
            doc = get_doc(doc_id)
            if doc.get("action") != "review":
                raise PageError(409, "這份文件不在待複核", next_url=f"/doc/{doc_id}", next_label="回結果頁",
                                hint="已經存檔或讀不出來的文件不能在這裡退回。")
            if confirmed != CONFIRMED:
                return ask_first(REJECT_DOC_ASK, action=f"/doc/{doc_id}/reject", back_url=f"/doc/{doc_id}",
                                 active="review")
            reject_document(current_cfg(), store(), doc_id, original=allowed_file(doc), **actor_fields(who))
            return _redirect_with_msg("/review", MSG_REJECTED)

    @app.post("/doc/{doc_id}/delete")
    def delete_doc(doc_id: int, confirmed: str | None = Form(None, alias=CONFIRM_DELETE_FIELD)) -> Response:
        """家人刪除一份文件(照片、讀值、提醒、更正、操作紀錄一起刪,不能復原):沒帶確認欄位就不刪,回確認頁再問。"""
        with doc_lock:
            who = require("can_manage")
            doc = get_doc(doc_id)
            if confirmed != CONFIRMED:
                return ask_first(DELETE_DOC_ASK, action=f"/doc/{doc_id}/delete", back_url=f"/doc/{doc_id}",
                                 active="cabinet")
            delete_document(current_cfg(), store(), doc_id, original=allowed_file(doc), actor=who.actor)
            return _redirect_with_msg("/cabinet", MSG_DOC_DELETED)

    # ---- 取消/恢復提醒(F8) ------------------------------------------------

    @app.post("/reminder/{action_id}")
    def reminder_toggle(action_id: int, decision: str = Form(...)) -> RedirectResponse:
        """自動列入的期限提醒可以取消,也能恢復(原則 4:自動只給可取消的行動)。

        已經是想要的狀態(例如連按兩次「取消提醒」)就照樣回結果頁,不當成錯誤;
        要家人確認的行動不走這裡,在「家人確認」確認或退回。
        """
        if decision not in _REMINDER_DECISIONS:
            raise HTTPException(status_code=400, detail="只能選「取消提醒」或「恢復提醒」")
        with doc_lock:      # 檢查「有沒有被取代」到寫入之間,不讓同一份文件的更正插進來
            who = require("can_remind")
            action = get_action(action_id)
            if action["superseded"]:
                # 家人更正後舊提醒已被新的取代:恢復它會冒出兩個期限(例如從更正前開著的舊頁面按)
                raise PageError(409, "這個提醒已經換成新的了", next_url=f"/doc/{action['document_id']}",
                                next_label="回結果頁", hint="家人更正過這份文件，舊的提醒不再使用。請回結果頁看最新的提醒。")
            if not is_auto_reminder(action):
                # 走到這裡只會是要家人確認的事項(取消/恢復本身可以重複按,不會衝突)
                raise PageError(409, "這個事項要在「家人確認」處理",
                                hint="要家人點頭的事項不能在這裡取消，請到「家人確認」確認或退回。")
            status, msg = _REMINDER_DECISIONS[decision]
            store().set_action_status(action_id, status,
                                      event="cancel_reminder" if decision == REMINDER_CANCEL else "restore_reminder",
                                      **actor_fields(who))
            return _redirect_with_msg(f"/doc/{action['document_id']}", msg)

    @app.get("/help", response_class=HTMLResponse)
    def manual_page(q: str = ""):
        """使用手冊首頁(說明中心,使用者 11/5):搜尋手冊與主題清單。畫面上的說明都集中在這裡(docs/使用手冊.md,只有一份)。
        q 是搜尋字:每個詞都要出現、順序不拘,只在手冊的文字裡找。"""
        manual, query = load_manual(), q[:MANUAL_SEARCH_MAX].strip()
        return page("help.html", active="help", manual=manual, query=query, query_max=MANUAL_SEARCH_MAX,
                    hits=search_manual(manual, query) if query else ())

    @app.get("/help/{number}", response_class=HTMLResponse)
    def manual_topic(number: str):
        """使用手冊的一篇(一個大節);「給開發者」不放到網站上,找不到就是 404。"""
        manual = load_manual()
        topic = manual.topic(number)
        if topic is None:
            raise HTTPException(status_code=404, detail="找不到這一篇")
        at = manual.topics.index(topic)
        return page("help_topic.html", active="help", manual=manual, topic=topic,
                    prev=manual.topics[at - 1] if at else None,
                    next=manual.topics[at + 1] if at + 1 < len(manual.topics) else None)

    @app.get("/reminders", response_class=HTMLResponse)
    def reminders_all(msg: str | None = None):
        """「要記得的事」的全部列(首頁只放前幾筆):每一筆都有入口。"""
        docs, actions, names, today = store().list_documents(limit=None), store().list_actions(), member_names(), date.today()
        events = store().list_audit_events(event=("complete_reminder", "cancel_reminder"), limit=ALL_EVENTS)
        reminders = reminders_for(docs, actions, names)
        return page("reminders.html", active="home", msg=msg,
                    remind_groups=group_rows(reminders), reminders_total=len(reminders),
                    completed=completed_rows(docs, actions, events, today, names, days=None),
                    cancelled=cancelled_rows(docs, actions, events, names))

    @app.post("/rejected/{event_id}/seen")
    def rejected_seen(event_id: int, back: str = Form("")) -> RedirectResponse:
        """看過退回通知,按「已讀」收掉:只記一筆 seen_reject,不改文件與行動;連按兩次只記一次。"""
        with doc_lock:
            who = require("can_remind")
            event = store().get_audit_event(event_id)
            if event is None or event["event"] not in ("reject_document", "reject"):
                raise HTTPException(status_code=404, detail="找不到這個通知")
            seen = store().list_audit_events(event="seen_reject", limit=ALL_EVENTS)
            if not any((e.get("detail") or {}).get("event_id") == event_id for e in seen):
                store().add_audit_event("seen_reject", document_id=event.get("document_id"), action_id=event.get("action_id"),
                                        detail={"event_id": event_id}, **actor_fields(who))
        return _redirect_with_msg("/reminders" if back == "all" else "/", MSG_SEEN)

    @app.post("/reminder/{action_id}/done")
    def reminder_done(action_id: int, decision: str = Form(...), back: str = Form("")) -> RedirectResponse:
        """長輩或家人標示「已繳費/已辦理」,或改回:只是記錄讓家人知道,不付款、不查核。

        只收生效中的期限提醒(自動列入的,或家人確認過的);等家人確認、已取消、被更正取代的與服藥時間表都不收。
        已經是想要的狀態(連按兩次)照樣回原頁,不重複記操作紀錄。改回時自動提醒回到生效、家人確認過的回到已確認。
        back 是按的那一頁(doc 結果頁、all 全部清單,其他值都回首頁);只拿來選固定的位置,不當網址用。
        """
        if decision not in (DONE_COMPLETE, DONE_UNDO):
            raise HTTPException(status_code=400, detail="只能選「已繳費」或「改回」")
        with doc_lock:
            who = require("can_remind")
            action = get_action(action_id)
            if action["kind"] != "calendar" or action["superseded"]:
                raise PageError(409, "這個事項不能標成完成", next_url="/", next_label="回首頁",
                                hint="只有生效中的期限提醒可以標示「已繳費」或「已辦理」。")
            where = {"doc": f"/doc/{action['document_id']}", "all": "/reminders"}.get(back, "/")
            if decision == DONE_COMPLETE:
                if action["status"] != "completed":
                    if action["status"] not in (("pending", "done") if action["tier"] == "auto" else ("done",)):
                        raise PageError(409, "這個提醒還沒生效", next_url="/", next_label="回首頁",
                                        hint="還在等家人確認或已取消的提醒，不能標成完成。")
                    store().set_action_status(action_id, "completed", event="complete_reminder", **actor_fields(who))
                return _redirect_with_msg(where, MSG_REMINDER_DONE)
            if action["status"] == "completed":
                store().set_action_status(action_id, "pending" if action["tier"] == "auto" else "done",
                                          event="undo_complete", **actor_fields(who))
            return _redirect_with_msg(where, MSG_REMINDER_UNDONE)

    # ---- 家人確認 ----------------------------------------------------------

    def confirm_item(a: dict[str, Any]) -> dict[str, Any]:
        """確認頁那一件的畫面資料。"""
        doc = store().get_document(a["document_id"]) or {}
        view = action_view(a)
        view["reject_ask"] = reject_action_ask(view["kind_label"])   # 按「退回」前先問的話
        late = overdue_days(a, date.today())
        view["overdue"] = overdue_text(late) if late else ""
        view["waited"] = waited_days(a, date.today())
        view["doc_title"] = doc_title(doc) if doc else "文件"
        view["doc_when"] = fmt_when(doc.get("created_at")) if doc else ""
        # 卡片旁放原件照片方便對照;PDF 沒辦法當圖片顯示,只留「看原件與全文」連結
        path = allowed_file(doc) if doc else None
        view["thumb_url"] = (f"/doc/{a['document_id']}/file"
                             if path is not None and not is_pdf(path) else None)
        view["thumb_size"] = _image_size(path) if view["thumb_url"] else None
        return view

    @app.get("/confirm", response_class=HTMLResponse)
    def confirm_list(msg: str | None = None, item: str | None = None):
        """家人確認,一次一件(使用者 11/5):整頁只有一顆「確認」,確認或退回後回到這裡,顯示下一件。

        順序照行動編號;?item=<行動編號> 打開指定的那一件(結果頁的「去確認」)。指定的那一件已經不用確認
        (別人處理過、被更正取代),或網址上的值不對,就照實說並顯示第一件。item 只拿來和編號比對,不當其他用途。
        """
        pending = pending_confirms()
        at = next((i for i, a in enumerate(pending) if str(a["id"]) == item), None)
        gone = item is not None and at is None
        at = at or 0
        # 看下一件:長輩不能確認,沒有這個連結就只看得到第一件;最後一件接回第一件
        following = None
        if len(pending) > 1:
            following = {"id": pending[(at + 1) % len(pending)]["id"],
                         "label": "看下一件" if at + 1 < len(pending) else "回到第 1 件"}
        return page("confirm.html", active="confirm", msg=msg, item=confirm_item(pending[at]) if pending else None,
                    position=at + 1, total=len(pending), next_item=following, gone=gone)

    @app.post("/confirm/{action_id}")
    def confirm_decide(action_id: int, decision: str = Form(...),
                       confirmed: str | None = Form(None, alias=CONFIRM_REJECT_FIELD),
                       expected_status: str = Form("pending")) -> Response:
        if decision not in _CONFIRM_DECISIONS:
            raise HTTPException(status_code=400, detail="只能選「確認」或「退回」")
        with doc_lock:      # 兩位家人同時按同一個事項:先到的算數,另一位看到「已經處理過了」
            who = require("can_confirm" if decision == "done" else "can_reject")
            action = get_action(action_id)
            permitted_states = ("pending", "done") if decision == "rejected" else ("pending",)
            if (action["tier"] != "confirm" or action["status"] not in permitted_states
                    or action["superseded"] or action["status"] != expected_status):
                raise HTTPException(status_code=409, detail="這個事項不需要確認，或已經處理過了")
            if decision == "rejected" and confirmed != CONFIRMED:
                # 退回在系統內沒有復原,要先確認(「確認」不必多問):沒帶確認欄位就不退回,回確認頁再問一次
                return ask_first(reject_action_ask(action_view(action)["kind_label"]), action=f"/confirm/{action_id}",
                                 back_url=f"/confirm?item={action_id}", active="confirm",
                                 fields={"decision": decision, **({"expected_status": expected_status} if expected_status != "pending" else {})})
            status, msg = _CONFIRM_DECISIONS[decision]
            store().set_action_status(action_id, status, event="confirm" if decision == "done" else "reject",
                                      **actor_fields(who))
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
                      auto_threshold: str | None = Form(None),
                      confirmed: str | None = Form(None, alias=CONFIRM_CLOUD_FIELD)):
        """存全家共用的設定(辨識模式、門檻)。不合規則就回同一頁寫中文說明,什麼都不存;
        有改的每一項都留紀錄(何時、改成什麼、誰改的)。辨識模式改了,下一次上傳會重建 analyzer。
        從本機切到雲端備援要先確認(之後不敏感的文件會交給雲端模型讀):沒帶確認欄位就整份不存,
        回確認頁再問一次;切回本機、只改門檻都不必確認。"""
        require("can_manage")
        values = {PROVIDER: provider, THRESHOLD: auto_threshold}
        current = current_cfg()
        changes, errors = form_changes(values, cfg, current)
        if errors:
            return settings_page(status_code=400, values=values, errors=errors)
        if changes.get(PROVIDER) == "workers_ai" and confirmed != CONFIRMED:
            return ask_first(cloud_ask(current.local_only_doc_types), action="/settings", back_url="/settings",
                             active="settings", fields={k: v for k, v in values.items() if v is not None})
        with doc_lock:
            who = require("can_manage")
            for key, value in changes.items():
                store().set_setting(key, value, actor=who.actor)
        return _redirect_with_msg("/settings", MSG_SETTINGS_SAVED if changes else MSG_SETTINGS_UNCHANGED)

    # ---- 家庭成員；和文件操作用同一把鎖，角色撤銷後的請求重新檢查 ----

    def members_page(*, values=None, error=None, saved=False, status_code=200):
        """家庭成員頁。沒過的說明寫在那一欄正下方(表單規範):這個電子郵件已經在名單上,就寫在他那一塊,否則寫在「加入家庭成員」。
        member_values 的說明都是程式裡固定的字,看字就知道是哪一欄;其他(角色、至少保留一位家人)寫在角色下面。
        電子郵件不對時和離開欄位時說同一句話。"""
        members, values, text = store().list_members(), values or {}, error or ""
        listed = any(m["email"] == values.get("email") for m in members)
        field = "email" if "電子郵件" in text else "display_name" if "稱呼" in text else "role"
        return page("members.html", active="settings", members=members, values={} if listed else values,
                    error=MEMBER_EMAIL_ERROR if error and field == "email" else error, error_field=field,
                    error_email=values.get("email") if listed else "", email_rule=MEMBER_EMAIL_ERROR,
                    saved=saved, status_code=status_code)

    def last_family(email: str) -> bool:
        families = [m["email"] for m in store().list_members() if m["role"] == "family"]
        return families == [email]

    @app.get("/settings/members", response_class=HTMLResponse)
    def members_list(saved: bool = False):
        require("can_manage")
        return members_page(saved=saved)

    @app.post("/settings/members")
    def member_save(email: str = Form(""), role: str = Form(""), display_name: str = Form("")):
        with doc_lock:
            who = require("can_manage")
            values, error = member_values(email, role, display_name)
            if not error and role != "family" and last_family(values["email"]):
                error = "至少保留一位家人；請先加入另一位家人。"
            if error:
                return members_page(values=values, error=error, status_code=400)
            store().set_member(values["email"], role, values["display_name"], actor=who.actor)
        return RedirectResponse("/settings/members?saved=true", status_code=303)

    @app.post("/settings/members/remove")
    def member_remove(email: str = Form(""), confirmed: str = Form("")):
        with doc_lock:
            who = require("can_manage")
            member = store().get_member(email)
            if not member:
                raise HTTPException(404, "找不到這位家庭成員。")
            if last_family(member["email"]):
                return members_page(values={"email": member["email"]}, error="至少保留一位家人；請先加入另一位家人。",
                                    status_code=400)
            if confirmed != CONFIRMED:
                return ask_first({"title": "確定移除這位成員嗎？", "message": "移除後，這位成員只能查看，不能更正或確認。",
                                  "ok": "確定移除", "field": "confirmed"},
                                 action="/settings/members/remove", back_url="/settings/members",
                                 active="settings", fields={"email": member["email"]})
            store().remove_member(member["email"], actor=who.actor)
        return RedirectResponse("/settings/members?saved=true", status_code=303)

    @app.get("/settings/export")
    def settings_export() -> Response:
        """匯出全部紀錄:文件與讀值、行動、更正、設定變更(JSON 下載,不含影像)。內容有個資,不讓瀏覽器或代理存下來。"""
        require("can_manage")
        body = json.dumps(export_records(store(), cfg, version=APP_VERSION), ensure_ascii=False, indent=2)
        stamp = f"{datetime.now():%Y%m%d-%H%M}"
        # 中文檔名給認得 filename* 的瀏覽器,其他的用英數檔名
        disposition = (f'attachment; filename="records-{stamp}.json"; '
                       f"filename*=UTF-8''{quote(f'看有紀錄-{stamp}.json')}")
        return Response(body, media_type="application/json",
                        headers={"Content-Disposition": disposition, "Cache-Control": "no-store"})

    @app.post("/settings/delete")
    def settings_delete(request: Request,
                        confirmed: str | None = Form(None, alias=CONFIRM_PURGE_FIELD)) -> Response:
        """刪除全部資料:文件、行動、更正(SQLite)、處理紀錄 records.jsonl 與四個資料夾裡的原件;
        設定與設定紀錄保留,刪除本身也記一筆。app.log 只記類型與數量,不含讀值,不刪。
        刪了無法復原,要先確認:沒帶確認欄位就什麼都不刪,回確認頁再問一次。

        先清資料庫再刪檔案:檔案刪到一半卡住時,畫面上已經沒有指向它的文件。資料庫清不了就停在這裡
        (什麼都還沒刪;先刪照片的話,畫面上會留著指不到原件的文件),照實說、請家人稍後再試。
        資料庫清掉之後,每一步各自接住錯誤、其餘照做,刪除照樣記一筆:有照片檔刪不掉寫在回首頁的訊息裡;
        處理紀錄檔刪不掉(例如正被別的程式開著,Windows 刪不了)時,含讀值的檔還在,用錯誤頁寫清楚
        哪些已刪、哪些沒刪,關掉那個程式後再按一次就補刪。
        和改文件的路由同一把鎖:做到一半的更正先做完,不會在刪完之後又把讀值寫回來。
        """
        require("can_manage")
        if confirmed != CONFIRMED:
            return ask_first(PURGE_ASK, action="/settings/delete", back_url="/settings", active="settings")
        with doc_lock:
            who = require("can_manage")
            try:
                store().purge_all()
            except sqlite3.Error as exc:
                # 紀錄只寫例外類型:訊息原文可能帶這台電腦的完整路徑
                log.error("刪除全部資料:資料庫清不了(%s),沒有刪除任何資料", type(exc).__name__)
                raise PageError(503, MSG_PURGE_NOT_STARTED, next_url="/settings", next_label="回設定",
                                hint="資料庫正在忙，文件、照片與紀錄都還在。請稍後回設定頁再按一次"
                                     "「刪除全部資料」；還是不行，請找管理者。") from exc
            records_stuck = files_stuck = False
            try:
                clear_records(cfg.paths.logs)
            except OSError as exc:
                records_stuck = True
                log.warning("刪除全部資料:處理紀錄檔刪不掉(%s)", type(exc).__name__)
            try:
                files_stuck = delete_data_files(cfg)[1] > 0
            except OSError as exc:
                files_stuck = True
                log.warning("刪除全部資料:刪照片檔時出錯(%s)", type(exc).__name__)
            store().set_setting(PURGED, datetime.now().isoformat(), actor=who.actor)
        if records_stuck:
            gone = "文件、提醒與更正紀錄已經刪除" if files_stuck else "文件、提醒、更正紀錄與照片都已經刪除"
            still = "處理紀錄檔（裡面有讀值）" + ("和有些照片檔" if files_stuck else "")
            raise PageError(500, MSG_PURGE_STUCK, next_url="/settings", next_label="回設定",
                            hint=f"{gone}，但{still}刪不掉，可能正被別的程式開著。"
                                 "請管理者關掉那個程式後，回設定頁再按一次「刪除全部資料」。")
        return _redirect_with_msg("/", MSG_PURGED_PARTLY if files_stuck else MSG_PURGED)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    # 僅本機存取;對外一律經 Cloudflare Tunnel,不直接開放埠號
    uvicorn.run(app, host="127.0.0.1", port=8000)
