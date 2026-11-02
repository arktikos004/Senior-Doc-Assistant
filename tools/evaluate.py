"""評測 CLI:模型比較、三種做法對照(只依自評 / 只解 QR / 模型加分層核對)與信心門檻校準。

用法(專案根目錄執行):
    # 先產生合成樣本與標準答案(data/samples/einvoice/,seed 固定可重現)
    python tools/make_sample.py --einvoice

    # 以標準答案評測單一本地模型(不搬動任何檔案,只讀樣本資料夾)
    python tools/evaluate.py --model gemma4:12b

    # 一次比較多個本地模型
    python tools/evaluate.py --model gemma4:12b --model mistral-small3.1

    # 指定其他合成樣本資料夾(資料夾內要有 labels.jsonl 或 labels.csv)
    python tools/evaluate.py --model gemma4:12b --samples <合成樣本資料夾>

    # 多類型合成評測集:只評 test split、把標準答案的類型當提示(模擬家人上傳時有選類型)
    python tools/evaluate.py --model gemma4:12b --samples <合成評測集資料夾> --split test --hint label

    # 評測 Cloudflare Workers AI 上的模型(需設定 CF_ACCOUNT_ID / CF_API_TOKEN)
    # ⚠ 影像會送到 Cloudflare:樣本資料夾只能放合成樣本;藥袋樣本一律略過(藥袋只在本機推論)
    python tools/evaluate.py --provider workers_ai --model @cf/google/gemma-4-26b-a4b-it

    # 沒有模型時先用 Mock 驗證整條評測流程跑得通(報告寫到 logs/eval/,不蓋掉真實模型的實測結果)
    python tools/evaluate.py --mock

只評測非中資的開源模型。

樣本:預設 data/samples/einvoice/(合成電子發票,有自己的 labels.csv)。資料夾裡可以放 labels.jsonl
(多類型合成評測集)、labels.csv(舊格式)或兩者都放,格式與「正確」的定義見 src/evaluation.py。
不要指到 data/samples/ 根目錄:那裡的 labels.csv 可能是真實資料(個資),工具會印警告。
每個樣本只呼叫模型一次,算出三種做法的決策(定義見 src/evaluation.py 模組說明);
和 Pipeline 一樣先核對再掃門檻,掃的是決策實際比的分數(decision.effective_score)。
沒加 --report 時:真實模型評測預設的合成電子發票,報告寫到 docs/模型實測結果.md(每次整份覆寫);
Mock 或其他樣本資料夾寫到 logs/eval/report_<時間>.md,不會蓋掉公開文件引用的數字。--report 可改路徑;
人寫的分析文件不會被動到。原始逐筆結果寫到 logs/eval/。評測不會搬移或修改樣本檔案,可反覆執行。
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
    DEADLINE_FIELDS,
    HINT_MODES,
    LABELS_CSV,
    LABELS_JSONL,
    MEDICATION_ERRORS,
    METHOD_LABELS,
    METHODS,
    SPLITS,
    UNLABELED,
    breakdown,
    deadline_summary,
    degraded_summary,
    evaluate_file,
    load_dataset,
    medication_summary,
    method_summary,
    recommend_threshold,
    summary_conflict_summary,
    sweep_thresholds,
    threshold_warning,
)

SAMPLES_ROOT = BASE_DIR / "data" / "samples"
DEFAULT_SAMPLES_DIR = SAMPLES_ROOT / "einvoice"   # tools/make_sample.py --einvoice 產生的合成電子發票
DOCS_DIR = BASE_DIR / "docs"
REPORT_PATH = DOCS_DIR / "模型實測結果.md"        # 自動產生、每次覆寫;不動人寫的模型比較報告
RESULTS_DIR = BASE_DIR / "logs" / "eval"

_DECISION_TEXT = {"archive": "接受", "review": "轉人工", "failed": "失敗"}
_HINT_TEXT = {"none": "不給(同上傳時沒選類型)", "label": "標準答案的類型(模擬家人上傳時有選類型)"}
# 欄位錯誤表的欄位順序;沒列的欄位排在後面
_FIELD_ORDER = ("doc_type", "date", "amount", "invoice_number", "fields.due_date", "fields.bill_kind",
                "fields.deadline_text", "fields.deadline", "fields.items")


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
    samples_dir: Path = DEFAULT_SAMPLES_DIR, split: str = "all", hint: str = "none",
) -> dict:
    """對樣本資料夾內所有有標準答案的檔案跑一次「辨識 → 三種做法的決策 → 比對」,回傳評測結果。

    split:dev / test / all(見 src/evaluation.load_dataset);hint:none 不給類型提示(同上傳時沒選類型),
    label 把標準答案的 doc_type 當 doc_type_hint 傳給模型(模擬家人上傳時有選類型)。
    """
    labels = load_dataset(samples_dir, split=split)
    if not labels:
        raise SystemExit(f"標準答案為空:{_display(samples_dir)}(split={split})")

    run_cfg = _with_model(cfg, provider, model_name)
    cloud = provider == "workers_ai" and not mock
    if cloud:
        # 評測沒有類型提示時,經 RoutingAnalyzer 會全部走本機;要量雲端模型就直接用雲端 provider。
        # 注意:這會把樣本影像送到 Cloudflare,只能用合成樣本評測;藥袋在下面略過,不送雲端。
        from src.providers.workers_ai import WorkersAIAnalyzer

        analyzer = WorkersAIAnalyzer(run_cfg)
    else:
        analyzer = create_analyzer(run_cfg, mock=mock)
    label_name = "mock" if mock else (model_name or _default_model(cfg, provider))

    records = []
    for sample_index, truth in enumerate(labels, start=1):
        path = samples_dir / truth.filename
        if not path.exists():
            print(f"  [略過] 找不到樣本 {sample_index:02d}")
            continue
        if cloud and truth.doc_type in run_cfg.local_only_doc_types:
            # 系統本來就不會把藥袋送雲端,量雲端模型讀藥袋沒有意義;合成的也不送(原則 6)
            print(f"  [略過] 樣本 {sample_index:02d} 是{truth.doc_type}:只在本機推論,不送雲端")
            continue

        row = evaluate_file(analyzer, path, truth, cfg=run_cfg,
                            hint=truth.doc_type if hint == "label" else None)  # 先核對再算分數,與 Pipeline 相同
        status = "✓" if row["correct"] else "✗"
        methods = "/".join(_DECISION_TEXT[row["做法"][m]["決策"]] for m in METHODS)
        print(f"  {status} 樣本 {sample_index:02d}(模型自評 {row['confidence']:.2f}、"
              f"比門檻的分數 {row['score']:.2f}、{row['耗時秒']}s;A/B/C:{methods})")
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
        "split": split,
        "類型提示": hint,
        "自動接受門檻": run_cfg.auto_threshold,
        "三種做法": {m: method_summary(records, m) for m in METHODS},
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


# ---- 三種做法對照 ----------------------------------------------------------------------

def _pct(numerator: int, denominator: int) -> str:
    """「45.5%(5/11)」;分母為 0 顯示「—」:沒有接受任何文件不代表錯誤率是 0。"""
    if not denominator:
        return "—"
    return f"{numerator / denominator:.1%}({numerator}/{denominator})"


def _rate_cells(s: dict) -> str:
    """method_summary 的四格:樣本數、自動接受比例、接受結果中的錯誤比例、轉人工比例。"""
    return (f"{s['樣本數']} | {_pct(s['自動接受'], s['樣本數'])} | {_pct(s['接受錯誤'], s['自動接受'])} "
            f"| {_pct(s['轉人工'], s['樣本數'])}")


_RATE_HEADER = "樣本數 | 自動接受比例 | 接受結果中的錯誤比例 | 轉人工比例 |"


def _method_table(records: list[dict]) -> list[str]:
    lines = [f"| 做法 | {_RATE_HEADER}", "|---|---|---|---|---|"]
    lines += [f"| {METHOD_LABELS[m]} | {_rate_cells(method_summary(records, m))} |" for m in METHODS]
    return lines


def _grouped_table(records: list[dict], key: str, title: str) -> list[str]:
    """依 key 分組的三種做法表;全部樣本都沒有標這個鍵時只寫一句說明。"""
    table = breakdown(records, key)
    if list(table) == [UNLABELED]:
        return [f"樣本沒有標註「{title}」,不分組。"]
    lines = [f"| {title} | 做法 | {_RATE_HEADER}", "|---|---|---|---|---|---|"]
    for name, by_method in table.items():
        lines += [f"| {name} | {METHOD_LABELS[m]} | {_rate_cells(by_method[m])} |" for m in METHODS]
    return lines


def _evidence_table(records: list[dict]) -> list[str]:
    lines = [f"| 證據類別 | {_RATE_HEADER}", "|---|---|---|---|---|"]
    for name, by_method in breakdown(records, "證據類別", methods=("C",)).items():
        lines.append(f"| {name} | {_rate_cells(by_method['C'])} |")
    return lines


def _field_error_table(records: list[dict]) -> list[str]:
    summaries = {m: method_summary(records, m) for m in METHODS}
    names = sorted({name for s in summaries.values() for name in s["欄位錯誤"]},
                   key=lambda name: (_FIELD_ORDER.index(name) if name in _FIELD_ORDER else len(_FIELD_ORDER), name))
    if not names:
        return ["三種做法的接受結果都沒有欄位錯誤。"]
    lines = ["| 做法 | 自動接受 | " + " | ".join(f"`{name}`" for name in names) + " |",
             "|---|---|" + "---|" * len(names)]
    for m, s in summaries.items():
        counts = " | ".join(str(s["欄位錯誤"].get(name, 0)) for name in names)
        lines.append(f"| {METHOD_LABELS[m]} | {s['自動接受']} | {counts} |")
    return lines


def _deadline_table(records: list[dict]) -> list[str]:
    summaries = {m: deadline_summary(records, m) for m in METHODS}
    present = [doc_type for doc_type in DEADLINE_FIELDS if summaries["A"][doc_type]["樣本數"]]
    if not present:
        return ["這次的樣本沒有帳單或公文。"]
    lines = ["| 類型 | 做法 | 樣本數 | 自動接受 | 接受結果中的期限錯誤 | 接受結果依證據類別(接受/期限錯) |",
             "|---|---|---|---|---|---|"]
    for doc_type in present:
        for m in METHODS:
            s = summaries[m][doc_type]
            evidence = "、".join(f"{name} {v['自動接受']}/{v['期限錯誤']}" for name, v in s["依證據類別"].items())
            lines.append(f"| {doc_type} | {METHOD_LABELS[m]} | {s['樣本數']} | {s['自動接受']} "
                         f"| {_pct(s['期限錯誤'], s['有期限答案'])} | {evidence if m == 'C' and evidence else '—'} |")
    return lines


_MEDICATION_TEXT = {
    "漏讀": "漏讀(藥袋上有、讀值沒有)",
    "多讀": "多讀(讀值有、藥袋上沒有,即幻覺)",
    "時段不符": "時段不符(配對到的藥)",
    "需要時不符": "「需要時」不符(配對到的藥)",
}


def _medication_lines(records: list[dict]) -> list[str]:
    s = medication_summary(records)
    if not s["樣本數"]:
        return ["這次的樣本沒有藥袋。"]
    bags = [r for r in records if r["doc_type"] == "藥袋"]
    lines = [
        f"藥袋 {s['樣本數']} 件,有模型讀值 {s['有讀值']} 件;A、C 用同一份讀值,B 不讀藥名。"
        "藥名以 NFKC、不分大小寫、去空白與標點後配對。服藥時間表不論決策一律等家人確認,"
        "「自動接受」只代表文件本身歸檔。",
        "",
        "| 項目 | 品項數 | 件數 |",
        "|---|---|---|",
        *(f"| {_MEDICATION_TEXT[kind]} | {s['品項'][kind]['品項']} | {s['品項'][kind]['件數']} |"
          for kind in MEDICATION_ERRORS),
        "",
        "| 做法 | 自動接受 | 接受結果中有漏讀或多讀 |",
        "|---|---|---|",
    ]
    for m in METHODS:
        per = s["做法"].get(m)
        if per is None:
            lines.append(f"| {METHOD_LABELS[m]} | {method_summary(bags, m)['自動接受']} | —(不讀藥名) |")
        else:
            lines.append(f"| {METHOD_LABELS[m]} | {per['自動接受']} | {per['接受結果中有漏讀或多讀']} |")
    return lines


def _degraded_lines(records: list[dict]) -> list[str]:
    if not degraded_summary(records, "A")["樣本數"]:
        return ["這次的樣本沒有降質影像(拍攝狀況都是 clean)。"]
    lines = ["| 做法 | 降質樣本 | 棄答(轉人工) | 錯了卻被接受 | 對且被接受 |", "|---|---|---|---|---|"]
    for m in METHODS:
        s = degraded_summary(records, m)
        lines.append(f"| {METHOD_LABELS[m]} | {s['樣本數']} | {_pct(s['棄答'], s['樣本數'])} "
                     f"| {_pct(s['錯了卻被接受'], s['樣本數'])} | {_pct(s['對且被接受'], s['樣本數'])} |")
    return lines + ["", "依拍攝狀況:", "", *_grouped_table(records, "condition", "拍攝狀況")]


def _summary_conflict_lines(records: list[dict]) -> list[str]:
    lines = ["| 做法 | 有白話解說的讀值 | 解說矛盾 | 自動接受 | 接受結果中解說矛盾 |", "|---|---|---|---|---|"]
    for m in METHODS:
        s = summary_conflict_summary(records, m)
        if s is None:
            accepted = method_summary(records, m)["自動接受"]
            lines.append(f"| {METHOD_LABELS[m]} | — | — | {accepted} | —(沒有白話解說) |")
        else:
            lines.append(f"| {METHOD_LABELS[m]} | {s['有解說']} | {s['解說矛盾']} | {s['自動接受']} "
                         f"| {_pct(s['接受結果中解說矛盾'], s['自動接受'])} |")
    return lines


def _three_way_section(records: list[dict], model: str, threshold: float | None = None) -> str:
    """「三種做法對照(同一測試集)」章節:總表、欄位錯誤、依類型、依證據類別、期限、藥袋、降質、split、版型、解說矛盾。"""
    sources = {r.get("來源") for r in records}
    threshold_text = (f"自動接受門檻 {threshold:.2f}(`config.yaml` 的 `confidence.auto_threshold`)。"
                      if threshold is not None else "")
    lines = [
        "## 三種做法對照(同一測試集)",
        "",
        f"同一批 {len(records)} 個樣本、同一次模型讀值(模型 **{model}**),用三種做法決定「自動接受」或「轉人工」;"
        "定義見 `src/evaluation.py` 模組說明。",
        "",
        "- **A 只依自評**:不核對,`decide()` 只看模型自評信心與必要欄位。",
        "- **B 只解 QR**:不用模型。標準答案標了有 QR 的才解碼;解得開就自動接受(類型當發票,日期與總計額取自 QR),"
        "解不開或沒有 QR 的類型一律轉人工。",
        "- **C 模型加分層核對**:現行流程,`verify_or_flag` 核對後再 `decide()`。",
        "",
        "只比正確率不夠:全部轉人工也能讓錯誤歸零,卻失去服務價值。所以每種做法同時列出自動接受比例、"
        "接受結果中的錯誤比例與轉人工比例(review + failed);括號內是件數,分母為 0 時顯示「—」"
        "(沒有接受任何文件不代表錯誤率是 0)。" + threshold_text,
        "",
        "### 總表",
        "",
        *_method_table(records),
        "",
        "### 接受結果中各欄位錯誤數",
        "",
        "關鍵欄位與輔助欄位(`invoice_number`、`fields.bill_kind`、帳單與藥袋的 `date`)都列;"
        "只有關鍵欄位錯才算整份錯。",
        "",
        *_field_error_table(records),
        "",
        "### 依類型",
        "",
        *_grouped_table(records, "doc_type", "類型"),
        "",
        "### 依證據類別(做法 C)",
        "",
        "「QR 相符」= 電子發票 QR 的獨立檢查通過且涵蓋該類型所有必要欄位(有獨立證據);"
        "「與 QR 不符」= 任一 QR 檢查與讀值矛盾(優先於 QR 相符,分數上限 0.10);"
        "「只有規則」= 其餘有驗證信心者(格式、檢查碼、合理性:只能把關,不能證明讀對,分數上限 0.85);"
        "「沒有驗證信心」= 沒有任何檢查可用,決策退回模型自評。",
        "",
        *_evidence_table(records),
        "",
        "### 期限錯誤(帳單、公文分開)",
        "",
        "帳單看模型讀的繳費期限(程式用 `parse_date` 讀,同 `_plan_bill`);公文看程式由期限原文算出的日期"
        "(`plan_actions`,同 `_plan_official`,以發文日期起算,讀不到發文日期才用評測當天)。"
        "比例的分母是接受結果中標準答案有期限的件數。",
        "",
        *_deadline_table(records),
        "",
        "### 藥袋漏讀/多讀",
        "",
        *_medication_lines(records),
        "",
        "### 降質影像:棄答 vs 錯了卻被接受",
        "",
        "降質 = 拍攝狀況(condition)不是 clean。",
        "",
        *_degraded_lines(records),
        "",
        "### 依 split",
        "",
        *_grouped_table(records, "split", "split"),
        "",
        "### 依版型",
        "",
        "同版型數值變化不算獨立泛化證據:同一版型的樣本版面相同,只差在內容與拍攝狀況。",
        "",
        *_grouped_table(records, "template", "版型"),
        "",
        "### 白話解說矛盾(啟發式)",
        "",
        "用確定性的正則抽出 `plain_summary` 裡的金額(「N 元」「NT$N」)與月日(「M月D日」「M/D」、含年的日期),"
        "和標準答案的金額、日期、期限比,不符記一筆。**只是啟發式**:國字數字與沒寫「元」的金額抽不到,"
        "標準答案沒有的日期(例如計費期間)會被誤記成矛盾。",
        "",
        *_summary_conflict_lines(records),
    ]
    if len(sources) > 1 and LABELS_CSV in sources:
        old = [r for r in records if r.get("來源") == LABELS_CSV]
        lines += ["", f"### 舊 CSV 樣本(`{LABELS_CSV}`)總表", "", *_method_table(old)]
    elif sources == {LABELS_CSV}:
        lines += ["", f"(這次的樣本全部來自舊格式 `{LABELS_CSV}`,上面的總表就是舊 CSV 樣本的三方法總表。)"]
    return "\n".join(lines)


def _label_files(samples_dir: Path) -> str:
    names = [f"`{name}`" for name in (LABELS_JSONL, LABELS_CSV) if (samples_dir / name).exists()]
    return " 與 ".join(names) or f"`{LABELS_CSV}`"


def default_report_path(samples_dir: Path, mock: bool) -> Path:
    """沒給 --report 時的報告路徑。

    只有「真實模型 × 預設的合成電子發票」寫進 docs/模型實測結果.md(公開文件的數字由這個指令重現);
    Mock 或其他樣本資料夾寫到 logs/eval/,多類型評測集試跑時才不會蓋掉它。
    """
    if not mock and samples_dir.resolve() == DEFAULT_SAMPLES_DIR.resolve():
        return REPORT_PATH
    return RESULTS_DIR / f"report_{time.strftime('%Y%m%d_%H%M%S')}.md"


def _write_report(results: list[dict], target: float, samples_dir: Path, out: Path = REPORT_PATH) -> Path:
    split = results[0].get("split", "all")
    hint = results[0].get("類型提示", "none")
    lines = [
        "# 模型實測結果(自動產生)",
        "",
        "由 `python tools/evaluate.py` 自動產生,**每次執行整份覆寫**;"
        "分析、判斷與結論寫在[模型比較報告](模型比較報告.md)第 7 節。",
        f"樣本來源:`{_display(samples_dir)}/`,標準答案:同資料夾的 {_label_files(samples_dir)}(只能用合成樣本);"
        f"split:{split};類型提示:{_HINT_TEXT.get(hint, hint)}。",
        "",
        "「正確」定義:依標準答案的類型,關鍵欄位全數相符(標準答案留空的欄位不比對):",
        "",
        "- 發票、收據:`doc_type`、`date`、`amount`(`invoice_number` 為輔助)",
        "- 帳單:`doc_type`、`amount`、`fields.due_date`(`fields.bill_kind` 為輔助)",
        "- 公文:`doc_type`、`date`、`fields.deadline_text`(NFKC、去空白與標點後比對)、"
        "程式由期限原文算出的期限 `fields.deadline`(同 `_plan_official`)",
        "- 藥袋:`doc_type`、`fields.items`(藥名配對後沒有漏讀、多讀,時段與「需要時」都相同)",
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

    # 三種做法與門檻校準都以正確率最高的模型為準(即實際會部署的那個)
    best_model = max(results, key=lambda r: r["整體正確率"])
    lines.append(_three_way_section(best_model["records"], best_model["模型"], best_model.get("自動接受門檻")))
    lines += ["", ""]
    lines.append(f"以下校準基於正確率最高的模型 **{best_model['模型']}**。")
    lines.append("")
    lines.append(_calibration_section(best_model["records"], target))
    lines += [
        "",
        "## 匿名逐筆明細",
        "",
        f"（模型:{best_model['模型']};A/B/C 為三種做法的決策）",
        "",
        "| 樣本 | 正確 | 模型自評 | 比門檻的分數 | 耗時(秒) | 錯誤欄位 | 類型 | 拍攝狀況 | A/B/C |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for sample_index, r in enumerate(best_model["records"], start=1):
        wrong = [k for k, ok in r["fields"].items() if not ok] or (["辨識失敗"] if r["錯誤"] else [])
        methods = "/".join(_DECISION_TEXT[r["做法"][m]["決策"]] for m in METHODS)
        lines.append(
            f"| 樣本 {sample_index:02d} | {'✓' if r['correct'] else '✗'} | {r['confidence']:.2f} "
            f"| {r['score']:.2f} | {r['耗時秒']} | {'、'.join(wrong) or '—'} "
            f"| {r.get('doc_type') or '—'} | {r.get('condition') or '—'} | {methods} |"
        )
    lines.append("")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def _print_three_way(result: dict) -> None:
    records = result["records"]
    print(f"三種做法(模型 {result['模型']},同一測試集 {len(records)} 筆):")
    for m in METHODS:
        s = method_summary(records, m)
        print(f"  {METHOD_LABELS[m]}:自動接受 {_pct(s['自動接受'], s['樣本數'])}、"
              f"接受結果中的錯誤 {_pct(s['接受錯誤'], s['自動接受'])}、轉人工 {_pct(s['轉人工'], s['樣本數'])}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="模型比較、三種做法對照與信心門檻校準")
    parser.add_argument("--provider", choices=["ollama", "workers_ai"], default="ollama",
                        help="推論來源:本地 Ollama 或 Cloudflare Workers AI(預設 ollama)")
    parser.add_argument("--model", action="append", default=None,
                        help="要評測的模型名稱,可重複指定以比較多個模型")
    parser.add_argument("--mock", action="store_true", help="使用 Mock 辨識器驗證評測流程(免 Ollama)")
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES_DIR,
                        help="樣本資料夾,內含 labels.jsonl 或 labels.csv(預設 data/samples/einvoice/,合成電子發票)")
    parser.add_argument("--split", choices=SPLITS, default="all",
                        help="只評測某個 split(labels.jsonl 的 dev/test;舊 CSV 樣本沒有 split,只在 all 出現)")
    parser.add_argument("--hint", choices=HINT_MODES, default="none",
                        help="none:不給類型提示(同上傳時沒選類型,預設);label:把標準答案的類型當提示")
    parser.add_argument("--report", type=Path, default=None,
                        help="報告輸出路徑(預設:真實模型評測預設樣本時寫 docs/模型實測結果.md,"
                             "其他情況寫 logs/eval/report_<時間>.md)")
    parser.add_argument("--target-precision", type=float, default=0.90,
                        help="自動歸檔正確率目標(預設 0.90)")
    args = parser.parse_args(argv)

    samples_dir = args.samples.resolve()
    warning = samples_warning(samples_dir)
    if warning:
        print(warning)
    if not any((samples_dir / name).exists() for name in (LABELS_JSONL, LABELS_CSV)):
        print(f"找不到標準答案檔:{samples_dir / LABELS_JSONL} 或 {samples_dir / LABELS_CSV}")
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
                                      samples_dir=samples_dir, split=args.split, hint=args.hint))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RESULTS_DIR / f"eval_{time.strftime('%Y%m%d_%H%M%S')}.json"
    raw_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    out = args.report or default_report_path(samples_dir, args.mock)
    report = _write_report(results, args.target_precision, samples_dir, out=out)
    print(f"\n原始結果:{raw_path}")
    print(f"報告已寫入:{report}")

    best = max(results, key=lambda r: r["整體正確率"])
    _print_three_way(best)
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
