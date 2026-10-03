"""全家共用的系統設定(設定頁):驗證規則、目前生效的設定、匯出與刪除全部資料。

設定頁能改的只有兩項,存在 SQLite 的 settings 表(src/store.py),蓋過 config.yaml 的值:
- 辨識模式(provider):ollama「只用這台電腦」/ workers_ai「可以用雲端備援」。
- 自動存檔門檻(auto_threshold):驗證信心達到才自動存檔。
安全底線寫死在這裡,設定改不掉:門檻只能在 0.80–0.98(不能比預設鬆);雲端備援要這台電腦設好帳號與
金鑰的環境變數才能開;展示模式(mock)不換辨識模式;藥袋一律只在本機辨識。存的值每次讀出來都重新
驗證(effective_config),被直接改資料庫的不合規則值不會生效。每次改動都由 Store.set_setting 留紀錄。
"""
from __future__ import annotations

import logging
import math
import os
import unicodedata
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .config import AppConfig
from .store import Store

log = logging.getLogger(__name__)

# settings 表的鍵(值一律是字串)
PROVIDER = "provider"            # 辨識模式:PROVIDER_CHOICES 之一
THRESHOLD = "auto_threshold"     # 自動存檔門檻,例 "0.85"
PURGED = "purged"                # 不是設定:「刪除全部資料」的時間,只為了在設定變更紀錄留一筆

PROVIDER_CHOICES = ("ollama", "workers_ai")   # 設定頁能選的辨識模式(mock 只能由 config.yaml 指定)
THRESHOLD_MIN, THRESHOLD_MAX = 0.80, 0.98     # 門檻只能調得更嚴:不能低於預設的 0.80
THRESHOLD_CHOICES = (0.80, 0.85, 0.90, 0.95)  # 設定頁的選項
ALWAYS_LOCAL = "藥袋"                          # 特種個資:不論設定,一律只在本機辨識

MSG_DEMO = "展示模式不能切換辨識模式。"
MSG_PROVIDER = "辨識模式只能選「只用這台電腦」或「可以用雲端備援」。"
MSG_NO_CLOUD_KEY = "這台電腦還沒有設定雲端模型的帳號與金鑰,不能開啟雲端備援。"
MSG_THRESHOLD_LOW = "自動存檔門檻不能低於 80%。"
MSG_THRESHOLD = "自動存檔門檻要在 80% 到 98% 之間。"

# 匯出時 SQLite 的 LIMIT 給負數 = 不限筆數(更正與設定變更的清單方法預設只給最近幾筆)
_ALL_ROWS = -1
_KEEP_FILE = ".gitkeep"   # 資料夾的佔位檔(repo 用來保留空資料夾),不是文件,刪除全部資料時留著


class SettingsError(ValueError):
    """設定不合規則;訊息是給家人看的中文,設定頁直接顯示。"""


def cloud_ready(cfg: AppConfig) -> bool:
    """雲端備援的帳號 ID 與 API Token(環境變數,名稱見 WorkersAIConfig)都有設定才能開;只看有沒有,不讀出值。"""
    wcfg = cfg.workers_ai
    return all(os.environ.get(name, "").strip() for name in (wcfg.account_id_env, wcfg.api_token_env))


def check_provider(value: str, cfg: AppConfig) -> str:
    """辨識模式能不能改成 value(cfg 是 config.yaml 的設定);可以就原樣回傳,不行丟 SettingsError。"""
    if cfg.provider == "mock":
        raise SettingsError(MSG_DEMO)
    if value not in PROVIDER_CHOICES:
        raise SettingsError(MSG_PROVIDER)
    if value == "workers_ai" and not cloud_ready(cfg):
        raise SettingsError(MSG_NO_CLOUD_KEY)
    return value


