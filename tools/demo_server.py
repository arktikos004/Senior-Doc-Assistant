"""展示用伺服器:在沒有 GPU / 模型的電腦上,也能看到完整的「看有」畫面流程。

用法(專案根目錄):
    python tools/demo_server.py            # 開在 http://127.0.0.1:8000
    python tools/demo_server.py --seed     # 先把示範文件跑過一遍,首頁就有資料可看

與正式執行的差別:
1. 資料全部放在 data/demo_run/(已 gitignore),**不讀也不動** data/archive、data/review、logs/
   裡的真實文件,展示畫面不會出現任何真實個資。
2. 辨識改用 DemoAnalyzer:對 tools/make_demo_docs.py 與 make_sample.py --einvoice 產生的
   合成文件,回傳「模型讀到的結果」的示範值(依內容雜湊比對),每筆都註明是展示模式。
   驗證(QR 解碼、統編檢查碼…)、決策、行動、網頁全部是真的程式在跑。
   其他照片則依使用者選的類型回傳通用示範值。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import BASE_DIR, AppConfig, PathsConfig  # noqa: E402
from src.models import ExtractionResult  # noqa: E402

DEMO_ROOT = BASE_DIR / "data" / "demo_run"
DEMO_DOCS = BASE_DIR / "data" / "samples" / "demo"
EINVOICE_DIR = BASE_DIR / "data" / "samples" / "einvoice"
DEMO_NOTE = "展示模式:這是模擬的模型讀值,驗證、決策與行動是真實程式的結果"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bill() -> ExtractionResult:
    return ExtractionResult(
        doc_type="帳單", date="2026-10-01", vendor="範例電力公司", amount=1286.0, confidence=0.92,
        fields={"due_date": "2026-10-20", "bill_kind": "電費"},
        plain_summary="這是範例電力公司的電費帳單,本期要繳 1,286 元,請在 2026 年 10 月 20 日前到超商或銀行繳費。",
    )


def _medication() -> ExtractionResult:
    return ExtractionResult(
        doc_type="藥袋", date="2026-09-30", vendor="範例診所", confidence=0.90,
        fields={
            "items": [
                {"name": "乙醯胺酚錠 500 毫克", "dose_text": "每次 1 錠", "frequency_text": "每日三次,早午晚飯後",
                 "timing": ["早", "中", "晚"], "prn": False, "days": 7},
                {"name": "氯苯那敏錠 4 毫克", "dose_text": "每次 1 錠", "frequency_text": "需要時(皮膚癢時)服用",
                 "timing": [], "prn": True, "days": 0},
            ],
            "pharmacist_phone": "00-0000-0000",
        },
        plain_summary="範例診所開了兩種藥:乙醯胺酚錠每天早、午、晚飯後各吃 1 顆,共 7 天;"
                      "氯苯那敏錠在皮膚癢的時候才吃,藥袋上寫可能會想睡,吃了不要開車。",
    )


def _letter() -> ExtractionResult:
    return ExtractionResult(
        doc_type="公文", date="2026-09-28", vendor="範例市稅務局", confidence=0.90,
        fields={
            "subject": "台端申請房屋稅減免一案,請於收到本函後15日內檢附相關證明文件補正,逾期未補正者,依規定不予受理。",
            "doc_number": "範稅字第1150000001號",
            "deadline_text": "收到本函後15日內",
            "required_actions": ["補交戶口名簿影本", "補交房屋使用現況照片"],
        },
        plain_summary="稅務局通知您:房屋稅減免的申請還缺文件,請在收到信後 15 天內補交戶口名簿影本和房屋照片,晚了就不受理。",
    )


_BY_TYPE = {"帳單": _bill, "藥袋": _medication, "公文": _letter}
_DEMO_FILES = {"範例帳單_電費.png": _bill, "範例藥袋.png": _medication, "範例公文_補正通知.png": _letter}


def _einvoice_results() -> dict[str, ExtractionResult]:
    """合成電子發票:模型讀的是「印刷」金額(金額不符那張會與 QR 矛盾,展示驗證攔截)。"""
    labels = EINVOICE_DIR / "labels.csv"
    out: dict[str, ExtractionResult] = {}
    if not labels.exists():
        return out
    with open(labels, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            path = EINVOICE_DIR / row["檔名"]
            if not path.exists():
                continue
            printed = float(row["printed_total"])
            out[_sha(path)] = ExtractionResult(
                doc_type="發票", date=row["date"], vendor=row["vendor"], amount=printed,
                invoice_number=row["invoice_number"], confidence=0.86,
                fields={"seller_tax_id": row["seller_tax_id"], "random_code": row["random_code"],
                        "period": row["period"], "buyer_tax_id": "00000000"},
                plain_summary=f"這是{row['vendor']}的電子發票,{row['date']} 消費 {printed:,.0f} 元。",
            )
    return out


class DemoAnalyzer:
    """依內容雜湊回傳合成文件的示範讀值;認不得的照片依使用者選的類型給通用值。"""

    def __init__(self):
        self.known: dict[str, ExtractionResult] = _einvoice_results()
        for name, make in _DEMO_FILES.items():
            path = DEMO_DOCS / name
            if path.exists():
                self.known[_sha(path)] = make()

    def analyze(self, file_path: Path, doc_type_hint: str | None = None, *,
                local_only: bool = False) -> ExtractionResult:
        # local_only 不用看:展示讀值是預錄的,不會送出這台電腦
        base = self.known.get(_sha(file_path))
        if base is None:
            make = _BY_TYPE.get(doc_type_hint or "")
            base = make() if make else ExtractionResult(
                doc_type=doc_type_hint or "收據", date=date.today().isoformat(), vendor="示範商店",
                amount=320.0, confidence=0.70, plain_summary="展示模式無法辨識這張照片,以下是示範內容。")
        result = ExtractionResult(**{**base.__dict__, "fields": dict(base.fields)})
        result.notes = DEMO_NOTE
        result.source_model = "demo:模擬讀值"
        return result


def demo_config() -> AppConfig:
    paths = PathsConfig(
        inbox=DEMO_ROOT / "inbox", archive=DEMO_ROOT / "archive", review=DEMO_ROOT / "review",
        failed=DEMO_ROOT / "failed", logs=DEMO_ROOT / "logs", uploads=DEMO_ROOT / "uploads",
    )
    cfg = AppConfig(paths=paths, provider="mock")
    cfg.ensure_dirs()
    return cfg


def seed(cfg: AppConfig, analyzer: DemoAnalyzer) -> None:
    """把示範文件各跑一次:帳單、藥袋、公文、兩張發票(其中一張金額不符)。"""
    from src.pipeline import Pipeline

    pipeline = Pipeline(cfg, analyzer)
    picks = [(DEMO_DOCS / n, t) for n, t in (("範例帳單_電費.png", "帳單"), ("範例藥袋.png", "藥袋"),
                                              ("範例公文_補正通知.png", "公文"))]
    picks += [(EINVOICE_DIR / "e01_電子發票.png", "發票"), (EINVOICE_DIR / "e11_電子發票_金額不符.png", "發票")]
    for src, hint in picks:
        if not src.exists():
            print(f"略過(找不到):{src.name}")
            continue
        dest = cfg.paths.uploads_path / src.name
        shutil.copy2(src, dest)
        record = pipeline.process_file(dest, hint)
        print(f"{src.name} → {record['動作']}({record['原因']})")


def main() -> int:
    parser = argparse.ArgumentParser(description="看有 展示伺服器(模擬辨識,資料隔離)")
    parser.add_argument("--seed", action="store_true", help="啟動前先處理一輪示範文件")
    parser.add_argument("--reset", action="store_true", help="清空 data/demo_run/ 後再開始")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.reset and DEMO_ROOT.exists():
        shutil.rmtree(DEMO_ROOT)
    cfg = demo_config()
    analyzer = DemoAnalyzer()
    if args.seed:
        seed(cfg, analyzer)

    import uvicorn

    from web.app import create_app

    print(f"展示資料夾:{DEMO_ROOT}")
    uvicorn.run(create_app(cfg, analyzer=analyzer), host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
