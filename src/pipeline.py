"""處理管線:串接「收件 → 辨識 → 驗證 → 決策 → 歸檔 → 行動 → 紀錄」,以及開機時接回中斷的文件。

驗證(src/verify)與行動(src/actions)各自只有一個入口函式,
Pipeline 只負責依序呼叫,不含它們的邏輯。其中「核對 → 決策 → 規劃行動」抽成
verify_decide_plan,上傳與家人更正讀值(src/review.py)走同一條。

中斷與寫入失敗(FIN-HARDEN 第四輪):網頁收件時先建一筆「處理中」的文件(receive_document),讀完更新同一筆;
寫資料庫的那一步是一個交易(文件、類別、行動一起),寫不進去就把原件搬回原位。服務在半路重開,
開機時 recover_documents 把還在處理中的改成「模型沒回應」的失敗(畫面寫「還沒讀」,可以重新辨識),
搬到一半的原件依雜湊認回資料庫記的位置。SQLite 是權威,records.jsonl 交易成功後才寫(原則 8)。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, Collection

from . import archiver
from .actions import plan_actions
from .archiver import SIDECAR_SUFFIX
from .config import AppConfig
from .decision import decide, decide_manual
from .duplicates import file_sha256
from .models import IDENTITY_CATEGORY, SENSITIVE_CATEGORIES, Decision, ExtractionResult, catalog_entry, category_for
from .record_log import append_record, timestamp
from .settings import hide_local_paths
from .store import Store
from .verify import verify_or_flag

log = logging.getLogger(__name__)

# process_file 的 on_stage 會收到的步驟名稱(網頁進度頁「AI 照抄文字」「程式核對」兩步)
STAGE_READ, STAGE_CHECKED = "read", "checked"

# 收件後還沒讀完的文件(documents.action):原件在 uploads,還沒有讀值;讀完更新成 archive/review/failed
PROCESSING = "processing"
PROCESSING_REASON = "收到了，還在讀"
# 讀到一半中斷(服務重開,或讀完寫不進資料庫):照片沒問題,原件還在,當成模型沒回應,可以「重新辨識」
INTERRUPTED = "Interrupted"
INTERRUPTED_ERROR = f"{INTERRUPTED}: 處理到一半中斷，原件還在，可以重新辨識"
INTERRUPTED_REASON = "處理到一半中斷，請按「重新辨識」"
# 中斷之後原件也找不到了:沒有東西可以重讀,照實寫讀不出來,請重拍
MISSING_ERROR = "OriginalMissing: 處理到一半中斷，原件找不到了"
MISSING_REASON = "處理到一半中斷，原件找不到了，請重新拍一張"

# 模型沒回應(服務沒開、逾時、忙碌、伺服器出錯,或讀到一半中斷):不是照片的問題,原件留著,可以「重新辨識」。
# 看處理紀錄「錯誤」開頭的例外種類(process_file 寫成「種類: 訊息」)
MODEL_UNAVAILABLE_ERRORS = frozenset({
    "ConnectionError", "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    "TimeoutException", "TimeoutError", "RemoteProtocolError", "ReadError", "ResponseError", "WorkersAIError",
    INTERRUPTED,
})
REDO_EVENT = "retry_read"   # 重新辨識記在操作紀錄的種類(src.models.AUDIT_EVENTS)


def model_unavailable(error: Any) -> bool:
    """處理紀錄的錯誤是不是「模型沒回應」(照片沒問題,等一下再讀一次就好)。"""
    return isinstance(error, str) and error.split(":", 1)[0].strip() in MODEL_UNAVAILABLE_ERRORS


@dataclass(frozen=True)
class Redo:
    """重新辨識同一份文件(模型沒回應而沒讀成的;start_redo 已改成處理中):更新原本那一筆、編號不變,誰按的記進操作紀錄。"""
    doc_id: int
    actor: str | None = None
    role: str | None = None


@dataclass(frozen=True)
class Received:
    """收件時先建好的那一筆(receive_document,處理中):第一次讀完就更新它,不新增,也不記操作紀錄。"""
    doc_id: int


def error_kind(exc: BaseException) -> str:
    """例外的種類(不含訊息):類別名稱,作業系統的錯誤再加錯誤碼,例 "PermissionError,WinError 32"。

    給處理紀錄的原因與錯誤 log 用。OSError 的訊息帶完整路徑,歸檔檔名裡有日期、商家與金額
    (藥袋的商家是醫療院所),所以這些地方只寫種類,不寫 str(exc)。
    """
    name = type(exc).__name__
    if isinstance(exc, OSError):
        winerror = getattr(exc, "winerror", None)
        if winerror is not None:
            return f"{name},WinError {winerror}"
        if exc.errno is not None:
            return f"{name},errno {exc.errno}"
    return name


def verify_decide_plan(
    result: ExtractionResult,
    file_path: Path,
    cfg: AppConfig,
    received_on: date,
    decide_fn: Callable[[ExtractionResult, AppConfig], Decision] = decide,
    *,
    log_name: str | None = None,
) -> Decision:
    """核對 → 決策 → 規劃行動;上傳(Pipeline.process_file)與家人更正(src/review.py)共用這一條。

    - 核對一律讀原檔 file_path;核對程式出錯時驗證信心設成 0、轉人工(verify_or_flag,fail-closed)。
    - 決策預設是 decision.decide;家人更正改用自己的規則(review.decide_corrected),其餘步驟相同。
    - 行動寫進 result.actions,期限從 received_on(上傳日;更正時也是原本的上傳日)起算;
      規劃出錯時不產生行動,不讓文件跟著失敗。行動的種類與分級只看 doc_type 與決策(原則 3)。
    - 錯誤紀錄用 log_name 指這份文件,沒給就用 file_path 的檔名(上傳時是上傳檔名)。家人更正時原檔已經
      依欄位改名(日期、商家、金額;藥袋的商家是醫療院所),由呼叫端改給「文件 N」。例外只寫種類,不寫訊息。
    """
    verify_or_flag(result, file_path, log_name=log_name)
    decision = decide_fn(result, cfg)
    try:
        result.actions = plan_actions(result, decision, cfg, received_on=received_on)
    except Exception as exc:
        log.error("行動規劃失敗:%s(%s)", log_name or file_path.name, error_kind(exc))
        result.actions = []
    return decision


def _upload_choices(category: str | None, label: str | None,
                    doc_type_hint: str | None) -> tuple[str | None, str | None, bool]:
    """上傳時的選擇 → (家人選的文件名稱, 類型提示, 要不要交給家人確認);收件與讀完用同一份規則(見 process_file)。"""
    entry = catalog_entry(category, label)
    if entry is not None:
        label, doc_type_hint = entry   # 名稱與類型提示一律取自固定清單
    else:
        label = None
    # 交給家人確認的(清單上沒有類型提示的項目、身分證明整類):只在本機辨識,一律轉人工
    manual = (entry is not None and entry[1] is None) or category == IDENTITY_CATEGORY
    return label, doc_type_hint, manual


def _upload_day(doc: dict[str, Any]) -> date:
    """文件的上傳日(created_at 的日期):隔幾天才重新辨識,期限照樣從這天起算,不是讀完那天。讀不懂就用今天。"""
    try:
        return datetime.fromisoformat(str(doc.get("created_at"))).date()
    except ValueError:
        return date.today()


def receive_document(store: Store, file_path: Path, doc_type_hint: str | None = None, *,
                     category: str | None = None, label: str | None = None) -> int:
    """收件:上傳檔存好(已去掉拍攝資訊)就先建一筆「處理中」的文件,回傳文件編號;讀完由
    process_file(redo=Received(編號)) 更新同一筆,不再新增。排隊中或讀到一半服務重開,文件也找得到。

    上傳時的選擇照 process_file 的規則先記下(名稱與類型提示取自固定清單、大類照選的),重開之後重新辨識照樣用;
    原件雜湊收件時就算好,原件搬到一半就重開時,開機恢復靠它認回來。
    """
    label, doc_type_hint, _ = _upload_choices(category, label, doc_type_hint)
    try:
        sha256: str | None = file_sha256(file_path)
    except OSError as exc:
        log.warning("算不出原件雜湊:%s(%s)", file_path.name, error_kind(exc))
        sha256 = None
    return store.add_document({
        "時間": timestamp(), "原始檔案": file_path.name, "動作": PROCESSING, "原因": PROCESSING_REASON,
        "目標路徑": str(file_path), "AI辨識結果": None, "錯誤": None, "使用者提示": doc_type_hint,
        "使用者選的文件": label, "類別": category_for(None, category), "檔案雜湊": sha256,
    })


def start_redo(cfg: AppConfig, store: Store, doc_id: int, original: Path) -> Path:
    """重新辨識之前:原件放回 uploads(檔名不變),文件改成處理中、記下這個位置,回傳原件現在的位置。

    呼叫的人已經確認過這份是「模型沒回應」的失敗、原件還在(web 層在鎖裡做)。改成處理中之後再按一次就不收了
    (只收讀不出來的);讀到一半重開,開機恢復改回「還沒讀」。先搬檔再寫資料庫:寫不進去就把原件搬回去再丟例外;
    在這兩步之間重開,開機恢復從 uploads 把它搬回資料庫記的位置。
    """
    uploads = cfg.paths.uploads_path
    try:
        in_uploads = original.resolve().parent == uploads.resolve()
    except (OSError, RuntimeError, ValueError):
        in_uploads = False
    dest = original if in_uploads else archiver.unique_path(uploads / original.name)
    if dest != original:
        uploads.mkdir(parents=True, exist_ok=True)
        os.replace(original, dest)
    try:
        store.update_document(doc_id, {"動作": PROCESSING, "原因": PROCESSING_REASON, "目標路徑": str(dest),
                                       "AI辨識結果": None, "錯誤": None}, expect="failed")
    except Exception:
        put_back(dest, original, name=f"文件 {doc_id}")
        raise
    return dest


def _drop_sidecar(path: Path, name: str) -> None:
    """刪掉原件旁的待複核辨識結果檔(衍生紀錄,待複核讀 SQLite);刪不掉只記 log。"""
    try:
        path.with_suffix(path.suffix + SIDECAR_SUFFIX).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("刪不掉待複核的辨識結果檔:%s(%s)", name, error_kind(exc))


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except (OSError, RuntimeError, ValueError):
        return a == b


def put_back(moved: Path | None, original: Path | None, *, name: str, discard: bool = False) -> None:
    """資料庫沒寫進去:剛搬好的原件搬回原位(資料庫記的還是原位),搬過去時寫的待複核辨識結果檔也收掉。

    discard:文件已經不在了(讀的時候被家人刪掉):原件不搬回去,直接刪掉,重開也不會冒出來。
    盡力而為:搬不回去只記 log(name 是「文件 N」,不寫檔名),下次開機 recover_documents 依雜湊認回來。
    原位已經有別的檔就不蓋過去。
    """
    if moved is None or original is None or _same_file(moved, original):
        return
    try:
        if discard:
            moved.unlink(missing_ok=True)
        elif original.exists():
            log.error("原件沒能搬回原位(原位已經有檔):%s", name)
            return
        else:
            os.replace(moved, original)
    except OSError as exc:
        log.error("原件沒能搬回原位:%s(%s)", name, error_kind(exc))
        return
    _drop_sidecar(moved, name)


def append_record_safely(cfg: AppConfig, record: dict[str, Any], name: str) -> None:
    """records.jsonl 加一行(資料庫的交易成功之後才呼叫):它是衍生的流水帳,寫不進去只記 log,不讓文件跟著失敗(原則 8)。"""
    try:
        append_record(cfg.paths.logs, record)
    except Exception as exc:
        log.warning("處理紀錄沒寫進 records.jsonl:%s(%s)", name, error_kind(exc))


class Pipeline:
    def __init__(self, cfg: AppConfig, analyzer, store: Store | None = None):
        self.cfg = cfg
        self.analyzer = analyzer
        self.store = store if store is not None else Store(cfg.paths.db_path)

    def process_file(self, file_path: Path, doc_type_hint: str | None = None, *,
                     category: str | None = None, label: str | None = None,
                     on_stage: Callable[[str], None] | None = None,
                     redo: Redo | Received | None = None) -> dict[str, Any]:
        """處理單一文件,回傳這筆處理紀錄(同時寫入 log 與 SQLite)。

        doc_type_hint:使用者上傳時選的文件類型(可為 None),交給 provider
        做隱私分流與類型專屬提示詞。
        category:使用者上傳時選的大類(可為 None)。屬敏感大類(SENSITIVE_CATEGORIES)時要求 provider
        只在本機辨識,不論類型提示;文件歸哪一類由 category_for 決定(選了就照選的,沒選依辨識出的類型)。
        label:使用者在大類裡選的文件名稱(CATEGORY_DOCS,例如「稅單」「保單」;可為 None)。只認所選大類
        清單上的名稱(catalog_entry),其他當作沒選;選了就改用清單上的類型提示(稅單 → 帳單)。清單上沒有
        類型提示的(保單、存摺…交給家人確認)和身分證明整類一樣:只在本機辨識、不論讀成什麼都轉人工。
        大類與名稱只看使用者的選擇與固定清單,文件上的文字決定不了分流、歸類與決策(原則 3)。
        on_stage:每做完一步就呼叫一次(網頁的進度頁用):STAGE_READ 辨識成功、STAGE_CHECKED 核對與決策做完;
        辨識失敗不呼叫 STAGE_READ。只在真的做完之後才呼叫,畫面不會先亮。
        redo:更新既有的那一筆、換掉它的行動,不新增文件。Received 是收件時先建好的處理中那一筆(receive_document),
        Redo 是重新辨識(start_redo 已把原件放回上傳時的位置、改成處理中)。兩種都只寫回還在處理中的文件
        (讀的時候被刪掉就不寫),期限從原本的上傳日起算。沒給是舊入口(資料夾監控、命令列):新增一筆。
        寫入:文件、類別與行動(重新辨識再加誰按的)在同一個交易,不會只寫一半;寫不進去時原件搬回處理前的位置、
        例外往外丟。records.jsonl 是衍生的流水帳,交易成功後才寫,寫不進去只記 log。
        """
        log.info("開始處理:%s", file_path.name)
        report = on_stage or (lambda stage: None)
        label, doc_type_hint, manual = _upload_choices(category, label, doc_type_hint)
        existing = self.store.get_document(redo.doc_id) if redo is not None else None
        if redo is not None and existing is None:
            raise LookupError(f"找不到文件 {redo.doc_id}")   # 排隊時被刪掉了:不讀
        received_on = _upload_day(existing) if existing is not None else date.today()

        # 步驟 1:AI 辨識
        result: ExtractionResult | None = None
        error: str | None = None
        try:
            result = self.analyzer.analyze(file_path, doc_type_hint=doc_type_hint,
                                           local_only=manual or category in SENSITIVE_CATEGORIES)
            # log 不記文件內容(商家、金額、日期):藥袋的商家是醫療院所,屬健康資料;內容以 SQLite 為準
            log.info(
                "辨識結果:類型=%s 自評信心=%.2f 模型=%s",
                result.doc_type, result.confidence, result.source_model or "-",
            )
            report(STAGE_READ)
        except Exception as exc:  # 模型逾時、影像損毀、JSON 解析失敗等
            # 例外訊息可能帶這台電腦的完整路徑(例如開檔失敗);錯誤會進處理紀錄、資料庫與匯出,只留相對位置
            error = f"{type(exc).__name__}: {hide_local_paths(str(exc), self.cfg)}"
            log.error("辨識失敗:%s(%s)", file_path.name, error)

        # 步驟 2–4:核對(要在搬檔前做,才讀得到原檔)→ 決策 → 規劃行動(提醒、服藥時間表…);
        # 辨識失敗沒有讀值可核對,直接記為 failed、不產生行動
        if result is not None:
            # 選了交給家人確認的文件:一律轉人工,原因寫家人選的名稱(行動也照轉人工的決策分級)
            decide_fn = partial(decide_manual, label=label) if manual else decide
            decision = verify_decide_plan(result, file_path, self.cfg, received_on=received_on,
                                          decide_fn=decide_fn)
            report(STAGE_CHECKED)
        else:
            decision = decide(None, self.cfg)

        # 原件雜湊(重複上傳提示用):搬檔前算;讀不到就不記,不讓文件跟著失敗
        try:
            sha256: str | None = file_sha256(file_path)
        except OSError as exc:
            log.warning("算不出原件雜湊:%s(%s)", file_path.name, error_kind(exc))
            sha256 = None

        # 步驟 5:依決策歸檔 / 移至待確認 / 移至失敗
        try:
            if decision.action == "archive":
                target = archiver.archive_file(file_path, result, self.cfg)
            elif decision.action == "review":
                target = archiver.move_to_review(
                    file_path, result, decision.reason, self.cfg
                )
            else:
                target = archiver.move_to_failed(file_path, self.cfg)
        except Exception as exc:
            # 搬移失敗(檔案被占用等):留在原地,記為 failed 但不搬移。
            # log 與原因都只寫例外的種類:OSError 的訊息帶來源與目的地的完整路徑,目的地檔名有日期、商家與金額
            log.error("搬移檔案失敗:%s(%s)", file_path.name, error_kind(exc))
            decision.action = "failed"
            decision.reason += f"；搬移失敗（{error_kind(exc)}）"
            target = file_path
            if result is not None:
                result.actions = []   # 失敗的文件不產生行動

        # 不記目標路徑:歸檔檔名含日期、商家、金額(路徑在 SQLite 的 target_path)
        log.info("處理完成:%s → [%s]", file_path.name, decision.action)

        # 步驟 6:寫入處理紀錄(時間只取一次,records.jsonl 與 SQLite 共用;第一次讀完沿用收件時間,匯入舊紀錄才認得出來)
        record = {
            "時間": existing["created_at"] if isinstance(redo, Received) else timestamp(),
            "原始檔案": file_path.name,
            "動作": decision.action,
            "原因": decision.reason,
            "目標路徑": str(target),
            "AI辨識結果": result.to_dict() if result else None,
            "錯誤": error,
            "使用者提示": doc_type_hint,
            "使用者選的文件": label,
            "類別": category_for(result.doc_type if result else None, category),
            "檔案雜湊": sha256,
        }
        doc_id = self._save(record, result.actions if result else [], redo, file_path, target)
        record["文件ID"] = doc_id
        append_record_safely(self.cfg, record, f"文件 {doc_id}")
        return record

    def _save(self, record: dict[str, Any], actions: list[dict[str, Any]], redo: Redo | Received | None,
              file_path: Path, target: Path) -> int:
        """寫進資料庫,回傳文件編號:文件、類別、行動(重新辨識再加誰按的)在同一個交易,不會文件有了、提醒少了。

        寫不進去就整個撤回,原件從 target 搬回 file_path(資料庫記的還是那裡),再丟例外;
        文件在讀的時候被刪掉了(只更新處理中的那一筆,所以寫不進去),讀完的原件不搬回去、直接刪掉。
        """
        try:
            with self.store.transaction():
                if redo is None:
                    doc_id = self.store.add_document(record)
                    for action in actions:
                        self.store.add_action(doc_id, action["kind"], action["tier"], action.get("payload", {}))
                    return doc_id
                # 第一次讀完不記操作紀錄;重新辨識記誰按的,「從」寫原本的讀不出來(文件先改成了處理中)
                retry = isinstance(redo, Redo)
                self.store.update_document(redo.doc_id, record, expect=PROCESSING,
                                           event=REDO_EVENT if retry else None, was="failed",
                                           actor=getattr(redo, "actor", None), role=getattr(redo, "role", None))
                self.store.set_document_category(redo.doc_id, record["類別"])   # 沒選大類的,讀成功後依類型歸類
                self.store.replace_actions(redo.doc_id, actions)
                return redo.doc_id
        except Exception:
            put_back(target, file_path, name=f"文件 {redo.doc_id}" if redo else file_path.name,
                     discard=redo is not None and _deleted(self.store, redo.doc_id))
            raise

    def process_folder(self, folder: Path) -> list[dict[str, Any]]:
        """一次處理資料夾內所有支援格式的檔案。"""
        records = []
        for f in sorted(folder.iterdir()):
            if f.is_file() and f.suffix.lower() in self.cfg.supported_extensions:
                records.append(self.process_file(f))
        return records


def _deleted(store: Store, doc_id: int) -> bool:
    """這份文件已經不在資料庫裡了嗎(讀的時候被家人刪掉);查不了(資料庫壞了)當作還在。"""
    try:
        return store.get_document(doc_id) is None
    except Exception:
        return False


# ---- 開機恢復:接回上次中斷的文件 ----------------------------------------------------------------

def _within_folders(cfg: AppConfig, path: Path) -> bool:
    """path(展開後)在原件的四個資料夾之一裡面,而且是原件的格式(同結果頁只送這四處的原件)。"""
    try:
        resolved = path.resolve()
        roots = [folder.resolve() for folder in (cfg.paths.archive, cfg.paths.review, cfg.paths.failed,
                                                 cfg.paths.uploads_path)]
    except (OSError, RuntimeError, ValueError):
        return False
    return (path.suffix.lower() in cfg.supported_extensions
            and any(resolved != root and resolved.is_relative_to(root) for root in roots))


def _stored_original(cfg: AppConfig, doc: dict[str, Any]) -> Path | None:
    """資料庫記的原件位置還有檔就回傳;沒有記、不在四個資料夾內或檔不在回 None。"""
    target = doc.get("target_path")
    if not target:
        return None
    path = Path(target)
    return path if _within_folders(cfg, path) and path.is_file() else None


def _is_link(path: Path) -> bool:
    """符號連結,或 Windows 的 junction(資料夾連結):一律不走進去、不當成原件。"""
    return path.is_symlink() or os.path.isjunction(path)


def _files_in(folder: Path, *, deep: bool) -> list[Path]:
    """資料夾裡的檔,照名稱排(deep 才往下走;uploads 只看最上層,等輸入密碼的 pending/ 不算)。"""
    if not folder.is_dir() or _is_link(folder):
        return []
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(folder):
        here = Path(dirpath)
        dirnames[:] = sorted(name for name in dirnames if deep and not _is_link(here / name))
        found.extend(here / name for name in sorted(filenames) if not _is_link(here / name))
    return found


class _LooseFiles:
    """四個資料夾裡沒有任何文件指到的原件檔:搬了檔、資料庫還沒寫就中斷時,原件就在這裡面。

    依原件雜湊認(收件時就算好了),沒有雜湊的舊文件依上傳時的檔名或資料庫記的檔名認;認走的不再給別份。
    uploads 排最前面:重新辨識搬到一半的在那裡。待複核的辨識結果檔、.gitkeep 不是原件的格式,不算。
    """

    def __init__(self, cfg: AppConfig, docs: list[dict[str, Any]]):
        taken: set[Path] = set()
        for doc in docs:
            if doc.get("target_path"):
                try:
                    taken.add(Path(doc["target_path"]).resolve())
                except (OSError, RuntimeError, ValueError):
                    pass
        self.files = [path for folder, deep in ((cfg.paths.uploads_path, False), (cfg.paths.failed, False),
                                                (cfg.paths.review, False), (cfg.paths.archive, True))
                      for path in _files_in(folder, deep=deep)
                      if path.suffix.lower() in cfg.supported_extensions and path.resolve() not in taken]
        self._digests: dict[Path, str | None] = {}

    def _digest(self, path: Path) -> str | None:
        if path not in self._digests:
            try:
                self._digests[path] = file_sha256(path)
            except OSError:
                self._digests[path] = None
        return self._digests[path]

    def take(self, doc: dict[str, Any]) -> Path | None:
        """這份文件的原件(認走了就從清單拿掉);找不到回 None。"""
        sha256 = doc.get("file_sha256")
        names = {doc.get("source_file"), Path(str(doc.get("target_path") or "")).name} - {None, ""}
        for path in self.files:
            same = self._digest(path) == sha256 if sha256 else path.name in names
            if same:
                self.files.remove(path)
                return path
        return None


def _put_where_recorded(cfg: AppConfig, doc: dict[str, Any], found: Path) -> Path:
    """把認回來的原件搬回資料庫記的位置(SQLite 是權威),回傳原件現在的位置;那裡不能放(不在四個資料夾內、
    已經有別的檔、搬不動)就留在原地。搬過去時寫的待複核辨識結果檔一律收掉(它不是權威,待複核讀 SQLite)。"""
    wanted = Path(doc["target_path"])
    name = f"文件 {doc['id']}"
    try:
        if _within_folders(cfg, wanted) and not wanted.exists():
            wanted.parent.mkdir(parents=True, exist_ok=True)
            os.replace(found, wanted)
            _drop_sidecar(found, name)
            return wanted
    except OSError as exc:
        log.warning("原件搬不回資料庫記的位置:%s(%s)", name, error_kind(exc))
    _drop_sidecar(found, name)
    return found


def _recover_one(cfg: AppConfig, store: Store, doc: dict[str, Any], loose: _LooseFiles) -> str | None:
    """一份文件:原件不在資料庫記的位置就認回來;還在處理中的改成失敗(原件還在是「還沒讀」,不在是「讀不出來」)。
    回傳做了什麼(interrupted / missing / relinked,log 計數用),沒事回 None。"""
    path = _stored_original(cfg, doc)
    relinked = False
    if path is None and doc.get("target_path"):
        found = loose.take(doc)
        if found is not None:
            path, relinked = _put_where_recorded(cfg, doc, found), True
            if doc.get("action") != PROCESSING and path != Path(doc["target_path"]):
                store.set_target_path(doc["id"], str(path), was=doc["target_path"])   # 搬不回去:改記現在的位置
    if doc.get("action") != PROCESSING:
        return "relinked" if relinked else None
    record = {"動作": "failed", "目標路徑": str(path) if path is not None else doc.get("target_path"),
              "AI辨識結果": None, "原因": INTERRUPTED_REASON if path is not None else MISSING_REASON,
              "錯誤": INTERRUPTED_ERROR if path is not None else MISSING_ERROR}
    try:
        store.update_document(doc["id"], record, expect=PROCESSING)
    except LookupError:
        return None   # 剛好讀完或被刪掉了:不動
    return "interrupted" if path is not None else "missing"


def recover_documents(cfg: AppConfig, store: Store, *, busy: Collection[int] = ()) -> dict[str, int]:
    """開機時接回上次中斷的文件(網頁啟動時呼叫;重開之前的背景工作都已經不在了),回傳各做了幾份。

    1. 還在處理中的(排隊或讀到一半就重開;busy 是這個行程裡還在做的,不算):改成「模型沒回應」的失敗
       (INTERRUPTED),畫面寫「還沒讀」、可以重新辨識。不自動重跑:行動等重新辨識讀完才在那個交易裡寫,不會重複。
    2. 原件不在資料庫記的位置(搬了檔、資料庫還沒寫就重開;上傳、重新辨識、更正、改類型、退回都可能):從四個資料夾裡
       沒有文件指到的檔認回來(_LooseFiles),搬回資料庫記的位置;搬不回去才改記現在的位置。處理中的文件找不到原件,
       改成讀不出來(請重拍);其他文件找不到就不動。
    沒有任何文件指到的上傳檔不會自己變成文件(可能是家人刪文件時剛好刪不掉的),也不刪,只記個數。
    log 只記數量,不記檔名:已存檔的檔名有日期、商家與金額。
    """
    docs = store.list_documents(limit=None)
    loose = _LooseFiles(cfg, docs)
    counts = dict.fromkeys(("interrupted", "missing", "relinked"), 0)
    for doc in docs:
        if doc.get("action") == PROCESSING and doc["id"] in busy:
            continue
        done = _recover_one(cfg, store, doc, loose)
        if done:
            counts[done] += 1
    stray = sum(1 for path in loose.files if path.parent == cfg.paths.uploads_path)
    if any(counts.values()) or stray:
        log.info("開機恢復:%d 份改成還沒讀、%d 份找不到原件、%d 份的原件認回來;uploads 有 %d 個沒有文件的檔(不動)",
                 counts["interrupted"], counts["missing"], counts["relinked"], stray)
    return counts


def interrupt_document(cfg: AppConfig, store: Store, doc_id: int) -> None:
    """背景工作丟了例外、文件還在處理中(例如讀完寫不進資料庫):照開機恢復改成「還沒讀」,原件還在就能重新辨識。
    這一步也失敗只記 log(資料庫還是寫不進去),下次開機 recover_documents 會再做一次。"""
    try:
        doc = store.get_document(doc_id)
        if doc is not None and doc.get("action") == PROCESSING:
            _recover_one(cfg, store, doc, _LooseFiles(cfg, store.list_documents(limit=None)))
    except Exception as exc:
        log.error("沒能把文件 %s 改成還沒讀(%s)", doc_id, error_kind(exc))
