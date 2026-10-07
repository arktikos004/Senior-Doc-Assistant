"""家人更正讀值與退回:人工的決定一律更新 SQLite 裡原本那筆文件(原則 8)。

Web 層(web/app.py)只負責 HTTP 與表單;這裡做「以原讀值為底套上家人填的值 → 重新核對(一律讀原檔)
→ 決策 → 重算行動 → 搬原檔 → 更新同一筆文件、換掉舊行動 → 寫更正紀錄 → records.jsonl 加一行」。
核對、決策與規劃和上傳走同一條(src.pipeline.verify_decide_plan),只有決策規則不同:
家人已對照原件更正,不再比驗證信心與門檻,程式核對沒有任何不通過就存檔(decide_corrected)。
表單值和模型讀到的文字一樣只是資料:行動種類與分級只看 doc_type 與決策結果(原則 3)。
"""
from __future__ import annotations

import copy
import logging
import re
import threading
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import archiver
from .archiver import SIDECAR_SUFFIX
from .config import AppConfig
from .models import COMMON_FIELDS, FIELD_LABELS, REQUIRED_FIELDS, Decision, ExtractionResult
from .pipeline import error_kind, verify_decide_plan
from .prompts import TYPE_FIELDS
from .record_log import append_record, timestamp
from .store import Store
from .verify import VERIFY_ERROR_SUMMARY

log = logging.getLogger(__name__)

CORRECTED_REASON = "家人更正後存檔"
REJECT_REASON = "影像品質不足"
SOURCE_CORRECT = "web_correct"   # records.jsonl 與 corrections 表上的來源
SOURCE_REJECT = "web_review"

# 家人能改的共通欄位;doc_type 不在內(類型決定行動種類,更正頁不改類型)。類型專屬欄位依 prompts.TYPE_FIELDS,
# 公文的 fields.deadline 不在其中:期限一律由程式從期限原文重算(原則 2)
_COMMON_CHANGES = tuple(name for name in COMMON_FIELDS if name != "doc_type")
# 更正紀錄只存讀值層(原件 → 讀值 → 衍生資料,原則 8);核對、白話解說與行動是衍生的
_READING_KEYS = ("doc_type", "date", "vendor", "amount", "currency", "invoice_number", "unreadable", "fields")
_DERIVED_FIELDS = ("deadline",)
# 原件不見時給核對用的檔名(本系統不會產生這種檔):讀不到影像,QR 一律「無法核對」
_NO_ORIGINAL = "（原件不存在）"
# 更正與退回都是「讀文件 → 核對 → 搬原檔 → 寫回」一整段,同一個行程裡一次只做一份:連按兩下時
# 第二個請求等第一個做完才開始,讀到的是更新後的文件(部署是單一 uvicorn 行程)
_LOCK = threading.Lock()


def correctable(doc: dict[str, Any] | None) -> bool:
    """有讀值、已存檔或待複核的文件才能更正;讀不出來的文件沒有讀值可改,要重新拍。"""
    return bool(doc) and isinstance(doc.get("result"), dict) and doc.get("action") in ("archive", "review")


def _field_key(name: str) -> str:
    """unreadable 裡的欄位名換成 changes 的寫法:類型專屬欄位寫 'due_date' 或 'fields.due_date' 都算同一欄。"""
    return name if name in COMMON_FIELDS or name.startswith("fields.") else f"fields.{name}"


def apply_changes(original: ExtractionResult, changes: dict[str, Any]) -> ExtractionResult:
    """以原讀值為底套上家人填的值,回傳新的讀值(原物件不動)。

    changes 的鍵同 REQUIRED_FIELDS 的寫法("amount"、"fields.due_date"),值已由表單轉好型別;
    None、空字串或空清單表示家人把這一欄清掉。表單沒有的欄位(例如發票的期別、統編)維持原讀值。
    只認這個類型會有的欄位,doc_type、fields.deadline 或其他任何鍵一律丟 ValueError。
    家人對照原件改過的讀值不再是模型的猜測,所以:自評信心設成 1.0(驗證信心仍由核對決定上限)、
    AI 的白話解說不保留(可能和更正後的期限不一致,畫面改用程式依欄位組的句子)、
    家人看過的欄位不再算「讀不清」。
    """
    allowed = {*_COMMON_CHANGES, *(f"fields.{name}" for name in TYPE_FIELDS.get(original.doc_type, ()))}
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise ValueError(f"不能更正的欄位：{'、'.join(unknown)}")
    result = copy.deepcopy(original)
    for key, value in changes.items():
        empty = value is None or value == "" or value == []
        if key.startswith("fields."):
            name = key.split(".", 1)[1]
            if empty:
                result.fields.pop(name, None)
            else:
                result.fields[name] = value
        else:
            setattr(result, key, None if empty else value)
    result.currency = result.currency or "NTD"
    result.confidence = 1.0
    result.plain_summary = ""
    result.unreadable = [name for name in result.unreadable if _field_key(name) not in changes]
    return result


