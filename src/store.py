"""SQLite 儲存層:文件、行動、人工更正。

SQLite 是文件、行動與更正狀態的單一事實來源(原則 8);records.jsonl 只是同一份處理紀錄的
衍生流水帳(人看得懂、可 grep),新功能一律讀這裡、不讀 jsonl。家人更正或退回時更新原本那筆文件
(update_document),不新增一筆;這份文件的舊行動換成新的(replace_actions)。corrections 表在
家人更正讀值時寫入(F7),記下更正前後的讀值與是否通過核對(verified)。每次呼叫開一條新連線,
FastAPI 的執行緒池與 CLI 都能安全共用同一個檔案。好幾個寫入要一起成功或一起撤回(文件與它的行動、
更正紀錄、操作紀錄)時包在 transaction() 裡:區塊內的方法共用同一條連線,做完才 commit。

文件的 action 除了處理結果(archive / review / failed),還有收件後還沒讀完的 processing(src/pipeline.py):
上傳檔存好就先建這一筆,讀完更新同一筆;服務中途重開時,開機恢復把它改成「模型沒回應」的失敗。

家庭角色:members 表把 Access 登入的 email 對照到長輩/家人;audit_events 表記下每次確認、
退回、取消/恢復提醒、更正讀值與退回文件是誰、什麼時候做的(讀值更正後,看得到原本是誰確認過)。
這一層只存與查,誰能做什麼由 web 層決定。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import AUDIT_EVENTS, CATEGORIES, ROLES, UNCATEGORIZED

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    source_file  TEXT NOT NULL,
    action       TEXT NOT NULL,              -- archive / review / failed;processing = 收件後還沒讀完
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
    doc_label    TEXT,                       -- 家人上傳時選的文件名稱(src.models.CATEGORY_DOCS,例「保單」);沒選為 None
    file_sha256  TEXT                        -- 原件的 SHA-256(重複上傳提示用,src/duplicates.py);上傳時的事實,更正不改
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
CREATE TABLE IF NOT EXISTS members (            -- 家庭成員:Access 登入的 email → 角色(刪除全部資料時保留,同設定)
    email        TEXT PRIMARY KEY,              -- 一律小寫
    role         TEXT NOT NULL,                 -- elder / family(src.models.ROLES)
    display_name TEXT,                          -- 畫面上的稱呼(例「媽媽」);可空
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (       -- 操作紀錄:誰、什麼時候、對哪份文件或哪個行動做了什麼
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    document_id  INTEGER REFERENCES documents(id),
    action_id    INTEGER REFERENCES actions(id),
    event        TEXT NOT NULL,                 -- src.models.AUDIT_EVENTS 的鍵
    actor        TEXT,                          -- Access 登入的 email;本機直接開為 None
    role         TEXT,                          -- 當下的角色(elder / family);還沒設定家庭成員時為 None
    detail_json  TEXT                           -- 例:{"from": "pending", "to": "done"}、{"correction_id": 12}
);
CREATE INDEX IF NOT EXISTS idx_documents_created ON documents(created_at);
CREATE INDEX IF NOT EXISTS idx_actions_document ON actions(document_id);
CREATE INDEX IF NOT EXISTS idx_corrections_vendor ON corrections(vendor_key, verified);
CREATE INDEX IF NOT EXISTS idx_audit_document ON audit_events(document_id);
"""

