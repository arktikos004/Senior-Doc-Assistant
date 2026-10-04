"""歸檔模組的單元測試:檔名清洗、樣板命名、重複檔名處理與搬檔(搬不動不留複本)。"""
import errno
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.archiver import (
    SIDECAR_SUFFIX,
    archive_file,
    build_filename,
    move_to_failed,
    move_to_review,
    sanitize,
    unique_path,
)
from src.config import AppConfig, PathsConfig
from src.models import ExtractionResult

CFG = AppConfig()


def test_sanitize_removes_invalid_chars():
    assert sanitize('全聯/實業:股份*有限公司?') == "全聯實業股份有限公司"


def test_sanitize_empty_becomes_unknown():
    assert sanitize("???") == "未知"


def test_sanitize_limits_length():
    assert len(sanitize("很" * 100)) == 30


def test_build_filename_full_fields():
    result = ExtractionResult(
        doc_type="發票", date="2026-07-04", vendor="全聯實業",
        amount=1250.0, confidence=0.9,
    )
    assert build_filename(result, CFG, ".JPG") == "20260704_發票_全聯實業_1250.jpg"


def test_build_filename_missing_fields():
    result = ExtractionResult(doc_type="收據", confidence=0.9)
    name = build_filename(result, CFG, ".png")
    assert name == "未知日期_收據_未知商家_未知金額.png"


def test_unique_path_appends_counter(tmp_path):
    first = tmp_path / "a.jpg"
    assert unique_path(first) == first  # 不存在 → 原名
    first.touch()
    second = unique_path(first)
    assert second.name == "a-1.jpg"
    second.touch()
    assert unique_path(first).name == "a-2.jpg"


# ---- 搬檔:同一個磁碟直接改名;搬不動時原檔留在原位,目的地不留複本 -------------------------

BILL = ExtractionResult(doc_type="帳單", date="2026-10-01", vendor="範例電力公司", amount=1286.0, confidence=0.9)
MOVES = {
    "archive": lambda path, cfg: archive_file(path, BILL, cfg),
    "review": lambda path, cfg: move_to_review(path, BILL, "驗證信心偏低", cfg),
    "failed": lambda path, cfg: move_to_failed(path, cfg),
}


def _tree(tmp_path) -> tuple[AppConfig, Path]:
    cfg = AppConfig(paths=PathsConfig(inbox=tmp_path / "inbox", archive=tmp_path / "archive",
                                      review=tmp_path / "review", failed=tmp_path / "failed", logs=tmp_path / "logs"))
    cfg.ensure_dirs()
    source = cfg.paths.inbox / "a.png"
    source.write_bytes(b"synthetic")
    return cfg, source


def _files(cfg: AppConfig) -> list[str]:
    """archive/、review/、failed/ 裡所有的檔(含 sidecar)。"""
    return sorted(p.name for folder in (cfg.paths.archive, cfg.paths.review, cfg.paths.failed)
                  for p in folder.rglob("*") if p.is_file())


def _busy(monkeypatch, source: Path, rename_errno: int) -> None:
    """假裝 source 被別的程式開著:刪不掉(Windows 的 WinError 32);改名回 rename_errno。"""
    real_unlink = os.unlink

    def rename(src, dst, *args, **kwargs):
        raise OSError(rename_errno, "改名失敗", str(src), None, str(dst))

    def unlink(path, *args, **kwargs):
        if Path(path) == source:
            raise PermissionError(errno.EACCES, "檔案正由另一個程序使用", str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "unlink", unlink)


@pytest.mark.parametrize("move", sorted(MOVES))
def test_file_that_cannot_be_moved_stays_put_and_leaves_no_copy(tmp_path, monkeypatch, move):
    """原檔被別的程式開著(讀得到,但改名與刪除都被擋):丟例外,原檔不動,目的地不多出一份複本。"""
    cfg, source = _tree(tmp_path)
    _busy(monkeypatch, source, errno.EACCES)
    with pytest.raises(PermissionError):
        MOVES[move](source, cfg)
    assert source.read_bytes() == b"synthetic" and _files(cfg) == []


@pytest.mark.parametrize("move", sorted(MOVES))
def test_move_to_another_disk_removes_the_copy_when_the_original_is_stuck(tmp_path, monkeypatch, move):
    """目的地在另一個磁碟(改名回 EXDEV)只能複製再刪原檔:原檔刪不掉就把複本刪掉再丟例外。"""
    cfg, source = _tree(tmp_path)
    _busy(monkeypatch, source, errno.EXDEV)
    with pytest.raises(PermissionError):
        MOVES[move](source, cfg)
    assert source.read_bytes() == b"synthetic" and _files(cfg) == []


def test_move_to_another_disk_copies_then_removes_the_original(tmp_path, monkeypatch):
    cfg, source = _tree(tmp_path)

    def rename(src, dst, *args, **kwargs):
        raise OSError(errno.EXDEV, "不同的磁碟", str(src), None, str(dst))

    monkeypatch.setattr(os, "rename", rename)
    target = archive_file(source, BILL, cfg)
    assert target.read_bytes() == b"synthetic" and not source.exists()
    assert target == cfg.paths.archive / "帳單" / "2026-10" / "20261001_帳單_範例電力公司_1286.png"


def test_review_sidecar_that_cannot_be_written_leaves_the_file_where_it_was(tmp_path, monkeypatch):
    """轉人工要附辨識結果檔:寫不了(磁碟滿、被擋)就整個不搬。不能原檔已經進了 review/ 才丟例外——
    呼叫端會當成沒搬成,把已經沒有檔的舊位置記進資料庫。"""
    cfg, source = _tree(tmp_path)
    real_write = Path.write_text

    def write_text(self, *args, **kwargs):
        if self.name.endswith(SIDECAR_SUFFIX):
            raise OSError(errno.ENOSPC, "磁碟滿了", str(self))
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)
    with pytest.raises(OSError):
        move_to_review(source, BILL, "驗證信心偏低", cfg)
    assert source.read_bytes() == b"synthetic" and _files(cfg) == []


def test_move_on_the_same_disk_is_a_rename(tmp_path):
    cfg, source = _tree(tmp_path)
    target = move_to_review(source, BILL, "驗證信心偏低", cfg)
    assert target == cfg.paths.review / "a.png" and target.read_bytes() == b"synthetic" and not source.exists()
    assert _files(cfg) == ["a.png", "a.png.ai.json"]
