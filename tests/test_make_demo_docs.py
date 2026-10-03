"""Demo 合成文件產生器測試:能產出三張影像,且都是虛構內容。"""
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import make_demo_docs  # noqa: E402


def test_generates_all_demo_docs(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["make_demo_docs.py", "--out", str(tmp_path)])
    assert make_demo_docs.main() == 0
    names = sorted(p.name for p in tmp_path.glob("*.png"))
    assert names == sorted(make_demo_docs.DOCS)
    for p in tmp_path.glob("*.png"):
        with Image.open(p) as img:
            assert img.width >= 1000 and img.height >= 1400


def test_all_names_are_marked_as_sample():
    # 虛構文件的檔名一律帶「範例」,避免和真實文件混在一起
    assert all(name.startswith("範例") for name in make_demo_docs.DOCS)