def reading_unchanged(original: ExtractionResult, changes: dict[str, Any]) -> bool:
    """表單送來的讀值和原本一樣(家人只改了類別):不必重新核對,也不換掉行動。

    「讀不清」清單不比:家人看過的欄位會從清單拿掉,但讀值本身沒變。比不出來(例如型別清理後不同)就當有改,
    走一般的更正流程,不會漏掉真的更正。
    """
    def plain(result: ExtractionResult) -> dict[str, Any]:
        return {key: value for key, value in reading(result).items() if key != "unreadable"}
    return plain(apply_changes(original, changes)) == plain(original)


def failed_checks(result: ExtractionResult) -> list[str]:
    """核對結果裡不通過的檢查名稱(含與 QR 矛盾的);底線開頭的鍵是附註,不算。"""
    verification = result.verification if isinstance(result.verification, dict) else {}
    return [name for name, check in verification.items()
            if not name.startswith("_") and isinstance(check, dict) and check.get("status") == "fail"]


def decide_corrected(result: ExtractionResult, cfg: AppConfig) -> Decision:
    """家人更正後的決策(由上而下逐條檢查,命中即回傳):

    1. 文件類型不在目標範圍              → review(同 decision.decide)
    2. 核對程式出錯(fail-closed)         → review
    3. 任何一項檢查不通過(含與 QR 矛盾)  → review
    4. 還缺必要欄位                      → review(表單已經擋過,這裡是第二道)
    5. 全部通過                          → archive「家人更正後存檔」
    驗證信心與門檻是用來判斷「要不要人看」的;家人已對照原件看過,只剩程式核對能把它擋回待複核。
    """
    if result.doc_type not in cfg.target_doc_types:
        return Decision(action="review", reason=f"文件類型「{result.doc_type}」不在目標範圍，需人工判斷")
    verification = result.verification if isinstance(result.verification, dict) else {}
    if verification.get("_summary") == VERIFY_ERROR_SUMMARY:
        return Decision(action="review", reason=f"家人更正後{VERIFY_ERROR_SUMMARY}，需人工確認")
    failed = failed_checks(result)
    if failed:
        return Decision(action="review", reason=f"家人更正後仍有檢查不通過：{'、'.join(failed)}")
    missing = [FIELD_LABELS.get(name, name) for name in REQUIRED_FIELDS.get(result.doc_type, ())
               if not result.get_field(name) or name in result.unreadable]
    if missing:
        return Decision(action="review", reason=f"家人更正後仍缺少必要欄位：{'、'.join(missing)}")
    return Decision(action="archive", reason=CORRECTED_REASON)


def reading(result: ExtractionResult) -> dict[str, Any]:
    """讀值層(更正紀錄的 before/after):共通欄位、讀不清的欄位與類型專屬欄位,不含程式算的期限。"""
    data = result.to_dict()
    out = {key: data[key] for key in _READING_KEYS}
    out["fields"] = {k: v for k, v in data["fields"].items() if k not in _DERIVED_FIELDS}
    return out


def vendor_key(result: ExtractionResult) -> str | None:
    """同一個來源的鍵(寫進更正紀錄,W2-A 的約定):有賣方統編用統編,否則用正規化的商家名稱。"""
    fields = result.fields if isinstance(result.fields, dict) else {}
    tax_id = str(fields.get("seller_tax_id") or "").strip()
    if re.fullmatch(r"\d{8}", tax_id):
        return tax_id
    name = re.sub(r"\s+", "", unicodedata.normalize("NFKC", result.vendor or ""))
    return name or None


def _received_on(doc: dict[str, Any]) -> date:
    """文件的上傳日(created_at 的日期):更正後重算期限要從這天起算,不是更正當天。讀不懂就用今天。"""
    try:
        return datetime.fromisoformat(str(doc.get("created_at"))).date()
    except ValueError:
        return date.today()


