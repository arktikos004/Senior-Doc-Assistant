"""歸檔模組的單元測試:檔名清洗、樣板命名與重複檔名處理。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.archiver import build_filename, sanitize, unique_path
from src.config import AppConfig
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