def parse_threshold(text: str) -> float:
    """門檻的表單值(例 "0.85")→ 0.80–0.98 的小數(取到小數第二位);不合規則丟 SettingsError。"""
    try:
        value = float(unicodedata.normalize("NFKC", str(text)).strip())
    except ValueError:
        raise SettingsError(MSG_THRESHOLD) from None
    if not math.isfinite(value) or value > THRESHOLD_MAX:
        raise SettingsError(MSG_THRESHOLD)
    if value < THRESHOLD_MIN:
        raise SettingsError(MSG_THRESHOLD_LOW)
    return round(value, 2)


def effective_config(cfg: AppConfig, store: Store) -> AppConfig:
    """目前生效的設定:config.yaml 的值(cfg),再套上設定頁存在 SQLite 的值。回傳新的物件,不改 cfg。

    存的值每次都重新驗證,不合規則就沿用 config.yaml:門檻要在 0.80–0.98、雲端備援要有帳號與金鑰、
    展示模式不換辨識模式。藥袋一律在 local_only_doc_types 裡(config.yaml 漏寫也補上)。
    """
    stored = store.get_settings()
    changes: dict[str, Any] = {}
    if PROVIDER in stored:
        try:
            changes["provider"] = check_provider(stored[PROVIDER], cfg)
        except SettingsError:
            pass   # 例如雲端金鑰後來被拿掉:回到 config.yaml 的模式,不讓上傳失敗
    if THRESHOLD in stored:
        try:
            changes["auto_threshold"] = parse_threshold(stored[THRESHOLD])
        except SettingsError:
            pass   # 不合規則的值(被直接改資料庫)不生效
    local_only = tuple(cfg.local_only_doc_types)
    if ALWAYS_LOCAL not in local_only:
        local_only = (ALWAYS_LOCAL, *local_only)
    return replace(cfg, local_only_doc_types=local_only, **changes)


def _form_text(value: Any) -> str | None:
    """表單的一欄:只收字串(沒送這一欄是 None)。"""
    return value.strip() if isinstance(value, str) else None


def form_changes(form: Mapping[str, Any], base: AppConfig,
                 current: AppConfig) -> tuple[dict[str, str], dict[str, str]]:
    """設定表單 → (要存的設定 {鍵: 字串值}, 錯誤 {鍵: 中文說明})。

    base 是 config.yaml 的設定,current 是目前生效的(effective_config)。只讀辨識模式與門檻兩欄,其他欄位
    一律不看(例如有人塞進來的 local_only_doc_types);只回傳和目前生效值不同的項目,沒改的不寫、不留紀錄。
    任何一欄有錯就整份不存(changes 是空的),畫面上照錯誤說明改好再送。
    """
    changes: dict[str, str] = {}
    errors: dict[str, str] = {}
    provider = _form_text(form.get(PROVIDER))
    if provider is not None and provider != current.provider:
        try:
            changes[PROVIDER] = check_provider(provider, base)
        except SettingsError as exc:
            errors[PROVIDER] = str(exc)
    threshold = _form_text(form.get(THRESHOLD))
    if threshold is not None:
        try:
            value = parse_threshold(threshold)
        except SettingsError as exc:
            errors[THRESHOLD] = str(exc)
        else:
            if value != round(current.auto_threshold, 2):
                changes[THRESHOLD] = f"{value:.2f}"
    return ({} if errors else changes), errors


# ---- 資料管理:匯出全部紀錄、刪除全部資料 -----------------------------------------

def data_folders(cfg: AppConfig) -> dict[str, Path]:
    """原件所在的四個資料夾(同結果頁只送這四處的原件):名稱 → 路徑。"""
    return {"archive": cfg.paths.archive, "review": cfg.paths.review,
            "failed": cfg.paths.failed, "uploads": cfg.paths.uploads_path}


def _resolved(path: Path) -> Path | None:
    """展開 ../ 與符號連結後的位置;連結繞圈之類解不開的回傳 None(當作不在資料夾裡)。"""
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return None


