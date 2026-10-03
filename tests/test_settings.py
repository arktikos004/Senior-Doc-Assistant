"""全家共用的系統設定(SET)單元測試:驗證規則、目前生效的設定、匯出與刪除全部資料(Store.purge_all)。

資料夾全部在 tmp_path;雲端金鑰用 monkeypatch 設定假的環境變數,不連網、不呼叫模型。
"""
import json
import os

import pytest

from src.config import AppConfig, PathsConfig
from src.settings import (
    MSG_DEMO,
    MSG_NO_CLOUD_KEY,
    MSG_PROVIDER,
    MSG_THRESHOLD,
    MSG_THRESHOLD_LOW,
    PROVIDER,
    THRESHOLD,
    SettingsError,
    check_provider,
    cloud_ready,
    delete_data_files,
    effective_config,
    export_records,
    form_changes,
    parse_threshold,
)
from src.store import Store


@pytest.fixture
def base(tmp_path) -> AppConfig:
    """config.yaml 的設定(本機模式);資料夾都在 tmp_path。"""
    paths = PathsConfig(inbox=tmp_path / "inbox", archive=tmp_path / "archive", review=tmp_path / "review",
                        failed=tmp_path / "failed", logs=tmp_path / "logs")
    cfg = AppConfig(paths=paths, provider="ollama")
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def store(base) -> Store:
    return Store(base.paths.db_path)


@pytest.fixture
def cloud_keys(monkeypatch):
    """這台電腦設好了雲端模型的帳號與金鑰(假的值)。"""
    monkeypatch.setenv("CF_ACCOUNT_ID", "synthetic-account")
    monkeypatch.setenv("CF_API_TOKEN", "synthetic-token")


@pytest.fixture
def no_cloud_keys(monkeypatch):
    monkeypatch.delenv("CF_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CF_API_TOKEN", raising=False)


# ---- 門檻與辨識模式的規則 --------------------------------------------------------

@pytest.mark.parametrize("text, value", [("0.80", 0.80), ("0.85", 0.85), ("0.9", 0.90), (" 0.95 ", 0.95),
                                         ("0.98", 0.98), ("0.８５", 0.85)])
def test_threshold_accepts_080_to_098(text, value):
    assert parse_threshold(text) == value


@pytest.mark.parametrize("text, message", [
    ("0.79", MSG_THRESHOLD_LOW), ("0.5", MSG_THRESHOLD_LOW), ("0", MSG_THRESHOLD_LOW), ("-1", MSG_THRESHOLD_LOW),
    ("0.99", MSG_THRESHOLD), ("1", MSG_THRESHOLD), ("85%", MSG_THRESHOLD), ("", MSG_THRESHOLD),
    ("abc", MSG_THRESHOLD), ("nan", MSG_THRESHOLD), ("inf", MSG_THRESHOLD), ("-inf", MSG_THRESHOLD),
])
def test_threshold_below_080_or_odd_values_are_refused_in_chinese(text, message):
    """門檻只能調得更嚴:不能低於 0.80;看不懂的值一律拒絕,不猜。"""
    with pytest.raises(SettingsError) as err:
        parse_threshold(text)
    assert str(err.value) == message


def test_cloud_needs_both_keys(base, monkeypatch, no_cloud_keys):
    assert not cloud_ready(base)
    monkeypatch.setenv("CF_ACCOUNT_ID", "synthetic-account")
    assert not cloud_ready(base)                                # 只有一個不夠
    monkeypatch.setenv("CF_API_TOKEN", "   ")
    assert not cloud_ready(base)                                # 空白等於沒設定
    monkeypatch.setenv("CF_API_TOKEN", "synthetic-token")
    assert cloud_ready(base)


def test_cloud_keys_follow_the_configured_variable_names(base, monkeypatch, no_cloud_keys):
    base.workers_ai.account_id_env, base.workers_ai.api_token_env = "MY_ACCOUNT", "MY_TOKEN"
    monkeypatch.setenv("MY_ACCOUNT", "synthetic-account")
    monkeypatch.setenv("MY_TOKEN", "synthetic-token")
    assert cloud_ready(base)


