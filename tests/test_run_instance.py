"""tools/run_instance.py:另開一份獨立的實例(例如現場展示)。資料全部在 --root,不碰正式資料;不真的開伺服器、不連網。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import run_instance  # noqa: E402
from src.config import AppConfig  # noqa: E402
from src.store import Store  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """假的專案資料夾:--root 不能放在它裡面。"""
    fake = tmp_path / "repo"
    fake.mkdir()
    monkeypatch.setattr(run_instance, "BASE_DIR", fake)
    monkeypatch.setenv("KANYOU_ACCESS_AUD", "x")   # 先記下原狀,測完還原(main 會直接改這個行程的環境變數)
    monkeypatch.delenv("KANYOU_ACCESS_AUD")
    return fake


def _run(args, calls=None):
    calls = [] if calls is None else calls
    code = run_instance.main(args, serve=lambda app, **kw: calls.append((app, kw)))
    return code, calls


def test_everything_lives_under_the_root_and_only_listens_locally(repo, tmp_path):
    root = tmp_path / "demo"
    code, [(app, kw)] = _run(["--root", str(root), "--port", "8100"])
    assert code == 0 and kw == {"host": "127.0.0.1", "port": 8100, "proxy_headers": True,
                                "forwarded_allow_ips": "127.0.0.1"}
    assert (root / run_instance.MARKER).is_file() and (root / "logs" / "app.db").is_file()
    cfg = run_instance.instance_config(AppConfig(), root)
    for path in (cfg.paths.inbox, cfg.paths.archive, cfg.paths.review, cfg.paths.failed, cfg.paths.logs,
                 cfg.paths.uploads_path, cfg.paths.db_path):
        assert path.is_relative_to(root)


@pytest.mark.parametrize("where", ["inside", "repo"])
def test_refuses_a_root_inside_the_project(repo, where):
    root = repo / "data" / "demo" if where == "inside" else repo
    code, calls = _run(["--root", str(root)])
    assert code == 2 and calls == [] and not (root / run_instance.MARKER).exists()


def test_refuses_a_folder_it_did_not_create(repo, tmp_path, capsys):
    other = tmp_path / "Documents"
    other.mkdir()
    (other / "family-photos.txt").write_text("x", encoding="utf-8")
    code, calls = _run(["--root", str(other), "--reset"])
    assert code == 2 and calls == [] and (other / "family-photos.txt").is_file()
    assert "不是這支程式建的" in capsys.readouterr().out


def test_reset_clears_only_its_own_folders(repo, tmp_path):
    root = tmp_path / "demo"
    _run(["--root", str(root)])
    (root / "archive" / "old.png").parent.mkdir(parents=True, exist_ok=True)
    (root / "archive" / "old.png").write_bytes(b"x")
    (root / "notes.txt").write_text("keep", encoding="utf-8")
    assert _run(["--root", str(root), "--reset"])[0] == 0
    assert not (root / "archive" / "old.png").exists() and (root / "notes.txt").is_file()


def test_members_and_access_audience_apply_to_this_instance_only(repo, tmp_path, monkeypatch):
    root = tmp_path / "demo"
    code, _ = _run(["--root", str(root), "--access-aud", "demo-aud-123",
                    "--member", "demo-family@example.test:family:家人", "--member", "demo-elder@example.test:elder"])
    assert code == 0
    import os
    assert os.environ["KANYOU_ACCESS_AUD"] == "demo-aud-123"
    members = {m["email"]: (m["role"], m["display_name"]) for m in Store(root / "logs" / "app.db").list_members()}
    assert members == {"demo-family@example.test": ("family", "家人"), "demo-elder@example.test": ("elder", None)}


@pytest.mark.parametrize("member", ["no-at-sign:family", "a@example.test:admin", "a@example.test"])
def test_bad_member_is_refused_before_anything_starts(repo, tmp_path, member):
    root = tmp_path / "demo"
    code, calls = _run(["--root", str(root), "--member", member])
    assert code == 2 and calls == [] and not root.exists()
