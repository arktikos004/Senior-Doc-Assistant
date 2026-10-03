"""處理管線:串接「辨識 → 驗證 → 決策 → 歸檔 → 行動 → 紀錄」。

驗證(src/verify)與行動(src/actions)各自只有一個入口函式,
Pipeline 只負責依序呼叫,不含它們的邏輯。其中「核對 → 決策 → 規劃行動」抽成
verify_decide_plan,上傳與家人更正讀值(src/review.py)走同一條。
"""
from __future__ import annotations

import logging
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any, Callable

from . import archiver
from .actions import plan_actions
from .config import AppConfig
from .decision import decide, decide_manual
from .models import IDENTITY_CATEGORY, SENSITIVE_CATEGORIES, Decision, ExtractionResult, catalog_entry, category_for
from .record_log import append_record, timestamp
from .store import Store
from .verify import verify_or_flag

log = logging.getLogger(__name__)


def verify_decide_plan(
    result: ExtractionResult,
    file_path: Path,
    cfg: AppConfig,
    received_on: date,
    decide_fn: Callable[[ExtractionResult, AppConfig], Decision] = decide,
) -> Decision:
    """核對 → 決策 → 規劃行動;上傳(Pipeline.process_file)與家人更正(src/review.py)共用這一條。

    - 核對一律讀原檔 file_path;核對程式出錯時驗證信心設成 0、轉人工(verify_or_flag,fail-closed)。
    - 決策預設是 decision.decide;家人更正改用自己的規則(review.decide_corrected),其餘步驟相同。
    - 行動寫進 result.actions,期限從 received_on(上傳日;更正時也是原本的上傳日)起算;
      規劃出錯時不產生行動,不讓文件跟著失敗。行動的種類與分級只看 doc_type 與決策(原則 3)。
    """
    verify_or_flag(result, file_path)
    decision = decide_fn(result, cfg)
    try:
        result.actions = plan_actions(result, decision, cfg, received_on=received_on)
    except Exception as exc:
        log.error("行動規劃失敗:%s(%s)", file_path.name, exc)
        result.actions = []
    return decision


class Pipeline:
    def __init__(self, cfg: AppConfig, analyzer, store: Store | None = None):
        self.cfg = cfg
        self.analyzer = analyzer
        self.store = store if store is not None else Store(cfg.paths.db_path)

    def process_file(self, file_path: Path, doc_type_hint: str | None = None, *,
                     category: str | None = None, label: str | None = None) -> dict[str, Any]:
        """處理單一文件,回傳這筆處理紀錄(同時寫入 log 與 SQLite)。

        doc_type_hint:使用者上傳時選的文件類型(可為 None),交給 provider
        做隱私分流與類型專屬提示詞。
        category:使用者上傳時選的大類(可為 None)。屬敏感大類(SENSITIVE_CATEGORIES)時要求 provider
        只在本機辨識,不論類型提示;文件歸哪一類由 category_for 決定(選了就照選的,沒選依辨識出的類型)。
        label:使用者在大類裡選的文件名稱(CATEGORY_DOCS,例如「稅單」「保單」;可為 None)。只認所選大類
        清單上的名稱(catalog_entry),其他當作沒選;選了就改用清單上的類型提示(稅單 → 帳單)。清單上沒有
        類型提示的(保單、存摺…初賽不能自動判讀)和身分證明整類一樣:只在本機辨識、不論讀成什麼都轉人工。
        大類與名稱只看使用者的選擇與固定清單,文件上的文字決定不了分流、歸類與決策(原則 3)。
        """
        log.info("開始處理:%s", file_path.name)

        entry = catalog_entry(category, label)
        if entry is not None:
            label, doc_type_hint = entry   # 名稱與類型提示一律取自固定清單
        else:
            label = None
        # 初賽不能自動判讀的(清單上沒有類型提示的項目、身分證明整類):只在本機辨識,一律轉人工
        manual = (entry is not None and entry[1] is None) or category == IDENTITY_CATEGORY

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
        except Exception as exc:  # 模型逾時、影像損毀、JSON 解析失敗等
            error = f"{type(exc).__name__}: {exc}"
            log.error("辨識失敗:%s(%s)", file_path.name, error)

        # 步驟 2–4:核對(要在搬檔前做,才讀得到原檔)→ 決策 → 規劃行動(提醒、服藥時間表…);
        # 辨識失敗沒有讀值可核對,直接記為 failed、不產生行動
        if result is not None:
            # 選了初賽不能自動判讀的文件:一律轉人工,原因寫家人選的名稱(行動也照轉人工的決策分級)
            decide_fn = partial(decide_manual, label=label) if manual else decide
            decision = verify_decide_plan(result, file_path, self.cfg, received_on=date.today(),
                                          decide_fn=decide_fn)
        else:
            decision = decide(None, self.cfg)

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
            # 搬移失敗(檔案被占用等):留在原地,記為 failed 但不搬移
            log.error("搬移檔案失敗:%s(%s)", file_path.name, exc)
            decision.action = "failed"
            decision.reason += f";搬移失敗:{exc}"
            target = file_path
            if result is not None:
                result.actions = []   # 失敗的文件不產生行動

        # 不記目標路徑:歸檔檔名含日期、商家、金額(路徑在 SQLite 的 target_path)
        log.info("處理完成:%s → [%s]", file_path.name, decision.action)

        # 步驟 6:寫入處理紀錄(時間只取一次,records.jsonl 與 SQLite 共用)
        record = {
            "時間": timestamp(),
            "原始檔案": file_path.name,
            "動作": decision.action,
            "原因": decision.reason,
            "目標路徑": str(target),
            "AI辨識結果": result.to_dict() if result else None,
            "錯誤": error,
            "使用者提示": doc_type_hint,
            "使用者選的文件": label,
            "類別": category_for(result.doc_type if result else None, category),
        }
        append_record(self.cfg.paths.logs, record)
        doc_id = self.store.add_document(record)
        record["文件ID"] = doc_id
        for action in (result.actions if result else []):
            self.store.add_action(doc_id, action["kind"], action["tier"], action.get("payload", {}))
        return record

    def process_folder(self, folder: Path) -> list[dict[str, Any]]:
        """一次處理資料夾內所有支援格式的檔案。"""
        records = []
        for f in sorted(folder.iterdir()):
            if f.is_file() and f.suffix.lower() in self.cfg.supported_extensions:
                records.append(self.process_file(f))
        return records
