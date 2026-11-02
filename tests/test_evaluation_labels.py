"""評測標準答案的讀取:labels.jsonl(多類型合成評測集)與舊 labels.csv 並存、split 過濾、檔名防護。

標準答案都是測試當場寫進 tmp_path 的合成資料,不讀 data/ 底下任何檔案。
"""
import json

import pytest

from src.evaluation import LABELS_CSV, LABELS_JSONL, load_dataset, load_jsonl_labels, load_labels

BILL_ROW = {
    "file": "test/bill_power_03_glare.png", "split": "test", "template": "bill_power", "variant": 3,
    "condition": "glare", "doc_type": "帳單", "vendor": "示範電力公司", "date": "2026-09-20",
    "amount": 1854, "invoice_number": None,
    "fields": {"due_date": "2026-10-15", "bill_kind": "電費"},
    "qr": {"present": False, "readable": False},
}
LETTER_ROW = {
    "file": "dev/letter_tax_01_clean.png", "split": "dev", "template": "letter_tax", "variant": 1,
    "condition": "clean", "doc_type": "公文", "vendor": "示範區公所", "date": "2026-09-25", "amount": None,
    "invoice_number": None,
    "fields": {"subject": "請補繳文件", "doc_number": "示範字第1150000001號",
               "deadline_text": "收到本函後15日內", "deadline": "2026-10-10", "required_actions": ["補繳影本"]},
    "qr": {"present": False, "readable": False},
}
CSV_HEADER = "檔名,doc_type,date,vendor,amount,invoice_number,degrade\n"


def _write_jsonl(folder, rows, extra_lines=()):
    lines = [json.dumps(row, ensure_ascii=False) for row in rows] + list(extra_lines)
    (folder / LABELS_JSONL).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_csv(folder, body):
    (folder / LABELS_CSV).write_text(CSV_HEADER + body, encoding="utf-8")


# ---- labels.jsonl --------------------------------------------------------------------

def test_jsonl_fields_are_read_into_ground_truth(tmp_path):
    _write_jsonl(tmp_path, [BILL_ROW])
    truth = load_jsonl_labels(tmp_path / LABELS_JSONL)["test/bill_power_03_glare.png"]
    assert (truth.doc_type, truth.date, truth.amount, truth.vendor) == ("帳單", "2026-09-20", 1854.0, "示範電力公司")
    assert truth.fields == {"due_date": "2026-10-15", "bill_kind": "電費"}
    assert (truth.split, truth.template, truth.variant, truth.condition) == ("test", "bill_power", 3, "glare")
    assert truth.qr_present is False and truth.qr_readable is False
    assert truth.invoice_number is None and truth.source == LABELS_JSONL


def test_jsonl_blank_lines_skipped_and_missing_values_are_unscored(tmp_path):
    _write_jsonl(tmp_path, [{"file": "a.png", "doc_type": "公文"}], extra_lines=["", "   "])
    (truth,) = load_jsonl_labels(tmp_path / LABELS_JSONL).values()
    assert truth.date is None and truth.amount is None and truth.fields == {}
    assert truth.condition == "clean" and truth.split == "" and truth.variant is None
    assert truth.qr_present is False and truth.qr_readable is None   # 沒標就是不知道,不當成讀不到


def test_jsonl_bool_is_not_a_number(tmp_path):
    _write_jsonl(tmp_path, [{"file": "a.png", "doc_type": "收據", "amount": True, "variant": True}])
    (truth,) = load_jsonl_labels(tmp_path / LABELS_JSONL).values()
    assert truth.amount is None and truth.variant is None


def test_jsonl_qr_flags(tmp_path):
    _write_jsonl(tmp_path, [{"file": "a.png", "doc_type": "發票", "qr": {"present": True, "readable": False}},
                            {"file": "b.png", "doc_type": "發票", "qr": {"present": "true"}}])
    labels = load_jsonl_labels(tmp_path / LABELS_JSONL)
    assert labels["a.png"].qr_present is True and labels["a.png"].qr_readable is False
    assert labels["b.png"].qr_present is False   # 只認 JSON 的 true,字串不算


@pytest.mark.parametrize("bad", ["{不是 JSON", json.dumps({"file": "a.png"}), json.dumps(["a.png", "發票"]),
                                 json.dumps({"file": "  ", "doc_type": "發票"})])