def test_provider_rules(base, no_cloud_keys, monkeypatch):
    assert check_provider("ollama", base) == "ollama"
    with pytest.raises(SettingsError, match=MSG_NO_CLOUD_KEY):
        check_provider("workers_ai", base)                      # 沒有金鑰不能切雲端
    with pytest.raises(SettingsError, match=MSG_PROVIDER):
        check_provider("mock", base)                            # 展示模式只能由 config.yaml 指定
    with pytest.raises(SettingsError, match=MSG_PROVIDER):
        check_provider("cloud-anything", base)
    monkeypatch.setenv("CF_ACCOUNT_ID", "synthetic-account")
    monkeypatch.setenv("CF_API_TOKEN", "synthetic-token")
    assert check_provider("workers_ai", base) == "workers_ai"
    base.provider = "mock"
    for value in ("ollama", "workers_ai"):
        with pytest.raises(SettingsError, match=MSG_DEMO):     # 展示模式不能切換辨識模式
            check_provider(value, base)


# ---- 目前生效的設定 ------------------------------------------------------------------

def test_nothing_stored_means_config_yaml_values(base, store):
    current = effective_config(base, store)
    assert current is not base                                  # 回傳新的物件,不改 config.yaml 的設定
    assert (current.provider, current.auto_threshold, current.local_only_doc_types) == ("ollama", 0.80, ("藥袋",))
    assert current.paths is base.paths


def test_stored_settings_override_config_yaml(base, store, cloud_keys):
    store.set_setting(THRESHOLD, "0.90")
    store.set_setting(PROVIDER, "workers_ai")
    current = effective_config(base, store)
    assert (current.provider, current.auto_threshold) == ("workers_ai", 0.90)
    assert (base.provider, base.auto_threshold) == ("ollama", 0.80)


@pytest.mark.parametrize("bad", ["0.5", "0.79", "0.99", "abc", "", "nan"])
def test_invalid_stored_threshold_never_takes_effect(base, store, bad):
    """不合規則的值(例如被直接改資料庫)不生效,沿用 config.yaml。"""
    store.set_setting(THRESHOLD, bad)
    assert effective_config(base, store).auto_threshold == 0.80


def test_stored_cloud_mode_needs_keys_at_use_time(base, store, cloud_keys, monkeypatch):
    store.set_setting(PROVIDER, "workers_ai")
    assert effective_config(base, store).provider == "workers_ai"
    monkeypatch.delenv("CF_API_TOKEN")                          # 金鑰後來被拿掉:回到本機,不讓上傳失敗
    assert effective_config(base, store).provider == "ollama"


def test_demo_mode_keeps_its_provider(base, store, cloud_keys):
    base.provider = "mock"
    store.set_setting(PROVIDER, "workers_ai")
    store.set_setting(THRESHOLD, "0.95")
    current = effective_config(base, store)
    assert current.provider == "mock" and current.auto_threshold == 0.95


@pytest.mark.parametrize("bad", ["mock", "openai", ""])
def test_unknown_stored_provider_is_ignored(base, store, bad):
    store.set_setting(PROVIDER, bad)
    assert effective_config(base, store).provider == "ollama"


def test_medication_bags_always_stay_local(base, store):
    """藥袋一律只在本機:config.yaml 漏寫也補上;原本就有的保持原順序。"""
    base.local_only_doc_types = ()
    assert effective_config(base, store).local_only_doc_types == ("藥袋",)
    base.local_only_doc_types = ("收據",)
    assert effective_config(base, store).local_only_doc_types == ("藥袋", "收據")
    base.local_only_doc_types = ("收據", "藥袋")
    assert effective_config(base, store).local_only_doc_types == ("收據", "藥袋")
    store.set_setting("local_only_doc_types", "")               # 設定表裡出現的其他鍵一律不看
    assert "藥袋" in effective_config(base, store).local_only_doc_types


# ---- 設定表單 ------------------------------------------------------------------------