ACTION_TIERS = ("auto", "confirm", "manual")
CONSENT_PREFIX = "consent:"    # 設定裡的鍵:consent:<email> = 看過資料使用說明的時間(刪除全部資料時保留)
DOCUMENT_DELETED = "document_deleted"   # 設定變更紀錄的鍵:刪除單份文件(old_value 是文件編號)
ACTION_STATUSES = ("pending", "done", "rejected", "completed")   # completed:長輩或家人按了「已繳費/已辦理」
MEMBER_CHANGE_PREFIX = "member:"   # 家庭成員的變更記在 setting_changes,key 是 member:<email>
MEMBER_REMOVED = "removed"         # 移除成員時 setting_changes 的 new_value


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._tx = threading.local()   # transaction() 裡這條執行緒共用的連線(連線不跨執行緒)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            _migrate(conn)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        shared = getattr(self._tx, "conn", None)
        if shared is not None:
            yield shared   # 在 transaction() 裡:用同一條連線,commit 或撤回由 transaction() 決定
            return
        conn = self._open()
        try:
            with conn:  # 成功自動 commit、例外自動 rollback
                yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """好幾個寫入包成一個交易:with store.transaction(): 裡呼叫的方法都用同一條連線,全部做完才 commit;
        任何一步丟例外(服務被關掉也一樣)就全部撤回,不會文件有了、提醒少了。

        只在呼叫的這條執行緒有效,別的執行緒照常各開各的連線;巢狀的 transaction() 併進最外層。
        delete_document 與 purge_all 做完要 VACUUM(不能在交易裡做),不要放進來。
        """
        if getattr(self._tx, "conn", None) is not None:
            yield
            return
        conn = self._open()
        self._tx.conn = conn
        try:
            with conn:
                yield
        finally:
            self._tx.conn = None
            conn.close()

    def writable(self) -> bool:
        """資料庫寫得進去嗎(/healthz 用):開一個寫入交易就撤回,不改任何資料;被鎖住超過 1 秒也算寫不進去。"""
        try:
            conn = sqlite3.connect(self.path, timeout=1)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("ROLLBACK")
            finally:
                conn.close()
        except sqlite3.Error:
            return False
        return True

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
            "file_sha256": record.get("檔案雜湊"),
        }
        with self._conn() as conn:
            cur = conn.execute(
                f"INSERT INTO documents ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                tuple(columns.values()),
            )
            return int(cur.lastrowid)

    def update_document(self, doc_id: int, record: dict[str, Any], *,
                        event: str | None = None, actor: str | None = None,
                        role: str | None = None, expect: str | None = None,
                        was: str | None = None) -> None:
        """家人更正或退回後,用新的處理紀錄更新同一筆文件(原則 8:人工的決定不新增一筆)。

        record 的格式同 add_document;上傳時間(created_at)、原始檔名、來源、類型提示與家人選的文件名稱
        是上傳時的事實,不變。找不到這份文件丟 LookupError。
        expect:只在文件現在是這個狀態時才更新(例如處理中的讀完才寫回:讀的時候被刪掉、或已經被改成別的狀態就不寫),
        不是的話和找不到一樣丟 LookupError。was:操作紀錄的「從」;沒給就用資料庫裡現在的狀態
        (重新辨識時文件已經先改成處理中,紀錄要寫原本的讀不出來)。
        """
        columns = _record_columns(record)
        sql = f"UPDATE documents SET {', '.join(f'{name} = ?' for name in columns)} WHERE id = ?"
        args: tuple[Any, ...] = (*columns.values(), doc_id)
        if expect is not None:
            sql, args = sql + " AND action = ?", (*args, expect)
        with self._conn() as conn:
            before = conn.execute("SELECT action FROM documents WHERE id = ?", (doc_id,)).fetchone()
            if conn.execute(sql, args).rowcount == 0:
                raise LookupError(f"找不到文件 {doc_id}" if expect is None else f"文件 {doc_id} 不是 {expect}")
            if event is not None:
                _insert_audit(conn, event, document_id=doc_id, actor=actor, role=role,
                              detail={"from": was or before["action"], "to": columns["action"]})

    def set_document_category(self, doc_id: int, category: str, *,
                              actor: str | None = None, role: str | None = None) -> None:
        """家人指定(或改)文件的大類,例如把「未分類」的公文歸到財產資產。不是四大類也不是未分類丟 ValueError。"""
        if category not in (*CATEGORIES, UNCATEGORIZED):
            raise ValueError(f"未知的文件類別:{category!r}")
        with self._conn() as conn:
            before = conn.execute("SELECT category FROM documents WHERE id = ?", (doc_id,)).fetchone()
            if conn.execute("UPDATE documents SET category = ? WHERE id = ?", (category, doc_id)).rowcount == 0:
                raise LookupError(f"找不到文件 {doc_id}")
            if actor is not None and before["category"] != category:
                _insert_audit(conn, "category", document_id=doc_id, actor=actor, role=role,
                              detail={"from": before["category"] or "未分類", "to": category})

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

    def documents_with_hash(self, sha256: str) -> list[dict[str, Any]]:
        """原件雜湊是 sha256 的文件,舊的在前(重複上傳提示用)。"""
        with self._conn() as conn:
            return [_document(r) for r in conn.execute(
                "SELECT * FROM documents WHERE file_sha256 = ? ORDER BY id", (sha256,)).fetchall()]

    def documents_missing_hash(self) -> list[dict[str, Any]]:
        """還沒有原件雜湊、但記著原件位置的文件(這個欄位加上之前就有的);只回編號與位置,網頁啟動時補算用。"""
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, target_path FROM documents WHERE file_sha256 IS NULL AND target_path IS NOT NULL "
                "ORDER BY id").fetchall()]

    def set_file_hash(self, doc_id: int, sha256: str) -> None:
        """補上原件雜湊;只補還沒有的,上傳時算好的不覆寫。"""
        with self._conn() as conn:
            conn.execute("UPDATE documents SET file_sha256 = ? WHERE id = ? AND file_sha256 IS NULL",
                         (sha256, doc_id))

    def set_target_path(self, doc_id: int, path: str, *, was: str | None) -> bool:
        """改記原件的位置(開機恢復認回原件、又搬不回原位時用),回傳有沒有改;資料庫記的已經不是 was 就不改。"""
        with self._conn() as conn:
            return conn.execute("UPDATE documents SET target_path = ? WHERE id = ? AND target_path IS ?",
                                (path, doc_id, was)).rowcount > 0

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

    def set_action_status(self, action_id: int, status: str, *, event: str | None = None,
                          actor: str | None = None, role: str | None = None) -> bool:
        """改一筆行動的狀態,回傳有沒有改到;已被取代(superseded)的不改:它不再生效,也不能恢復。

        web 層改之前會先檢查,這裡在同一句 UPDATE 再擋一次:檢查和寫入之間就算有更正插進來,舊行動也不會被改回來。
        給了 event(src.models.AUDIT_EVENTS 的鍵)就在同一個交易記下是誰改的;狀態沒變(例如連按兩次)不記。
        """
        if status not in ACTION_STATUSES:
            raise ValueError(f"未知的行動狀態:{status!r}")
        with self._conn() as conn:
            row = conn.execute("SELECT document_id, status FROM actions WHERE id = ? AND superseded = 0",
                               (action_id,)).fetchone()
            if row is None or row["status"] == status:
                return False
            # 條件帶著讀到的舊狀態:讀與寫之間被別人改過就不改,也不記一筆不實的「從什麼改成什麼」
            cur = conn.execute("UPDATE actions SET status = ? WHERE id = ? AND superseded = 0 AND status = ?",
                               (status, action_id, row["status"]))
            if cur.rowcount == 0:
                return False
            if event is not None:
                _insert_audit(conn, event, document_id=row["document_id"], action_id=action_id,
                              actor=actor, role=role, detail={"from": row["status"], "to": status})
            return True

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
        actor: str | None = None,
        role: str | None = None,
    ) -> int:
        """寫入一筆更正(更正前後的讀值),回傳更正編號;有文件編號時在同一個交易記下是誰更正的(correct)。"""
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO corrections
                   (document_id, created_at, doc_type, vendor_key, before_json, after_json, verified, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (document_id, _now(), doc_type, vendor_key, _dumps(before), _dumps(after),
                 int(verified), source),
            )
            correction_id = int(cur.lastrowid)
            if document_id is not None:
                _insert_audit(conn, "correct", document_id=document_id, actor=actor, role=role,
                              detail={"correction_id": correction_id})
            return correction_id

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

    # ---- audit_events(操作紀錄) ---------------------------------------------

    def add_audit_event(self, event: str, *, document_id: int | None = None, action_id: int | None = None,
                        actor: str | None = None, role: str | None = None,
                        detail: dict[str, Any] | None = None) -> int:
        """記一筆操作紀錄,回傳編號。行動狀態與更正請用 set_action_status、add_correction(同一個交易記);
        這裡給其他操作用,例如退回整份文件(reject_document)。"""
        with self._conn() as conn:
            return _insert_audit(conn, event, document_id=document_id, action_id=action_id,
                                 actor=actor, role=role, detail=detail)

    def list_audit_events(self, document_id: int | None = None, limit: int = 50,
                          event: str | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """操作紀錄,新的在前;給 document_id 就只看這份文件的(含它的行動),給 event 就只看這一種(或這幾種)操作。"""
        sql, args = "SELECT * FROM audit_events WHERE 1=1", []
        if document_id is not None:
            sql += " AND document_id = ?"
            args.append(document_id)
        if event is not None:
            names = (event,) if isinstance(event, str) else tuple(event)
            if not names:
                return []
            sql += f" AND event IN ({', '.join('?' * len(names))})"
            args.extend(names)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as conn:
            return [_audit(r) for r in conn.execute(sql, args).fetchall()]

    def get_audit_event(self, event_id: int) -> dict[str, Any] | None:
        """用編號取一筆操作紀錄;沒有回 None。"""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM audit_events WHERE id = ?", (event_id,)).fetchone()
        return _audit(row) if row else None

    # ---- members(家庭成員與角色;誰能做什麼由 web 層決定) ---------------------

    def list_members(self) -> list[dict[str, Any]]:
        """家庭成員,家人在前,同角色依 email 排。"""
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM members ORDER BY CASE role WHEN 'family' THEN 0 ELSE 1 END, email").fetchall()]

    def get_member(self, email: str | None) -> dict[str, Any] | None:
        """這個 email(不分大小寫)的成員資料;不是家庭成員回 None。"""
        key = _member_key(email)
        if not key:
            return None
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM members WHERE email = ?", (key,)).fetchone()
        return dict(row) if row else None

    def set_member(self, email: str, role: str, display_name: str | None = None, *,
                   actor: str | None = None) -> bool:
        """新增或修改一位家庭成員,回傳有沒有變;有變才寫,並在設定變更紀錄留一筆(key 是 member:<email>)。"""
        key = _member_key(email)
        if not key:
            raise ValueError("家庭成員的 email 不能是空的")
        if role not in ROLES:
            raise ValueError(f"未知的角色:{role!r}")
        name = (display_name or "").strip() or None
        with self._conn() as conn:
            row = conn.execute("SELECT role, display_name FROM members WHERE email = ?", (key,)).fetchone()
            if row and row["role"] == role and row["display_name"] == name:
                return False
            now = _now()
            conn.execute(
                "INSERT INTO members (email, role, display_name, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(email) DO UPDATE SET role = excluded.role, display_name = excluded.display_name, "
                "updated_at = excluded.updated_at",
                (key, role, name, now),
            )
            conn.execute(
                "INSERT INTO setting_changes (created_at, key, old_value, new_value, actor) VALUES (?, ?, ?, ?, ?)",
                (now, MEMBER_CHANGE_PREFIX + key, row["role"] if row else None, role, actor),
            )
            return True

    def remove_member(self, email: str, *, actor: str | None = None) -> bool:
        """移除一位家庭成員,回傳有沒有移除;同樣在設定變更紀錄留一筆(new_value 是 removed)。"""
        key = _member_key(email)
        with self._conn() as conn:
            row = conn.execute("SELECT role FROM members WHERE email = ?", (key,)).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM members WHERE email = ?", (key,))
            conn.execute(
                "INSERT INTO setting_changes (created_at, key, old_value, new_value, actor) VALUES (?, ?, ?, ?, ?)",
                (_now(), MEMBER_CHANGE_PREFIX + key, row["role"], MEMBER_REMOVED, actor),
            )
            return True

    # ---- 刪除單份文件(文件頁,只有家人) ----------------------------------------------

    def delete_document(self, doc_id: int, *, actor: str | None = None) -> bool:
        """刪除一份文件與它的行動、更正、操作紀錄,回傳有沒有刪;原件檔案由呼叫的人刪。

        同一個交易刪完(先刪參照文件與行動的操作紀錄),不會只刪一半;在設定變更紀錄留一筆誰刪的,
        只記文件編號、不記內容(這份文件的操作紀錄跟著刪了,誰刪的只能記在那裡)。刪完再 VACUUM,同 purge_all。
        """
        with self._conn() as conn:
            if conn.execute("SELECT 1 FROM documents WHERE id = ?", (doc_id,)).fetchone() is None:
                return False
            conn.execute("DELETE FROM audit_events WHERE document_id = ? "
                         "OR action_id IN (SELECT id FROM actions WHERE document_id = ?)", (doc_id, doc_id))
            for table in ("actions", "corrections"):
                conn.execute(f"DELETE FROM {table} WHERE document_id = ?", (doc_id,))
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            conn.execute(
                "INSERT INTO setting_changes (created_at, key, old_value, new_value, actor) VALUES (?, ?, ?, ?, ?)",
                (_now(), DOCUMENT_DELETED, str(doc_id), "deleted", actor),
            )
        try:
            with self._conn() as conn:
                conn.execute("VACUUM")
        except sqlite3.OperationalError as exc:
            logging.getLogger(__name__).warning("刪除文件後 VACUUM 失敗:%s", exc)
        return True

    # ---- 刪除全部資料(設定頁) ------------------------------------------------

    def purge_all(self) -> dict[str, int]:
        """刪除全部文件、行動、更正與操作紀錄,回傳各表刪了幾筆;設定、設定變更紀錄與家庭成員保留。
        原件檔案由呼叫的人刪。

        同一個交易刪完(先刪參照文件與行動的操作紀錄,再刪行動與更正),不會只刪一半。文件編號不重來:
        開著的舊頁面或書籤只會「找不到」,不會變成別份文件。刪完再 VACUUM,把已刪的內容從資料庫檔案裡清掉;
        剛好有別的連線在用而做不了時只記 warning(資料已經刪了,舊內容等之後被新資料覆寫)。
        """
        with self._conn() as conn:
            counts = {table: conn.execute(f"DELETE FROM {table}").rowcount
                      for table in ("audit_events", "actions", "corrections", "documents")}
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
    if "file_sha256" not in columns:
        conn.execute("ALTER TABLE documents ADD COLUMN file_sha256 TEXT")
    # 欄位補上之後才能建索引(SCHEMA 先跑,舊資料庫那時還沒有這個欄位)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_sha ON documents(file_sha256)")
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


def _insert_audit(conn: sqlite3.Connection, event: str, *, document_id: int | None = None,
                  action_id: int | None = None, actor: str | None = None, role: str | None = None,
                  detail: dict[str, Any] | None = None) -> int:
    if event not in AUDIT_EVENTS:
        raise ValueError(f"未知的操作紀錄種類:{event!r}")
    if role is not None and role not in ROLES:
        raise ValueError(f"未知的角色:{role!r}")
    cur = conn.execute(
        """INSERT INTO audit_events (created_at, document_id, action_id, event, actor, role, detail_json)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (_now(), document_id, action_id, event, actor, role, _dumps(detail) if detail is not None else None),
    )
    return int(cur.lastrowid)


def _member_key(email: str | None) -> str:
    """成員表的鍵:去頭尾空白、一律小寫(Access 轉過來的 email 大小寫可能和家人輸入的不同)。"""
    return (email or "").strip().lower()


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


def _audit(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    raw = d.pop("detail_json")
    d["detail"] = json.loads(raw) if raw else None
    return d