def _inside(path: Path, folder: Path) -> bool:
    try:
        return path.resolve().is_relative_to(folder.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def _archived_as(path: Path, result: ExtractionResult, cfg: AppConfig) -> bool:
    """原檔已經在這份讀值該在的位置(資料夾與檔名同 archiver.archive_file;只差重複序號 -1、-2 也算)。"""
    folder = cfg.paths.archive / result.doc_type
    if cfg.group_by_month and result.date:
        folder = folder / result.date[:7]
    name = archiver.build_filename(result, cfg, "")   # 不含副檔名,例:20261001_帳單_範例電力公司_1286
    pattern = rf"{re.escape(name)}(-\d+)?{re.escape(path.suffix.lower())}"
    return path.parent.resolve() == folder.resolve() and re.fullmatch(pattern, path.name) is not None


def _recorded_original(cfg: AppConfig, doc: dict[str, Any]) -> Path | None:
    """資料庫現在記的原件位置;條件同結果頁送原件(在本系統的四個資料夾內、是支援的格式、檔案存在),不符合回 None。"""
    recorded = doc.get("target_path")
    if not recorded:
        return None
    try:
        path = Path(recorded).resolve()
        folders = [folder.resolve() for folder in (cfg.paths.archive, cfg.paths.review,
                                                   cfg.paths.failed, cfg.paths.uploads_path)]
    except (OSError, RuntimeError, ValueError):
        return None
    if not any(path != folder and path.is_relative_to(folder) for folder in folders):
        return None
    return path if path.suffix.lower() in cfg.supported_extensions and path.is_file() else None


def _original_now(cfg: AppConfig, doc: dict[str, Any], original: Path | None) -> Path | None:
    """原件現在的位置。original 是 web 層稍早讀文件時算的,可能已經過期(連按兩下「存檔並重新核對」時,
    前一個請求剛把原件改名搬走;那個檔名之後還可能被另一份文件用掉):資料庫現在記的是別的位置就以它為準。
    資料庫記的位置不能用,才看 original 還有沒有檔;也沒有就回 None(當作原件不存在)——不拿過期的位置
    去核對,也不把它寫回資料庫。web 層說原件不存在(None)就照樣是 None,這裡不自己去找。
    """
    if original is None:
        return None
    recorded = _recorded_original(cfg, doc)
    if recorded is not None:
        try:
            moved = original.resolve() != recorded
        except (OSError, RuntimeError, ValueError):
            moved = True
        if moved:
            return recorded
    return original if original.is_file() else None


def _drop_sidecar(original: Path, doc_id: int) -> None:
    """刪掉待複核原件旁的辨識結果檔。刪不掉(例如被別的程式開著)只記 log:原件已經搬好,新位置照樣要寫回。"""
    try:
        original.with_suffix(original.suffix + SIDECAR_SUFFIX).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("刪不掉待複核的辨識結果檔:文件 %s(%s)", doc_id, error_kind(exc))


def _relocate(cfg: AppConfig, doc_id: int, original: Path | None, result: ExtractionResult,
              decision: Decision) -> Path | None:
    """依更正後的決策搬原檔,回傳原檔現在的位置(沒有原檔、或搬不動而且原檔已經不在原位,回 None)。

    存檔:原檔在 review/ → 依更正後的欄位命名、移到 archive/,刪掉 sidecar;已在 archive/ → 檔名跟著
    更正後的欄位改。待複核:原檔在 archive/ → 移回 review/(附 sidecar);已在 review/ 就不動。
    原檔位置是衍生紀錄(原則 8):搬不動(例如檔案被占用)只記 log,文件照樣更新。
    回傳的位置會寫回資料庫,所以只回傳檔案真的在的位置;回 None 時資料庫記的位置不變。
    """
    if original is None:
        return None
    try:
        if decision.action == "archive" and _inside(original, cfg.paths.review):
            target = archiver.archive_file(original, result, cfg)
            _drop_sidecar(original, doc_id)
            return target
        if decision.action == "archive" and _inside(original, cfg.paths.archive):
            return original if _archived_as(original, result, cfg) else archiver.archive_file(original, result, cfg)
        if decision.action == "review" and _inside(original, cfg.paths.archive):
            return archiver.move_to_review(original, result, decision.reason, cfg)
    except OSError as exc:
        # 只寫文件編號與例外種類:已存檔的檔名有日期、商家與金額(藥袋的商家是醫療院所),例外訊息還帶完整路徑
        log.error("更正後搬移原檔失敗:文件 %s(%s)", doc_id, error_kind(exc))
    return original if original.exists() else None


def _record(doc: dict[str, Any], action: str, reason: str, target: Path | None,
            result: dict[str, Any] | None, source: str) -> dict[str, Any]:
    """人工決定的處理紀錄:records.jsonl 加一行、SQLite 更新同一筆文件,兩邊用同一份(時間只取一次)。"""
    return {
        "時間": timestamp(),
        "原始檔案": doc.get("source_file", ""),
        "動作": action,
        "原因": reason,
        "目標路徑": str(target) if target is not None else doc.get("target_path"),
        "AI辨識結果": result,
        "錯誤": None,
        "來源": source,
        "文件ID": doc["id"],
    }


def correct_document(cfg: AppConfig, store: Store, doc_id: int, changes: dict[str, Any],
                     original: Path | None) -> Decision:
    """家人更正一份文件的讀值,回傳更正後的決策(archive 或 review)。

    original 是原檔目前的位置(web 層已確認它在本系統的資料夾內;原件不見了是 None)。核對一律讀原檔:
    原件不在就沒有影像可讀,QR 一律「無法核對」,規則檢查照跑;不拿檔名去 review/ 猜,同名的檔
    可能是另一份文件。找不到文件丟 LookupError,讀不出來的文件(沒有讀值)丟 ValueError。
    original 可能已經過期(web 層讀完文件之後,同一份文件剛被另一個請求更正、原件搬走了):整段在鎖裡做,
    文件重新讀,資料庫現在記的是別的位置就以它為準(_original_now)。
    """
    with _LOCK:
        doc = store.get_document(doc_id)
        if doc is None:
            raise LookupError(f"找不到文件 {doc_id}")
        if not correctable(doc):
            raise ValueError("這份文件沒有可以更正的讀值")
        original = _original_now(cfg, doc, original)
        before = ExtractionResult.from_dict(doc["result"])
        result = apply_changes(before, changes)
        check_path = original if original is not None else cfg.paths.review / _NO_ORIGINAL
        decision = verify_decide_plan(result, check_path, cfg, received_on=_received_on(doc),
                                      decide_fn=decide_corrected, log_name=f"文件 {doc_id}")
        target = _relocate(cfg, doc_id, original, result, decision)
        record = _record(doc, decision.action, decision.reason, target, result.to_dict(), SOURCE_CORRECT)
        store.update_document(doc_id, record)
        store.replace_actions(doc_id, result.actions)
        # 驗證閘門(更正紀錄的 verified):沒有任何檢查不通過、核對有完成、必要欄位齊全,也就是能存檔
        store.add_correction(reading(before), reading(result), document_id=doc_id, doc_type=result.doc_type,
                             vendor_key=vendor_key(result), verified=decision.action == "archive",
                             source=SOURCE_CORRECT)
        append_record(cfg.paths.logs, record)
        # log 不記文件內容(藥袋的院所與藥名屬健康資料),內容以 SQLite 為準
        log.info("家人更正:文件 %s → [%s]", doc_id, decision.action)
        return decision


def reject_document(cfg: AppConfig, store: Store, doc_id: int, original: Path | None,
                    reason: str = REJECT_REASON) -> None:
    """家人判定這份讀不了(例如影像品質太差):同一筆文件改成「讀不出來」,請長輩重新拍。

    只接受待複核的文件(其他丟 ValueError,找不到丟 LookupError)。原檔在 review/ 就移到 failed/、刪 sidecar;
    這份文件的行動一併退場(等確認的期限提醒不再留在「家人確認」)。讀值留著,當作 AI 當初讀到什麼的紀錄。
    original 過期時的處理同 correct_document:整段在鎖裡做,資料庫現在記的是別的位置就以它為準。
    """
    with _LOCK:
        doc = store.get_document(doc_id)
        if doc is None:
            raise LookupError(f"找不到文件 {doc_id}")
        if doc.get("action") != "review":
            raise ValueError("只有等待複核的文件可以退回")
        original = _original_now(cfg, doc, original)
        target = original
        if original is not None and _inside(original, cfg.paths.review):
            try:
                target = archiver.move_to_failed(original, cfg)
            except OSError as exc:
                log.error("退回時搬移原檔失敗:文件 %s(%s)", doc_id, error_kind(exc))
                if not original.exists():
                    target = None   # 原檔已經不在原位:不把這個位置寫回(資料庫記的位置不變)
            else:
                _drop_sidecar(original, doc_id)
        record = _record(doc, "failed", f"人工複核判定退回：{reason}", target, doc.get("result"), SOURCE_REJECT)
        store.update_document(doc_id, record)
        store.replace_actions(doc_id, [])
        append_record(cfg.paths.logs, record)
        log.info("家人退回:文件 %s → [failed]", doc_id)