def test_form_returns_only_what_changed(base, store, cloud_keys):
    current = effective_config(base, store)
    assert form_changes({"provider": "ollama", "auto_threshold": "0.80"}, base, current) == ({}, {})
    assert form_changes({"provider": "workers_ai", "auto_threshold": "0.85"}, base, current) == (
        {"provider": "workers_ai", "auto_threshold": "0.85"}, {})
    assert form_changes({"auto_threshold": "0.9"}, base, current) == ({"auto_threshold": "0.90"}, {})


def test_form_with_any_error_saves_nothing(base, store, no_cloud_keys):
    current = effective_config(base, store)
    changes, errors = form_changes({"provider": "workers_ai", "auto_threshold": "0.85"}, base, current)
    assert changes == {} and errors == {"provider": MSG_NO_CLOUD_KEY}
    changes, errors = form_changes({"provider": "ollama", "auto_threshold": "0.7"}, base, current)
    assert changes == {} and errors == {"auto_threshold": MSG_THRESHOLD_LOW}


def test_form_ignores_other_fields(base, store):
    """表單值只是資料:只讀辨識模式與門檻,塞進來的其他欄位(例如本機限定的類型)一律不看。"""
    current = effective_config(base, store)
    form = {"local_only_doc_types": "", "provider": ["workers_ai"], "tier": "auto", "auto_threshold": "0.85"}
    assert form_changes(form, base, current) == ({"auto_threshold": "0.85"}, {})


def test_demo_mode_form_cannot_switch_provider(base, store):
    base.provider = "mock"
    current = effective_config(base, store)
    assert form_changes({"provider": "ollama"}, base, current) == ({}, {"provider": MSG_DEMO})
    assert form_changes({"provider": "mock", "auto_threshold": "0.95"}, base, current) == (
        {"auto_threshold": "0.95"}, {})


# ---- 刪除全部資料:資料庫 --------------------------------------------------------------

def _doc(store, name="a.png", target=None):
    return store.add_document({"原始檔案": name, "動作": "archive", "目標路徑": str(target) if target else None,
                               "AI辨識結果": {"doc_type": "帳單", "vendor": "範例電力公司", "amount": 1286}})


def test_purge_all_removes_documents_actions_corrections_but_keeps_settings(store):
    doc_id = _doc(store)
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費"})
    for i in range(3):
        store.add_correction({"amount": i}, {"amount": i + 1}, document_id=doc_id)
    store.set_setting(THRESHOLD, "0.85", actor="family@example.com")
    assert store.purge_all() == {"actions": 1, "corrections": 3, "documents": 1}
    assert store.list_documents() == [] and store.list_actions() == []
    assert store.list_corrections(verified_only=False) == []
    assert store.get_settings() == {THRESHOLD: "0.85"}                    # 設定與設定紀錄保留
    assert [c["new_value"] for c in store.list_setting_changes()] == ["0.85"]
    assert _doc(store, "b.png") > doc_id                                # 文件編號不重來:舊網址不會變成別份文件


def test_purge_all_on_empty_store(store):
    assert store.purge_all() == {"actions": 0, "corrections": 0, "documents": 0}


# ---- 刪除全部資料:檔案 ----------------------------------------------------------------

def _write(path, content=b"synthetic"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_deletes_only_files_inside_the_four_folders(base, tmp_path):
    inside = [_write(base.paths.archive / "帳單" / "2026-10" / "20261001_帳單_範例_1286.png"),
              _write(base.paths.review / "r.png"), _write(base.paths.failed / "f.pdf"),
              _write(base.paths.uploads_path / "u.jpg"), _write(base.paths.archive / ".DS_Store")]
    outside = [_write(base.paths.inbox / "scan.png"), _write(tmp_path / "keep.png"),
               _write(base.paths.logs / "records.jsonl", b"{}\n")]
    keepers = [_write(base.paths.archive / ".gitkeep", b""), _write(base.paths.uploads_path / ".gitkeep", b"")]
    assert delete_data_files(base) == (len(inside), 0)
    assert not any(p.exists() for p in inside)
    assert all(p.exists() for p in outside + keepers)
    # 四個資料夾留著;變空的子資料夾(名稱有類型與月份)收掉
    for folder in (base.paths.archive, base.paths.review, base.paths.failed, base.paths.uploads_path):
        assert folder.is_dir()
    assert not (base.paths.archive / "帳單").exists()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="這個系統不能建符號連結")
