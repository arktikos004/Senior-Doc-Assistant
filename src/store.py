"""SQLite 儲存層:文件、行動、人工更正。

SQLite 是文件、行動與更正狀態的單一事實來源(原則 8);records.jsonl 只是同一份處理紀錄的
衍生流水帳(人看得懂、可 grep),新功能一律讀這裡、不讀 jsonl。家人更正或退回時更新原本那筆文件
(update_document),不新增一筆;這份文件的舊行動換成新的(replace_actions)。corrections 表在
家人更正讀值時寫入(F7),記下更正前後的讀值與是否通過核對(verified)。每次呼叫開一條新連線,
FastAPI 的執行緒池與 CLI 都能安全共用同一個檔案。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import CATEGORIES, UNCATEGORIZED

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    source_file  TEXT NOT NULL,
    action       TEXT NOT NULL,              -- archive / review / failed
    reason       TEXT,
    target_path  TEXT,
    doc_type     TEXT,
    date         TEXT,
    vendor       TEXT,
    amount       REAL,
    currency     TEXT,
    result_json  TEXT,                       -- ExtractionResult.to_dict()
    origin       TEXT,                       -- pipeline / web_review / migrate
    error        TEXT,
    doc_type_hint TEXT,                      -- 使用者上傳時選的類型(None = 沒選)
    category     TEXT,                       -- 四大類(src.models.CATEGORIES)或「未分類」;舊資料為 None
    doc_label    TEXT                        -- 家人上傳時選的文件名稱(src.models.CATEGORY_DOCS,例「保單」);沒選為 None
);
CREATE TABLE IF NOT EXISTS actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER NOT NULL REFERENCES documents(id),
    created_at   TEXT NOT NULL,
    kind         TEXT NOT NULL,              -- 例:calendar / medication_schedule / todo
    tier         TEXT NOT NULL,              -- auto / confirm / manual
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending / done / rejected
    payload_json TEXT NOT NULL,
    superseded   INTEGER NOT NULL DEFAULT 0  -- 1 = 文件後來被家人更正或退回,這筆已被取代(不再生效,也不能恢復)
);
CREATE TABLE IF NOT EXISTS corrections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id  INTEGER REFERENCES documents(id),
    created_at   TEXT NOT NULL,
    doc_type     TEXT,
    vendor_key   TEXT,                       -- 統編或正規化商家名,供 few-shot 檢索
    before_json  TEXT NOT NULL,
    after_json   TEXT NOT NULL,
    verified     INTEGER NOT NULL DEFAULT 0, -- 通過驗證閘門才可進入記憶
    source       TEXT
);
CREATE TABLE IF NOT EXISTS settings (           -- 全家共用的設定(設定頁可改的部分,覆蓋 config.yaml)
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS setting_changes (    -- 每次改設定都留一筆:什麼時候、誰、從什麼改成什麼
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    key          TEXT NOT NULL,
    old_value    TEXT,
    new_value    TEXT NOT NULL,
    actor        TEXT                        -- 登入的家人(Cloudflare Access 的 email);本機直接開為 None
);
CREATE INDEX IF NOT EXISTS idx_documents_created ON documents(created_at);
CREATE INDEX IF NOT EXISTS idx_actions_document ON actions(document_id);
CREATE INDEX IF NOT EXISTS idx_corrections_vendor ON corrections(vendor_key, verified);
"""

