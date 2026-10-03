"""合成發票/收據樣本工具(tools/make_sample.py 預設模式)的測試:店名虛構、預設輸出位置、不蓋掉既有標準答案。

樣本只寫進 tmp_path;--einvoice 的端到端測試在 test_verify_samples.py。
"""
import csv
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import make_sample  # noqa: E402
from src.verify.checks import tax_id_checksum_ok  # noqa: E402


def test_vendors_are_fictional():
    """店名一律帶「示範」(比照電子發票樣本),不是真實商家。"""
    assert all("示範" in vendor for vendor, _ in make_sample.VENDORS)
    assert "示範" in make_sample.LEGACY_VENDOR


def test_legacy_sample_uses_checksum_valid_fake_tax_id(tmp_path):
    name, row = make_sample.make_legacy_sample(tmp_path)
    assert (tmp_path / name).is_file() and row["vendor"] == make_sample.LEGACY_VENDOR
    assert tax_id_checksum_ok(make_sample.fake_tax_id(random.Random(make_sample._LEGACY_TAX_SEED)))


def test_default_output_is_the_synthetic_folder(tmp_path, monkeypatch):
    assert make_sample.SYNTHETIC_DIR == make_sample.SAMPLES_DIR / "synthetic"
    monkeypatch.setattr(make_sample, "SYNTHETIC_DIR", tmp_path / "synthetic")
    assert make_sample.main(["--count", "1"]) == 0
    with open(tmp_path / "synthetic" / "labels.csv", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["檔名"] for r in rows][0] == "合成發票範例.png" and len(rows) == 2
    assert all((tmp_path / "synthetic" / r["檔名"]).is_file() for r in rows)


def test_existing_labels_are_kept_unless_forced(tmp_path, capsys):
    """data/samples/labels.csv 是真實樣本的標準答案:沒加 --force 就什麼都不寫。"""
    labels = tmp_path / "labels.csv"
    labels.write_text("檔名\n真的.jpg\n", encoding="utf-8")
    assert make_sample.main(["--out", str(tmp_path), "--count", "1"]) == 1
    assert "--force" in capsys.readouterr().err
    assert labels.read_text(encoding="utf-8") == "檔名\n真的.jpg\n"
    assert [p.name for p in tmp_path.iterdir()] == ["labels.csv"]       # 影像也沒寫

    assert make_sample.main(["--out", str(tmp_path), "--count", "1", "--force"]) == 0
    assert labels.read_text(encoding="utf-8-sig").splitlines()[0] == ",".join(make_sample.LABEL_FIELDS)


def test_legacy_only_rewrites_the_tracked_example(tmp_path, monkeypatch):
    monkeypatch.setattr(make_sample, "SAMPLES_DIR", tmp_path)
    assert make_sample.main(["--legacy"]) == 0
    assert [p.name for p in tmp_path.iterdir()] == ["合成發票範例.png"]   # 不寫標準答案


@pytest.mark.parametrize("mode", ["--einvoice", "--legacy"])
def test_fixed_outputs_cannot_be_redirected(tmp_path, mode):
    with pytest.raises(SystemExit) as exc:
        make_sample.main([mode, "--out", str(tmp_path)])
    assert exc.value.code == 2
    assert not any(tmp_path.iterdir())