def test_symlinks_never_lead_outside(base, tmp_path):
    """指到資料夾外的符號連結不跟過去:外面的檔案與資料夾一個都不能少。"""
    secret = _write(tmp_path / "outside" / "secret.png")
    try:
        (base.paths.archive / "link.png").symlink_to(secret)
        (base.paths.review / "linked-dir").symlink_to(secret.parent, target_is_directory=True)
    except OSError:
        pytest.skip("這個系統不讓一般使用者建符號連結")
    inner = _write(base.paths.failed / "inner.png")
    (base.paths.failed / "inner-link.png").symlink_to(inner)   # 指到資料夾裡面的連結:連結本身可以刪
    deleted, failed = delete_data_files(base)
    assert secret.exists() and secret.parent.is_dir()
    assert (deleted, failed) == (2, 0)
    assert not inner.exists() and not (base.paths.failed / "inner-link.png").is_symlink()


def test_database_and_logs_are_never_deleted_even_if_configured_inside(tmp_path):
    """設定寫錯(logs 放進 archive 裡)也不能把資料庫刪掉:設定與設定紀錄要保留。"""
    archive = tmp_path / "data"
    paths = PathsConfig(inbox=tmp_path / "inbox", archive=archive, review=archive / "review",
                        failed=tmp_path / "failed", logs=archive / "logs")
    cfg = AppConfig(paths=paths)
    cfg.ensure_dirs()
    store = Store(cfg.paths.db_path)
    store.set_setting(THRESHOLD, "0.90")
    doc = _write(archive / "doc.png")
    _write(cfg.paths.review / "r.png")
    assert delete_data_files(cfg) == (2, 0)
    assert not doc.exists() and cfg.paths.db_path.exists() and cfg.paths.review.is_dir()
    assert Store(cfg.paths.db_path).get_settings() == {THRESHOLD: "0.90"}


def test_files_that_cannot_be_deleted_are_counted(base, monkeypatch):
    stuck = _write(base.paths.review / "open.png")
    _write(base.paths.review / "free.png")
    real_unlink = type(stuck).unlink

    def unlink(self, *args, **kwargs):
        if self.name == "open.png":
            raise PermissionError("檔案正被開著")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(type(stuck), "unlink", unlink)
    assert delete_data_files(base) == (1, 1)
    assert stuck.exists()


# ---- 匯出全部紀錄 ----------------------------------------------------------------------

def test_export_has_every_table_but_no_images(base, store):
    image = _write(base.paths.archive / "帳單" / "2026-10" / "bill.png", b"\x89PNG\r\n\x1a\nSYNTHETIC-IMAGE")
    doc_id = _doc(store, target=image)
    other = _doc(store, "elsewhere.png", target=base.paths.inbox / "elsewhere.png")
    store.add_action(doc_id, "calendar", "auto", {"title": "繳電費", "date": "2026-10-20"})
    for i in range(25):                                        # 清單方法預設只給最近 20 筆:匯出要全部
        store.add_correction({"amount": i}, {"amount": i + 1}, document_id=doc_id)
    for i in range(12):
        store.set_setting(THRESHOLD, f"0.{80 + i}", actor="family@example.com")
    data = export_records(store, base, version="2026.10.02")
    assert set(data) == {"exported_at", "version", "note", "documents", "actions", "corrections", "setting_changes"}
    assert len(data["documents"]) == 2 and len(data["corrections"]) == 25 and len(data["setting_changes"]) == 12
    docs = {d["id"]: d for d in data["documents"]}
    assert docs[doc_id]["result"]["vendor"] == "範例電力公司"        # 含讀值
    assert docs[doc_id]["target_path"] == "archive/帳單/2026-10/bill.png"   # 只寫資料夾裡的相對位置
    assert docs[other]["target_path"] == "elsewhere.png"
    assert data["actions"][0]["payload"]["title"] == "繳電費"
    text = json.dumps(data, ensure_ascii=False)
    assert "SYNTHETIC-IMAGE" not in text and str(base.paths.archive) not in text   # 不含影像,也不含完整路徑