def test_jsonl_broken_line_raises_with_line_number(tmp_path, bad):
    # 標準答案壞掉時寧可停下來,不默默少評幾筆
    _write_jsonl(tmp_path, [BILL_ROW], extra_lines=[bad])
    with pytest.raises(ValueError, match="第 2 行"):
        load_jsonl_labels(tmp_path / LABELS_JSONL)


# ---- 舊 labels.csv ---------------------------------------------------------------------

def test_csv_samples_are_einvoices_with_qr(tmp_path):
    _write_csv(tmp_path, "e01_電子發票.png,發票,2026-07-04,晨光示範超市,350,AB12345678,\n"
                         "e02_電子發票_模糊.png,,2026-07-05,,120,,模糊\n")
    labels = load_labels(tmp_path / LABELS_CSV)
    first, blurred = labels["e01_電子發票.png"], labels["e02_電子發票_模糊.png"]
    assert first.qr_present is True and first.condition == "clean" and first.split == ""
    assert first.source == LABELS_CSV and first.fields == {}
    assert blurred.doc_type == "發票" and blurred.condition == "模糊"   # doc_type 空白視為發票


def test_csv_without_degrade_column_still_loads(tmp_path):
    (tmp_path / LABELS_CSV).write_text(
        "檔名,doc_type,date,vendor,amount,invoice_number\nb.jpg,收據,2026-03-16,7-ELEVEN,89,\n", encoding="utf-8")
    truth = load_labels(tmp_path / LABELS_CSV)["b.jpg"]
    assert truth.doc_type == "收據" and truth.condition == "clean"   # 有寫類型就照寫的


# ---- load_dataset:兩種格式並存、split ----------------------------------------------------

def test_dataset_reads_both_formats_and_jsonl_wins(tmp_path):
    _write_jsonl(tmp_path, [BILL_ROW, {"file": "shared.png", "doc_type": "收據"}])
    _write_csv(tmp_path, "shared.png,發票,2026-07-04,,350,,\ne01.png,發票,2026-07-04,,350,,\n")
    dataset = load_dataset(tmp_path)
    assert [t.filename for t in dataset] == ["test/bill_power_03_glare.png", "shared.png", "e01.png"]
    assert [t.source for t in dataset] == [LABELS_JSONL, LABELS_JSONL, LABELS_CSV]
    assert dataset[1].doc_type == "收據"


def test_dataset_split_filter(tmp_path):
    _write_jsonl(tmp_path, [BILL_ROW, LETTER_ROW])
    _write_csv(tmp_path, "e01.png,發票,2026-07-04,,350,,\n")
    assert [t.doc_type for t in load_dataset(tmp_path, "dev")] == ["公文"]
    assert [t.doc_type for t in load_dataset(tmp_path, "test")] == ["帳單"]   # 舊 CSV 沒有 split,只在 all
    assert len(load_dataset(tmp_path, "all")) == len(load_dataset(tmp_path)) == 3
    with pytest.raises(ValueError, match="split"):
        load_dataset(tmp_path, "train")


def test_dataset_without_label_files_is_empty(tmp_path):
    assert load_dataset(tmp_path) == []


def test_dataset_only_csv_keeps_old_behaviour(tmp_path):
    _write_csv(tmp_path, "e01.png,發票,2026-07-04,,350,AB12345678,\n")
    (truth,) = load_dataset(tmp_path)
    assert (truth.doc_type, truth.date, truth.amount, truth.invoice_number) == ("發票", "2026-07-04", 350.0,
                                                                              "AB12345678")


@pytest.mark.parametrize("name", ["../outside.png", "sub/../../outside.png", "ABSOLUTE"])
def test_label_pointing_outside_samples_dir_is_rejected(tmp_path, name):
    # 評測只讀樣本資料夾裡的合成樣本:標準答案不能把評測導去讀別的資料夾(例如真實文件)
    folder = tmp_path / "samples"
    folder.mkdir()
    if name == "ABSOLUTE":
        name = str(tmp_path / "outside.png")
    _write_jsonl(folder, [{"file": name, "doc_type": "發票"}])
    with pytest.raises(ValueError, match="外面"):
        load_dataset(folder)
