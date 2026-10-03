"""評測核心:標準答案比對與信心門檻校準(供 tools/evaluate.py 使用)。

- 標準答案檔:樣本資料夾裡的 labels.csv(欄位:檔名,doc_type,date,vendor,amount,invoice_number;
  多的欄位不讀)。預設樣本是合成電子發票 data/samples/einvoice/(tools/make_sample.py --einvoice)。
- 「正確」定義:doc_type、date、amount 三個關鍵欄位全對(vendor 為輔助指標,
  商家全名與簡稱難以嚴格比對,採雙向包含即算對,不納入整體正確)
- 每個樣本和 Pipeline 一樣先核對(verify_result)再看分數(evaluate_file)。
- 門檻校準:對「決策實際比的分數(decision.effective_score:有驗證信心用它,否則用模型自評)
  vs 實際正確」掃描門檻,找出滿足「自動歸檔正確率 ≥ 目標(預設 0.90)」下自動化比例最高的門檻
"""
from __future__ import annotations

import csv
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .decision import effective_score
from .models import ExtractionResult
from .verify import RULE_FLOOR, RULE_SPAN, verify_or_flag

_NON_ALNUM = re.compile(r"[^0-9A-Za-z一-鿿]+")

# 只有規則核對(沒有 QR 之類的獨立證據)時,驗證信心最高只到 0.60 + 0.25 = 0.85(見 src/verify)
RULE_ONLY_MAX_SCORE = RULE_FLOOR + RULE_SPAN


@dataclass
class GroundTruth:
    filename: str
    doc_type: str
    date: str | None = None
    vendor: str | None = None
    amount: float | None = None
    invoice_number: str | None = None


def load_labels(csv_path: Path) -> dict[str, GroundTruth]:
    """讀取標準答案 CSV,回傳 檔名 → GroundTruth。空欄位視為「不比對」。"""
    labels: dict[str, GroundTruth] = {}
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("檔名") or "").strip()
            if not name:
                continue
            amount_raw = (row.get("amount") or "").strip()
            labels[name] = GroundTruth(
                filename=name,
                doc_type=(row.get("doc_type") or "").strip(),
                date=(row.get("date") or "").strip() or None,
                vendor=(row.get("vendor") or "").strip() or None,
                amount=float(amount_raw) if amount_raw else None,
                invoice_number=(row.get("invoice_number") or "").strip() or None,
            )
    return labels


def _norm(text: str | None) -> str:
    return _NON_ALNUM.sub("", text or "")


def compare(result: ExtractionResult, truth: GroundTruth) -> dict[str, Any]:
    """逐欄位比對辨識結果與標準答案。標準答案留空的欄位不納入評分。"""
    fields: dict[str, bool] = {}
    fields["doc_type"] = result.doc_type == truth.doc_type
    if truth.date:
        fields["date"] = (result.date or "") == truth.date
    if truth.amount is not None:
        fields["amount"] = result.amount is not None and abs(result.amount - truth.amount) < 0.5
    if truth.invoice_number:
        fields["invoice_number"] = _norm(result.invoice_number) == _norm(truth.invoice_number)

    # vendor 採寬鬆雙向包含(全聯實業股份有限公司 vs 全聯實業),僅供參考不納入整體
    vendor_ok: bool | None = None
    if truth.vendor:
        a, b = _norm(result.vendor), _norm(truth.vendor)
        vendor_ok = bool(a) and (a in b or b in a)

    key_fields = [v for k, v in fields.items() if k in ("doc_type", "date", "amount")]
    return {
        "fields": fields,
        "vendor_ok": vendor_ok,
        "correct": all(key_fields),
        "confidence": result.confidence,        # 模型自評,只當參考
        "score": effective_score(result),       # 決策實際比門檻的分數
    }


def evaluate_file(analyzer, path: Path, truth: GroundTruth) -> dict[str, Any]:
    """對一個樣本跑「辨識 → 核對 → 與標準答案比對」,回傳逐筆紀錄。

    和 Pipeline 一樣先跑 verify_result(讀原檔)再看分數,score 才會是決策實際比的那個值;
    核對程式出錯時也和 Pipeline 一樣把分數設成 0(轉人工),不退回模型自評。
    辨識失敗(逾時、影像解不開、JSON 解析失敗)記成錯誤列:不算正確、score 0。耗時秒只計模型辨識。
    """
    t0 = time.perf_counter()
    try:
        result = analyzer.analyze(path)
    except Exception as exc:
        return {
            "檔名": truth.filename, "耗時秒": round(time.perf_counter() - t0, 2),
            "correct": False, "confidence": 0.0, "score": 0.0, "fields": {}, "vendor_ok": None,
            "辨識結果": None, "錯誤": f"{type(exc).__name__}: {exc}",
        }
    elapsed = time.perf_counter() - t0
    verify_or_flag(result, path)
    row = compare(result, truth)
    row.update({"檔名": truth.filename, "耗時秒": round(elapsed, 2), "辨識結果": result.to_dict(), "錯誤": None})
    return row


def sweep_thresholds(
    records: list[dict[str, Any]],
    thresholds: list[float] | None = None,
) -> list[dict[str, float]]:
    """records 需含 score(決策實際比的分數,見 compare)與 correct 欄位。

    回傳每個門檻下的:自動化比例(分數達標比例)、自動歸檔正確率、涵蓋的正確筆數。
    """
    thresholds = thresholds or [round(0.50 + 0.05 * i, 2) for i in range(10)]
    rows = []
    n = len(records)
    for t in thresholds:
        auto = [r for r in records if r["score"] >= t]
        n_auto = len(auto)
        n_correct = sum(1 for r in auto if r["correct"])
        rows.append(
            {
                "threshold": t,
                "automation_rate": n_auto / n if n else 0.0,
                "auto_precision": n_correct / n_auto if n_auto else float("nan"),
                "n_auto": n_auto,
            }
        )
    return rows


def recommend_threshold(
    rows: list[dict[str, float]], target_precision: float = 0.90
) -> dict[str, float] | None:
    """在自動歸檔正確率 ≥ 目標的門檻中,選自動化比例最高(即門檻最低)者。"""
    qualified = [
        r
        for r in rows
        if r["n_auto"] > 0 and r["auto_precision"] == r["auto_precision"]  # 排除 NaN
        and r["auto_precision"] >= target_precision
    ]
    if not qualified:
        return None
    return max(qualified, key=lambda r: (r["automation_rate"], -r["threshold"]))


def threshold_warning(threshold: float) -> str | None:
    """建議門檻高於 RULE_ONLY_MAX_SCORE(0.85)時回傳警告,否則 None。

    只有規則核對的文件(收據、帳單、公文、藥袋,以及 QR 讀不到的發票)驗證信心最高 0.85,
    門檻設得比它高,這些文件會全部轉人工。
    """
    if threshold <= RULE_ONLY_MAX_SCORE:
        return None
    return (
        f"建議門檻 {threshold:.2f} 高於 {RULE_ONLY_MAX_SCORE:.2f}:只有規則核對的類型"
        f"(收據、帳單、公文、藥袋,以及 QR 讀不到的發票)驗證信心最高 {RULE_ONLY_MAX_SCORE:.2f},"
        "用這個門檻會全部轉人工。"
    )
