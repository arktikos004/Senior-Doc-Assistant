"""全家共用的系統設定(SET)單元測試:驗證規則、目前生效的設定、匯出與刪除全部資料(Store.purge_all)。

資料夾全部在 tmp_path;雲端金鑰用 monkeypatch 設定假的環境變數,不連網、不呼叫模型。
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from src.config import BASE_DIR, AppConfig, PathsConfig
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
    hide_local_paths,
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


@pytest.mark.parametrize("low", [0.5, 0.79, 0.0, -1.0, float("nan")])
def test_config_yaml_threshold_below_080_takes_effect_as_080(base, store, low):
    """config.yaml 的門檻也守設定頁那條底線:寫得比 0.80 低(或不是數字)就以 0.80 生效。"""
    base.auto_threshold = low
    assert effective_config(base, store).auto_threshold == 0.80
    store.set_setting(THRESHOLD, "0.90")                        # 設定頁存的值照樣蓋過去
    assert effective_config(base, store).auto_threshold == 0.90
    store.set_setting(THRESHOLD, "0.5")                         # 存的值不合規則:回到底線,不是 config.yaml 的低值
    assert effective_config(base, store).auto_threshold == 0.80


@pytest.mark.parametrize("strict", [0.80, 0.9, 0.99])
def test_config_yaml_threshold_at_or_above_080_is_kept(base, store, strict):
    base.auto_threshold = strict                                # 比底線嚴的照 config.yaml
    assert effective_config(base, store).auto_threshold == strict


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


@pytest.fixture
def link_dir():
    """建資料夾連結的函式 link_dir(連結, 目標):Windows 用 junction(一般帳號就能建;is_symlink() 對它是
    False,os.walk 照樣走進去),其他系統用符號連結。測試結束時只把連結本身拿掉,不動它指到的資料夾。"""
    made = []

    def make(link, target):
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
        else:
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                pytest.skip("這個系統不讓一般使用者建符號連結")
        made.append(link)
        return link

    yield make
    for link in made:
        if os.path.lexists(link):
            (os.rmdir if os.name == "nt" else os.unlink)(link)


@pytest.fixture
def walked(monkeypatch):
    """記下 os.walk 走過的每個資料夾。"""
    seen = []
    real_walk = os.walk

    def walk(top, *args, **kwargs):
        for item in real_walk(top, *args, **kwargs):
            seen.append(Path(item[0]))
            yield item

    monkeypatch.setattr(os, "walk", walk)
    return seen


def test_folder_links_are_not_entered(base, tmp_path, link_dir, walked):
    """資料夾連結不走進去:指到外面的,外面的檔案一個都不少、連結本身留著;指到大資料夾也不會走完整棵樹。"""
    canary = _write(tmp_path / "outside" / "canary.png")
    deep = _write(tmp_path / "outside" / "sub" / "deep.png")
    link = link_dir(base.paths.uploads_path / "j-out", canary.parent)
    inside = [_write(base.paths.uploads_path / "u.jpg"), _write(base.paths.uploads_path / "2026-10" / "n.jpg")]
    assert delete_data_files(base) == (2, 0)
    assert canary.exists() and deep.exists() and os.path.lexists(link)
    assert not any(p.exists() for p in inside) and not (base.paths.uploads_path / "2026-10").exists()
    assert not [p for p in walked if p == link or link in p.parents]


def test_folder_link_looping_back_is_not_walked_or_counted_as_stuck(base, link_dir, walked):
    """連結繞回資料夾自己:不沿著迴圈一路走下去,同一個檔不會被算成很多個「刪不掉」。"""
    uploads = base.paths.uploads_path
    inside = [_write(uploads / "u.jpg"), _write(uploads / "2026-10" / "n.jpg")]
    link = link_dir(uploads / "loop", uploads)
    assert delete_data_files(base) == (2, 0)
    assert not any(p.exists() for p in inside) and not (uploads / "2026-10").exists()
    assert os.path.lexists(link) and uploads.is_dir()
    assert not [p for p in walked if p == link or link in p.parents]


def test_folder_link_to_another_data_subfolder_is_left_alone(base, link_dir, walked):
    """指到同一個資料夾裡別處的連結:裡面的檔案照實際位置刪一次,連結本身不刪、也不走進去。"""
    bill = _write(base.paths.archive / "帳單" / "2026-10" / "b.png")
    link = link_dir(base.paths.archive / "alias", bill.parent.parent)
    assert delete_data_files(base) == (1, 0)
    assert not bill.exists() and os.path.lexists(link)
    assert not [p for p in walked if p == link or link in p.parents]
    # 它指到的資料夾空了、被收掉之後,連結變成指到不存在的位置:再刪一次也還是不動它
    assert not bill.parent.parent.exists()
    assert delete_data_files(base) == (0, 0) and os.path.lexists(link)


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


def test_file_that_is_already_gone_is_not_counted_as_stuck(base, monkeypatch):
    """輪到它之前就不見的檔(例如兩次刪除同時跑,另一邊先刪了):已經不在,不算刪不掉,也不算這次刪掉的。"""
    gone = _write(base.paths.review / "gone.png")
    _write(base.paths.review / "free.png")
    real_unlink = type(gone).unlink

    def unlink(self, *args, **kwargs):
        if self.name == "gone.png":
            real_unlink(self)                                   # 另一邊先刪掉了
        return real_unlink(self, *args, **kwargs)               # 這裡才刪:FileNotFoundError

    monkeypatch.setattr(type(gone), "unlink", unlink)
    assert delete_data_files(base) == (1, 0)
    assert not gone.exists()


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


# ---- 匯出的 error、reason 不帶這台電腦的完整路徑 -----------------------------------------

def _spellings(path) -> set[str]:
    """同一個位置在文字裡的寫法:原樣、斜線、例外訊息 repr 出來的雙反斜線。"""
    text = str(path)
    return {text, path.as_posix(), text.replace("\\", "\\\\")}


def test_export_error_and_reason_carry_no_local_paths(base, store, tmp_path):
    """處理失敗的文件,error、reason 裡可能有完整路徑(含帳號資料夾):匯出換成資料夾裡的相對位置。"""
    upload = base.paths.uploads_path / "20261001-101500-abcd1234.jpg"
    target = base.paths.archive / "帳單" / "2026-10" / "20261001_帳單_範例電力公司_1286.jpg"
    as_repr = store.add_document({
        "原始檔案": upload.name, "動作": "failed", "目標路徑": str(upload),
        "原因": f"自動存檔;搬移失敗:[WinError 32] 檔案正由另一個程序使用。: {str(upload)!r} -> {str(target)!r}",
        "錯誤": f"UnreadableImageError: 無法讀取影像 {upload.name}:cannot identify image file {str(upload)!r}"})
    plain = store.add_document({
        "原始檔案": "b.pdf", "動作": "failed", "原因": f"找不到 {base.paths.review / 'b.pdf'}",
        "錯誤": f"FileNotFoundError: {(base.paths.failed / 'b.pdf').as_posix()}"})
    untouched = store.add_document({"原始檔案": "c.png", "動作": "failed", "原因": "AI 辨識失敗", "錯誤": None})

    docs = {d["id"]: d for d in export_records(store, base, version="2026.10.03")["documents"]}

    for doc_id in (as_repr, plain):
        for key in ("error", "reason"):
            value = docs[doc_id][key]
            assert not any(s in value for s in _spellings(tmp_path)), value
            assert tmp_path.name not in value                    # 上層資料夾的名稱一段都不留
    assert "'uploads" in docs[as_repr]["error"] and upload.name in docs[as_repr]["error"]
    assert "'uploads" in docs[as_repr]["reason"] and "-> 'archive" in docs[as_repr]["reason"]
    assert docs[plain]["reason"].startswith("找不到 review") and docs[plain]["reason"].endswith("b.pdf")
    assert docs[plain]["error"] == "FileNotFoundError: failed/b.pdf"
    assert (docs[untouched]["reason"], docs[untouched]["error"]) == ("AI 辨識失敗", None)
    # 資料庫裡的原值不動:只有匯出的內容改寫
    assert repr(str(upload)) in store.get_document(as_repr)["reason"]


def test_hide_local_paths_knows_the_data_folders_project_and_home(base):
    archive = base.paths.archive
    sep = os.sep
    assert hide_local_paths(f"讀不到 {archive}{sep}a.png。", base) == f"讀不到 archive{sep}a.png。"
    assert hide_local_paths(f"'{archive.as_posix()}/a.png'", base) == "'archive/a.png'"
    assert hide_local_paths(str(archive).upper(), base) == "archive"          # Windows 的路徑不分大小寫
    assert hide_local_paths(str(base.paths.logs / "app.db"), base) == f"logs{sep}app.db"
    assert hide_local_paths(str(base.paths.inbox), base) == "inbox"
    assert hide_local_paths(str(BASE_DIR / "config.yaml"), base) == f".{sep}config.yaml"
    assert hide_local_paths(str(Path.home() / "x.png"), base) == f"~{sep}x.png"
    # 只是開頭相同的另一個資料夾不算 archive;不是字串的值、沒有路徑的文字原樣回傳
    assert not hide_local_paths(f"{archive}2{sep}a.png", base).startswith("archive2")
    assert hide_local_paths(None, base) is None and hide_local_paths("模型逾時", base) == "模型逾時"