def _resolved_folders(cfg: AppConfig) -> dict[str, Path]:
    """四個資料夾展開後的位置(解不開的略過)。"""
    folders: dict[str, Path] = {}
    for name, path in data_folders(cfg).items():
        root = _resolved(path)
        if root is not None:
            folders[name] = root
    return folders


def _within(path: Path, root: Path) -> bool:
    """path 展開後在 root(已 resolve)裡面,而且不是 root 本身。"""
    resolved = _resolved(path)
    return resolved is not None and resolved != root and resolved.is_relative_to(root)


def _shown_path(target: Any, folders: dict[str, Path]) -> str | None:
    """匯出用的原件位置:四個資料夾內寫相對位置(例 archive/帳單/2026-10/…png),不寫這台電腦的完整路徑。"""
    if not target:
        return None
    path = Path(str(target))
    resolved = _resolved(path)
    for name, root in folders.items():
        if resolved is not None and resolved.is_relative_to(root):
            return f"{name}/{resolved.relative_to(root).as_posix()}"
    return path.name


def export_records(store: Store, cfg: AppConfig, *, version: str) -> dict[str, Any]:
    """「匯出全部紀錄」的內容:文件(含讀值)、行動(提醒、服藥時間表)、更正、設定變更;不含影像。"""
    folders = _resolved_folders(cfg)
    documents = [{**doc, "target_path": _shown_path(doc.get("target_path"), folders)}
                 for doc in store.list_documents(limit=None)]
    return {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "version": version,
        "note": "看有匯出的全部紀錄:文件與讀值、提醒與服藥時間表、更正、設定變更。不含照片與 PDF 原件。",
        "documents": documents,
        "actions": store.list_actions(),
        "corrections": store.list_corrections(verified_only=False, limit=_ALL_ROWS),
        "setting_changes": store.list_setting_changes(limit=_ALL_ROWS),
    }


def delete_data_files(cfg: AppConfig) -> tuple[int, int]:
    """刪除全部資料的檔案部分,回傳 (刪掉幾個檔案, 刪不掉幾個)。

    只刪四個資料夾「裡面」的檔案,再收掉變空的子資料夾(資料夾名稱有類型與月份);四個資料夾本身留著。
    每個要刪的路徑都 resolve() 後確認還在該資料夾內:指到外面的符號連結不跟過去、也不刪,
    連結到別處的資料夾不進去。資料庫與 logs/ 就算被設定在這些資料夾裡也不碰(設定與設定紀錄要保留)。
    刪不掉的(例如檔案正被開著)跳過、計數,不讓整個刪除中斷。
    """
    keep = [p for p in (_resolved(cfg.paths.db_path), _resolved(cfg.paths.logs)) if p is not None]

    def protected(path: Path) -> bool:
        resolved = _resolved(path)
        return resolved is None or any(resolved == k or resolved.is_relative_to(k) for k in keep)

    deleted = failed = 0
    roots = {root for root in _resolved_folders(cfg).values() if root.is_dir()}
    for root in roots:
        # 由下往上走,子資料夾的檔案刪完才輪到它自己;os.walk 預設不跟進資料夾的符號連結
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            here = Path(dirpath)
            for name in filenames:
                path = here / name
                if name == _KEEP_FILE or not _within(path, root) or protected(path):
                    continue
                try:
                    path.unlink()
                    deleted += 1
                except OSError:
                    failed += 1
            for name in dirnames:
                sub = here / name
                # 另一個資料夾被設在這個裡面時,它本身也要留著
                if sub.is_symlink() or not _within(sub, root) or protected(sub) or _resolved(sub) in roots:
                    continue
                try:
                    sub.rmdir()   # 只收空的;還有刪不掉的檔案就留著
                except OSError:
                    pass
    # log 只記數量:檔名含日期、商家與金額
    log.info("刪除全部資料:刪掉 %d 個檔案,%d 個刪不掉", deleted, failed)
    return deleted, failed
