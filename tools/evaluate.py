"""評測 CLI:模型比較 + 信心門檻校準。

用法(專案根目錄執行):
    # 先產生合成樣本與標準答案(data/samples/einvoice/,seed 固定可重現)
    python tools/make_sample.py --einvoice

    # 以標準答案評測單一本地模型(不搬動任何檔案,只讀樣本資料夾)
    python tools/evaluate.py --model gemma4:12b

    # 一次比較多個本地模型
    python tools/evaluate.py --model gemma4:12b --model mistral-small3.1

    # 指定其他合成樣本資料夾(資料夾內要有自己的 labels.csv)
    python tools/evaluate.py --model gemma4:12b --samples <合成樣本資料夾>

    # 評測 Cloudflare Workers AI 上的模型(需設定 CF_ACCOUNT_ID / CF_API_TOKEN)
    # ⚠ 影像會送到 Cloudflare:樣本資料夾只能放合成樣本
    python tools/evaluate.py --provider workers_ai --model @cf/google/gemma-4-26b-a4b-it

    # 沒有模型時先用 Mock 驗證整條評測流程跑得通
    python tools/evaluate.py --mock

只評測非中資的開源模型。

樣本:預設 data/samples/einvoice/(合成電子發票,有自己的 labels.csv,欄位與 src/evaluation.py 一致)。
不要指到 data/samples/ 根目錄:那裡的 labels.csv 可能是真實資料(個資),工具會印警告。
每個樣本和 Pipeline 一樣先核對(verify_result)再掃門檻,掃的是決策實際比的分數(decision.effective_score)。
報告寫到 docs/模型實測結果.md(每次整份覆寫);人寫的分析文件不會被動到。
評測不會搬移或修改樣本檔案,可反覆執行。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.providers import create_analyzer  # noqa: E402
from src.config import BASE_DIR, load_config  # noqa: E402
from src.evaluation import (  # noqa: E402
    evaluate_file,
    load_labels,
    recommend_threshold,
    sweep_thresholds,
    threshold_warning,
)

SAMPLES_ROOT = BASE_DIR / "data" / "samples"
DEFAULT_SAMPLES_DIR = SAMPLES_ROOT / "einvoice"   # tools/make_sample.py --einvoice 產生的合成電子發票
DOCS_DIR = BASE_DIR / "docs"
REPORT_PATH = DOCS_DIR / "模型實測結果.md"        # 自動產生、每次覆寫;不動人寫的模型比較報告
RESULTS_DIR = BASE_DIR / "logs" / "eval"


def samples_warning(samples_dir: Path) -> str | None:
    """樣本資料夾是 data/samples/ 根目錄時回傳警告,否則 None。"""
    if samples_dir.resolve() != SAMPLES_ROOT.resolve():
        return None
    return (
        "⚠️ 樣本資料夾是 data/samples/ 根目錄:那裡的 labels.csv 可能是真實資料(個資)。"
        "評測請用合成樣本,例如預設的 data/samples/einvoice/(python tools/make_sample.py --einvoice 產生)。"
    )


def _display(path: Path) -> str:
    """報告裡顯示的路徑:專案內用相對路徑,專案外只寫資料夾名稱(不把本機路徑寫進文件)。"""
    try:
        return path.resolve().relative_to(BASE_DIR).as_posix()
    except ValueError:
        return path.name


def _with_model(cfg, provider: str, model_name: str | None):
    """回傳指定 provider 與模型的設定副本(不改動原設定)。"""
    run_cfg = replace(cfg, provider=provider)
    if model_name is None:
        return run_cfg
    if provider == "workers_ai":
        return replace(run_cfg, workers_ai=replace(cfg.workers_ai, model=model_name))
    return replace(run_cfg, ollama=replace(cfg.ollama, model=model_name))


def _default_model(cfg, provider: str) -> str:
    return cfg.workers_ai.model if provider == "workers_ai" else cfg.ollama.model


def evaluate_model(
    cfg, model_name: str | None, mock: bool, provider: str = "ollama",
    samples_dir: Path = DEFAULT_SAMPLES_DIR,
) -> dict:
    """對樣本資料夾內所有有標準答案的檔案跑一次「辨識 → 核對 → 比對」,回傳評測結果。"""
    labels_csv = samples_dir / "labels.csv"
    labels = load_labels(labels_csv)
    if not labels:
        raise SystemExit(f"標準答案為空:{labels_csv}")

    run_cfg = _with_model(cfg, provider, model_name)
    if provider == "workers_ai" and not mock:
        # 評測沒有類型提示,經 RoutingAnalyzer 會全部走本機;要量雲端模型就直接用雲端 provider。
        # 注意:這會把樣本影像送到 Cloudflare,只能用合成樣本評測。
        from src.providers.workers_ai import WorkersAIAnalyzer

        analyzer = WorkersAIAnalyzer(run_cfg)
    else:
        analyzer = create_analyzer(run_cfg, mock=mock)
    label_name = "mock" if mock else (model_name or _default_model(cfg, provider))

    records = []
    for sample_index, (filename, truth) in enumerate(labels.items(), start=1):
        path = samples_dir / filename
        if not path.exists():
            print(f"  [略過] 找不到樣本 {sample_index:02d}")
            continue

        row = evaluate_file(analyzer, path, truth)   # 先核對再算分數,與 Pipeline 相同
        status = "✓" if row["correct"] else "✗"
        print(f"  {status} 樣本 {sample_index:02d}(模型自評 {row['confidence']:.2f}、"
              f"比門檻的分數 {row['score']:.2f}、{row['耗時秒']}s)")
        records.append(row)

    n = len(records)
    n_correct = sum(1 for r in records if r["correct"])
    times = [r["耗時秒"] for r in records]
    return {
        "模型": label_name,
        "樣本數": n,
        "整體正確率": n_correct / n if n else 0.0,
        "平均耗時秒": round(sum(times) / n, 2) if n else 0.0,
        "平均模型自評": round(sum(r["confidence"] for r in records) / n, 3) if n else 0.0,
        "辨識失敗": sum(1 for r in records if r["錯誤"]),
        "records": records,
    }


def _calibration_section(records: list[dict], target: float) -> str:
    rows = sweep_thresholds(records)
    best = recommend_threshold(rows, target_precision=target)

    lines = [
        "## 信心門檻校準",
        "",
        f"目標:自動歸檔文件的正確率 ≥ {target:.0%},在此前提下自動化比例越高越好。",
        "掃的是決策實際比的分數(`src/decision.py` 的 `effective_score`):每個樣本先跑核對,"
        "有驗證信心就用它,否則用模型自評。",
        "",
        "| 門檻 | 自動歸檔筆數 | 自動化比例 | 自動歸檔正確率 |",
        "|---|---|---|---|",
    ]
    for r in rows:
        precision = (
            f"{r['auto_precision']:.1%}" if r["auto_precision"] == r["auto_precision"] else "—"
        )
        lines.append(
            f"| {r['threshold']:.2f} | {r['n_auto']} | {r['automation_rate']:.1%} | {precision} |"
        )

    lines.append("")
    if best:
        lines += [
            f"**建議門檻:`auto_threshold: {best['threshold']:.2f}`**"
            f"（自動化比例 {best['automation_rate']:.1%}、自動歸檔正確率 {best['auto_precision']:.1%}）",
            "",
        ]
        warning = threshold_warning(best["threshold"])
        if warning:
            lines += [f"⚠️ {warning}", ""]
        lines.append("調整方式:編輯 `config.yaml` 的 `confidence.auto_threshold`。")
    else:
        lines += [
            f"⚠️ **沒有任何門檻能達到 {target:.0%} 的自動歸檔正確率。**",
            "",
            "代表比門檻的分數(驗證信心或模型自評)與實際正確率的相關性不足,可能的處理方向:",
            "換更大的模型(例如雲端 `gemma-4-26b-a4b-it`)、強化提示詞的信心評分原則、",
            "或提高必要欄位檢查的嚴格度(見 `src/decision.py`)。",
        ]
    return "\n".join(lines)


def _write_report(results: list[dict], target: float, samples_dir: Path, out: Path = REPORT_PATH) -> Path:
    lines = [
        "# 模型實測結果(自動產生)",
        "",
        "由 `python tools/evaluate.py` 自動產生,**每次執行整份覆寫**;"
        "分析、判斷與結論寫在[模型比較報告](模型比較報告.md)第 7 節。",
        f"樣本來源:`{_display(samples_dir)}/`,標準答案:同資料夾的 `labels.csv`(只能用合成樣本)。",
        "",
        "「正確」定義:`doc_type`、`date`、`amount` 三個關鍵欄位全數相符。",
        "",
        "## 模型比較",
        "",
        "| 模型 | 樣本數 | 整體正確率 | 平均耗時(秒/張) | 平均模型自評 | 辨識失敗 |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['模型']} | {r['樣本數']} | {r['整體正確率']:.1%} "
            f"| {r['平均耗時秒']} | {r['平均模型自評']} | {r['辨識失敗']} |"
        )
    lines += ["", ""]

    # 門檻校準以正確率最高的模型為準(即實際會部署的那個)
    best_model = max(results, key=lambda r: r["整體正確率"])
    lines.append(f"以下校準基於正確率最高的模型 **{best_model['模型']}**。")
    lines.append("")
    lines.append(_calibration_section(best_model["records"], target))
    lines += [
        "",
        "## 匿名逐筆明細",
        "",
        f"（模型:{best_model['模型']}）",
        "",
        "| 樣本 | 正確 | 模型自評 | 比門檻的分數 | 耗時(秒) | 錯誤欄位 |",
        "|---|---|---|---|---|---|",
    ]
    for sample_index, r in enumerate(best_model["records"], start=1):
        wrong = [k for k, ok in r["fields"].items() if not ok] or (["辨識失敗"] if r["錯誤"] else [])
        lines.append(
            f"| 樣本 {sample_index:02d} | {'✓' if r['correct'] else '✗'} | {r['confidence']:.2f} "
            f"| {r['score']:.2f} | {r['耗時秒']} | {'、'.join(wrong) or '—'} |"
        )
    lines.append("")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="模型比較與信心門檻校準")
    parser.add_argument("--provider", choices=["ollama", "workers_ai"], default="ollama",
                        help="推論來源:本地 Ollama 或 Cloudflare Workers AI(預設 ollama)")
    parser.add_argument("--model", action="append", default=None,
                        help="要評測的模型名稱,可重複指定以比較多個模型")
    parser.add_argument("--mock", action="store_true", help="使用 Mock 辨識器驗證評測流程(免 Ollama)")
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES_DIR,
                        help="樣本資料夾,內含 labels.csv(預設 data/samples/einvoice/,合成電子發票)")
    parser.add_argument("--target-precision", type=float, default=0.90,
                        help="自動歸檔正確率目標(預設 0.90)")
    args = parser.parse_args()

    samples_dir = args.samples.resolve()
    warning = samples_warning(samples_dir)
    if warning:
        print(warning)
    labels_csv = samples_dir / "labels.csv"
    if not labels_csv.exists():
        print(f"找不到標準答案檔:{labels_csv}")
        print("請先執行 python tools/make_sample.py --einvoice 產生合成樣本與標準答案"
              "(見 docs/使用手冊.md「建立標準答案」一節)。")
        return 1

    cfg = load_config()
    models: list[str | None] = (
        [None] if args.mock else (args.model or [_default_model(cfg, args.provider)])
    )

    results = []
    for model_name in models:
        print(f"\n評測模型:{'mock' if args.mock else model_name}({args.provider})")
        results.append(evaluate_model(cfg, model_name, mock=args.mock, provider=args.provider,
                                      samples_dir=samples_dir))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RESULTS_DIR / f"eval_{time.strftime('%Y%m%d_%H%M%S')}.json"
    raw_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    report = _write_report(results, args.target_precision, samples_dir)
    print(f"\n原始結果:{raw_path}")
    print(f"報告已寫入:{report}")

    best = max(results, key=lambda r: r["整體正確率"])
    rows = sweep_thresholds(best["records"])
    rec = recommend_threshold(rows, args.target_precision)
    if rec:
        print(f"建議 auto_threshold = {rec['threshold']:.2f}"
              f"(自動化比例 {rec['automation_rate']:.1%}、正確率 {rec['auto_precision']:.1%})")
        threshold_note = threshold_warning(rec["threshold"])
        if threshold_note:
            print(f"⚠️ {threshold_note}")
    else:
        print(f"⚠️ 無門檻可達 {args.target_precision:.0%} 正確率,詳見報告的處理方向。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