ACTION_TIERS = ("auto", "confirm", "manual")
ACTION_STATUSES = ("pending", "done", "rejected")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            _migrate(conn)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:  # 成功自動 commit、例外自動 rollback
                yield conn
        finally:
            conn.close()

    # ---- documents -------------------------------------------------------

    def add_document(self, record: dict[str, Any], origin: str = "pipeline") -> int:
        """寫入一筆處理紀錄(與 records.jsonl 同格式的 dict),回傳文件 ID。"""
        columns = {
            "created_at": record.get("時間") or _now(),
            "source_file": record.get("原始檔案", ""),
            **_record_columns(record),
            "origin": record.get("來源") or origin,
            "doc_type_hint": record.get("使用者提示"),
            "category": record.get("類別"),
            "doc_label": record.get("使用者選的文件"),
        }
        with self._conn() as conn:
            cur = conn.execute(
                f"INSERT INTO documents ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                tuple(columns.values()),
            )
            return int(cur.lastrowid)

    def update_document(self, doc_id: int, record: dict[str, Any]) -> None:
        """家人更正或退回後,用新的處理紀錄更新同一筆文件(原則 8:人工的決定不新增一筆)。

        record 的格式同 add_document;上傳時間(created_at)、原始檔名、來源、類型提示與家人選的文件名稱
        是上傳時的事實,不變。找不到這份文件丟 LookupError。
        """
        columns = _record_columns(record)
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE documents SET {', '.join(f'{name} = ?' for name in columns)} WHERE id = ?",
                (*columns.values(), doc_id),
            )
            if cur.rowcount == 0:
                raise LookupError(f"找不到文件 {doc_id}")

    def set_document_category(self, doc_id: int, category: str) -> None:
        """家人指定(或改)文件的大類,例如把「未分類」的公文歸到財產資產。不是四大類也不是未分類丟 ValueError。"""
        if category not in (*CATEGORIES, UNCATEGORIZED):
            raise ValueError(f"未知的文件類別:{category!r}")
        with self._conn() as conn:
            if conn.execute("UPDATE documents SET category = ? WHERE id = ?", (category, doc_id)).rowcount == 0:
                raise LookupError(f"找不到文件 {doc_id}")

    def count_documents(self, action: str | None = None) -> int:
        """文件數(例如導覽列「待複核」的數字:action="review")。"""
        sql, args = "SELECT COUNT(*) FROM documents", []
        if action:
            sql += " WHERE action = ?"
            args.append(action)
        with self._conn() as conn:
            return int(conn.execute(sql, args).fetchone()[0])

    def get_document(self, doc_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return _document(row) if row else None

    def list_documents(
        self, doc_type: str | None = None, action: str | None = None, limit: int | None = 100,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """新的在前;limit=None 表示全部列出(待複核清單不能漏掉任何一份)。category 篩四大類或「未分類」。"""
        sql, args = "SELECT * FROM documents WHERE 1=1", []
        if doc_type:
            sql += " AND doc_type = ?"
            args.append(doc_type)
        if category:
            sql += " AND category = ?"
            args.append(category)
        if action:
            sql += " AND action = ?"
            args.append(action)
        sql += " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        with self._conn() as conn:
            return [_document(r) for r in conn.execute(sql, args).fetchall()]

    def import_records(self, records: Iterable[dict[str, Any]]) -> int:
        """把舊的 records.jsonl 匯入;同一時間+同檔名視為已匯入,可重複執行。"""
        imported = 0
        with self._conn() as conn:
            existing = {
                (r["created_at"], r["source_file"])
                for r in conn.execute("SELECT created_at, source_file FROM documents")
            }
        for rec in records:
            key = (rec.get("時間"), rec.get("原始檔案", ""))
            if key in existing:
                continue
            self.add_document(rec, origin="migrate")
            existing.add(key)
            imported += 1
        return imported

    # ---- actions ---------------------------------------------------------

    def add_action(
        self, document_id: int, kind: str, tier: str, payload: dict[str, Any], status: str = "pending"
    ) -> int:
        with self._conn() as conn:
            return _insert_action(conn, document_id, kind, tier, payload, status)

    def replace_actions(self, document_id: int, actions: list[dict[str, Any]]) -> list[int]:
        """家人更正(或退回)後換掉這份文件的行動,回傳新行動的 ID。

        舊的行動一律標成 rejected 並註記被取代(superseded):還在等確認的不再列在「家人確認」,
        已確認或已取消的也不能再恢復;接著寫入新的(格式同 plan_actions 的回傳值,退回時是空清單)。
        同一個交易完成,不會只做一半。
        """
        with self._conn() as conn:
            conn.execute(
                "UPDATE actions SET status = 'rejected', superseded = 1 WHERE document_id = ? AND superseded = 0",
                (document_id,),
            )
            return [_insert_action(conn, document_id, a["kind"], a["tier"], a.get("payload", {}))
                    for a in actions]

    def list_actions(
        self, document_id: int | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM actions WHERE 1=1", []
        if document_id is not None:
            sql += " AND document_id = ?"
            args.append(document_id)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY id"
        with self._conn() as conn:
            return [_action(r) for r in conn.execute(sql, args).fetchall()]

    def set_action_status(self, action_id: int, status: str) -> None:
        """改一筆行動的狀態;已被取代(superseded)的不改:它不再生效,也不能恢復。

        web 層改之前會先檢查,這裡在同一句 UPDATE 再擋一次:檢查和寫入之間就算有更正插進來,舊行動也不會被改回來。
        """
        if status not in ACTION_STATUSES:
            raise ValueError(f"未知的行動狀態:{status!r}")
        with self._conn() as conn:
            conn.execute("UPDATE actions SET status = ? WHERE id = ? AND superseded = 0", (status, action_id))

    # ---- settings(全家共用的設定;驗證規則在 src/settings.py) ---------------

    def get_settings(self) -> dict[str, str]:
        """目前存的設定(沒改過的項目不在裡面,沿用 config.yaml)。"""
        with self._conn() as conn:
            return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}

    def set_setting(self, key: str, value: str, actor: str | None = None) -> bool:
        """寫入一項設定;值有變才寫,並在同一個交易留一筆變更紀錄。回傳有沒有變。"""
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            old = row["value"] if row else None
            if old == value:
                return False
            now = _now()
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, now),
            )
            conn.execute(
                "INSERT INTO setting_changes (created_at, key, old_value, new_value, actor) VALUES (?, ?, ?, ?, ?)",
                (now, key, old, value, actor),
            )
            return True

    def list_setting_changes(self, limit: int = 10) -> list[dict[str, Any]]:
        """最近的設定變更,新的在前。"""
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM setting_changes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    # ---- corrections -----------------------------------------------------

    def add_correction(
        self,
        before: dict[str, Any],
        after: dict[str, Any],
        *,
        document_id: int | None = None,
        doc_type: str | None = None,
        vendor_key: str | None = None,
        verified: bool = False,
        source: str = "web_review",
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO corrections
                   (document_id, created_at, doc_type, vendor_key, before_json, after_json, verified, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (document_id, _now(), doc_type, vendor_key, _dumps(before), _dumps(after),
                 int(verified), source),
            )
            return int(cur.lastrowid)

    def list_corrections(
        self,
        vendor_key: str | None = None,
        doc_type: str | None = None,
        verified_only: bool = True,
        limit: int = 20,
        document_id: int | None = None,
    ) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM corrections WHERE 1=1", []
        if verified_only:
            sql += " AND verified = 1"
        if document_id is not None:
            sql += " AND document_id = ?"
            args.append(document_id)
        if vendor_key:
            sql += " AND vendor_key = ?"
            args.append(vendor_key)
        if doc_type:
            sql += " AND doc_type = ?"
            args.append(doc_type)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as conn:
            return [_correction(r) for r in conn.execute(sql, args).fetchall()]

    # ---- 刪除全部資料(設定頁) ------------------------------------------------

    def purge_all(self) -> dict[str, int]:
        """刪除全部文件、行動與更正,回傳各表刪了幾筆;設定與設定變更紀錄保留。原件檔案由呼叫的人刪。

        同一個交易刪完(先刪參照文件的行動與更正),不會只刪一半。文件編號不重來:開著的舊頁面或書籤
        只會「找不到」,不會變成別份文件。刪完再 VACUUM,把已刪的內容從資料庫檔案裡清掉;
        剛好有別的連線在用而做不了時只記 warning(資料已經刪了,舊內容等之後被新資料覆寫)。
        """
        with self._conn() as conn:
            counts = {table: conn.execute(f"DELETE FROM {table}").rowcount
                      for table in ("actions", "corrections", "documents")}
        try:
            with self._conn() as conn:
                conn.execute("VACUUM")
        except sqlite3.OperationalError as exc:
            logging.getLogger(__name__).warning("刪除全部資料後 VACUUM 失敗:%s", exc)
        return counts


def _migrate(conn: sqlite3.Connection) -> None:
    """舊版資料庫缺的欄位補上(CREATE TABLE IF NOT EXISTS 不會改既有的表)。"""
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
    if "doc_type_hint" not in columns:
        conn.execute("ALTER TABLE documents ADD COLUMN doc_type_hint TEXT")
    if "category" not in columns:
        conn.execute("ALTER TABLE documents ADD COLUMN category TEXT")
    if "doc_label" not in columns:
        conn.execute("ALTER TABLE documents ADD COLUMN doc_label TEXT")
    action_columns = {r["name"] for r in conn.execute("PRAGMA table_info(actions)")}
    if "superseded" not in action_columns:
        conn.execute("ALTER TABLE actions ADD COLUMN superseded INTEGER NOT NULL DEFAULT 0")


def _record_columns(record: dict[str, Any]) -> dict[str, Any]:
    """處理紀錄(records.jsonl 的格式)→ documents 表裡跟著處理結果變的欄位;新增與更新共用這份對應。"""
    result = record.get("AI辨識結果") or {}
    return {
        "action": record.get("動作", ""),
        "reason": record.get("原因"),
        "target_path": record.get("目標路徑"),
        "doc_type": result.get("doc_type"),
        "date": result.get("date"),
        "vendor": result.get("vendor"),
        "amount": result.get("amount"),
        "currency": result.get("currency"),
        "result_json": _dumps(result) if result else None,
        "error": record.get("錯誤"),
    }


def _insert_action(conn: sqlite3.Connection, document_id: int, kind: str, tier: str,
                   payload: dict[str, Any], status: str = "pending") -> int:
    if tier not in ACTION_TIERS:
        raise ValueError(f"未知的行動分級:{tier!r}")
    if status not in ACTION_STATUSES:
        raise ValueError(f"未知的行動狀態:{status!r}")
    cur = conn.execute(
        """INSERT INTO actions (document_id, created_at, kind, tier, status, payload_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (document_id, _now(), kind, tier, status, _dumps(payload)),
    )
    return int(cur.lastrowid)


def _document(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["result"] = json.loads(d.pop("result_json")) if d.get("result_json") else None
    return d


def _action(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["payload"] = json.loads(d.pop("payload_json"))
    d["superseded"] = bool(d.get("superseded"))
    return d


def _correction(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["before"] = json.loads(d.pop("before_json"))
    d["after"] = json.loads(d.pop("after_json"))
    d["verified"] = bool(d["verified"])
    return d
