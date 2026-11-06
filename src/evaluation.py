"""評測核心:標準答案比對、三種做法對照與信心門檻校準(供 tools/evaluate.py 使用)。

標準答案放在樣本資料夾裡,兩種格式可以並存(load_dataset 兩個都讀,同一個檔名以 labels.jsonl 為準):
- labels.jsonl(UTF-8,每行一個 JSON;多類型合成評測集的格式):
    {"file": "test/bill_telecom_03_glare.png", "split": "dev"|"test", "template": "bill_telecom", "variant": 3,
     "condition": "clean"|"blur"|"tilt"|"glare"|"crop"|"dark"|"qr_damaged",
     "doc_type": "發票"|"收據"|"帳單"|"公文"|"藥袋", "vendor": str|null, "date": "YYYY-MM-DD"|null,
     "amount": number|null, "invoice_number": str|null,
     "fields": {帳單:due_date、bill_kind;公文:subject、doc_number、deadline_text、deadline(以發文日起算)、
                required_actions;藥袋:items [{"name", "timing": ["早","晚"], "prn"}]},
     "qr": {"present": bool, "readable": bool}}
  file 是相對於樣本資料夾的路徑,不能指到樣本資料夾外面(評測只讀合成樣本)。
- labels.csv(舊格式,合成電子發票 data/samples/einvoice/;欄位:檔名,doc_type,date,vendor,amount,invoice_number,
  另讀 degrade 當 condition,其他欄位不讀):doc_type 空白視為發票、一律當成有 QR(qr.present = true)、沒有 split。

「正確」= 標準答案那一類的關鍵欄位全對(標準答案留空的欄位不比對;KEY_FIELDS):
- 發票/收據:doc_type、date、amount(現行定義,不變;其他類型也沿用);invoice_number 為輔助。
- 帳單:doc_type、amount、fields.due_date(用 dates.parse_date 讀模型的值,和 _plan_bill 同一個讀法);
  bill_kind 為輔助。
- 公文:doc_type、date、fields.deadline_text(NFKC、去空白與標點,再去掉開頭的「請」「應」「並」「於」後比對)、
  期限 fields.deadline——
  由 program_deadline 對結果的深拷貝跑 plan_actions 算出,也就是 _plan_official 本身、同一個基準日
  (發文日期,讀不到才用上傳日);模型自己填的 deadline 不採信。
- 藥袋:doc_type、fields.items——以正規化藥名配對(NFKC、不分大小寫、去空白與標點);漏讀(標準答案有、
  輸出沒有)、多讀/幻覺(輸出有、標準答案沒有)、配對到的藥 timing 集合或 prn 不同,任一項就不算對。
vendor 只當參考(商家全名與簡稱難以嚴格比對,採雙向包含即算對),不納入正確。

三種做法(同一測試集;每筆都算三次決策,模型只呼叫一次;archive = 自動接受,review + failed = 轉人工):
- A `只依自評`:對結果的深拷貝把 verified_confidence 設 None、verification 清空,再跑 decide()
  (effective_score 會退回模型自評)。
- B `只解 QR`:不用模型。label qr.present 為真的才嘗試 load_image → decode_qr_texts → find_left_qr;
  解得開 → 自動接受,以 QR 的日期與總計額比對標準答案(類型視為發票);解不開或沒有 QR 的類型 → 轉人工。
- C `模型加分層核對`:現行流程 verify_or_flag(result, path) + decide(),archive = 自動接受。

三個指標一起報(method_summary;每種做法,整體與各分組都有):
- 關鍵欄位讀對率 = 關鍵欄位全對 ÷ 樣本數,不管決策;辨識失敗算錯,B 只有解得開 QR 的才有讀值。
- 錯誤自動接受率 = 自動接受卻有關鍵欄位錯 ÷ 自動接受(「接受錯誤比例」)。
- 正確且免更正完成率 = 自動接受(不用人看)而且關鍵欄位全對 ÷ 樣本數;轉人工與辨識失敗都算沒完成,
  所以全部轉人工時錯誤歸零、這個比例也歸零。藥袋的自動接受只代表文件歸檔,服藥時間表一律等家人確認(只是不用更正)。
- 帳單期限、公文期限各自獨立一列,同樣三個指標只看期限那一欄(deadline_metrics),不能用整體平均掩蓋。

其他指標(每種做法;整體,以及依 doc_type、split、template、condition 分組;C 另依證據類別分組):
- 自動接受比例、接受結果中的錯誤比例、轉人工比例(review + failed)、接受結果中各欄位錯誤數(method_summary)。
  只比正確率不夠:全部轉人工也能讓錯誤歸零,卻失去服務價值,所以三個比例一起看。
- 期限錯誤:接受結果中帳單 due_date、公文期限錯的件數與比例,帳單、公文分開(deadline_summary)。
- 降質影像(condition 不是 clean):棄答(轉人工)vs 錯了卻被接受(degraded_summary)。
- 藥袋:漏讀、多讀(幻覺)的品項數與件數(medication_summary)。
- 白話解說矛盾:用確定性的正則抽出 plain_summary 裡的金額與月日,和標準答案的金額/日期/期限比,
  不符記一筆(summary_conflicts)。只是啟發式:國字數字抽不到,標準答案沒有的日期(如計費期間)會被誤記。
- 證據類別(evidence_class,做法 C):「QR 相符」= INDEPENDENT_CHECKS 有 pass 且涵蓋該類型所有必要欄位;
  「與 QR／條碼不符」= 任一 INDEPENDENT_CHECKS(電子發票 QR、帳單條碼的應繳金額)為 fail(與獨立證據矛盾,
  優先於 QR 相符);其餘有驗證信心者為
  「只有規則」;沒有驗證信心(退回模型自評)、核對出錯、辨識失敗另列。

門檻校準:對「決策實際比的分數(decision.effective_score:有驗證信心用它,否則用模型自評)
vs 實際正確」掃描門檻,找出滿足「自動歸檔正確率 ≥ 目標(預設 0.90)」下自動化比例最高的門檻。
每個樣本和 Pipeline 一樣先核對(verify_or_flag)再看分數(evaluate_file)。
"""
from __future__ import annotations

import copy
import csv
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .actions import plan_actions
from .actions.medication import normalize_items
from .config import AppConfig
from .dates import parse_date
from .decision import decide, effective_score
from .models import DOC_TYPES, REQUIRED_FIELDS, Decision, ExtractionResult
from .verify import (
    INDEPENDENT_CHECKS,
    RULE_FLOOR,
    RULE_SPAN,
    VERIFY_ERROR_SUMMARY,
    einvoice,
    verify_or_flag,
)

_NON_ALNUM = re.compile(r"[^0-9A-Za-z一-鿿]+")

# 只有規則核對(沒有 QR 之類的獨立證據)時,驗證信心最高只到 0.60 + 0.25 = 0.85(見 src/verify)
RULE_ONLY_MAX_SCORE = RULE_FLOOR + RULE_SPAN

LABELS_JSONL = "labels.jsonl"
LABELS_CSV = "labels.csv"
SPLITS: tuple[str, ...] = ("dev", "test", "all")
HINT_MODES: tuple[str, ...] = ("none", "label")   # none:不給類型提示;label:標準答案的 doc_type 當提示
CLEAN = "clean"          # 拍攝狀況:沒有降質
UNLABELED = "(未標註)"   # 分組時沒有標 split、版型的樣本

# 三種做法(定義見模組說明);決策 archive = 自動接受,review + failed = 轉人工
METHODS: tuple[str, ...] = ("A", "B", "C")
METHOD_LABELS: dict[str, str] = {"A": "A 只依自評", "B": "B 只解 QR", "C": "C 模型加分層核對"}
MODEL_METHODS: tuple[str, ...] = ("A", "C")   # 用到模型讀值的做法(B 不用模型)
ACCEPTED = "archive"
HANDED_OFF: tuple[str, ...] = ("review", "failed")

# 做法 C 的證據類別(evidence_class)
EVIDENCE_QR = "QR 相符"
EVIDENCE_QR_CONFLICT = "與 QR／條碼不符"
EVIDENCE_RULES = "只有規則"
EVIDENCE_SELF = "沒有驗證信心"
EVIDENCE_VERIFY_ERROR = "核對出錯"
EVIDENCE_FAILED = "辨識失敗"
EVIDENCE_CLASSES: tuple[str, ...] = (EVIDENCE_QR, EVIDENCE_QR_CONFLICT, EVIDENCE_RULES, EVIDENCE_SELF,
                                     EVIDENCE_VERIFY_ERROR, EVIDENCE_FAILED)

# 各類型的關鍵欄位(全對才算正確);沒列的類型沿用發票/收據的定義
KEY_FIELDS: dict[str, tuple[str, ...]] = {
    "發票": ("doc_type", "date", "amount"),
    "收據": ("doc_type", "date", "amount"),
    "帳單": ("doc_type", "amount", "fields.due_date"),
    "公文": ("doc_type", "date", "fields.deadline_text", "fields.deadline"),
    "藥袋": ("doc_type", "fields.items"),
}
DEFAULT_KEY_FIELDS: tuple[str, ...] = ("doc_type", "date", "amount")
# 期限欄位:帳單是模型讀的繳費期限,公文是程式由期限原文算出的日期(兩類分開算)
DEADLINE_FIELDS: dict[str, str] = {"帳單": "fields.due_date", "公文": "fields.deadline"}


@dataclass
class GroundTruth:
    filename: str
    doc_type: str
    date: str | None = None
    vendor: str | None = None
    amount: float | None = None
    invoice_number: str | None = None
    # 以下是 labels.jsonl 才有的資訊;舊 labels.csv 的樣本由 load_labels 補上固定值
    fields: dict[str, Any] = field(default_factory=dict)   # 類型專屬欄位的標準答案
    split: str = ""                    # dev / test;舊 CSV 樣本沒有
    template: str = ""                 # 版型名稱
    variant: int | None = None
    condition: str = CLEAN             # 拍攝狀況;不是 clean 的都算降質影像
    qr_present: bool = False           # 文件上印有電子發票 QR:做法 B 只對這些嘗試解碼
    qr_readable: bool | None = None    # 標準答案標註 QR 讀不讀得到;舊 CSV 沒有標 → None
    source: str = ""                   # 標準答案來自 labels.jsonl 或 labels.csv


# ---- 讀標準答案 --------------------------------------------------------------------

def load_labels(csv_path: Path) -> dict[str, GroundTruth]:
    """讀取舊格式的標準答案 CSV,回傳 檔名 → GroundTruth。空欄位視為「不比對」。

    舊 CSV 是合成電子發票:doc_type 空白視為發票、一律當成有 QR(做法 B 會嘗試解碼)、沒有 split;
    degrade 欄(模糊、旋轉、褪色、壓縮)當 condition,空白為 clean。
    """
    labels: dict[str, GroundTruth] = {}
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("檔名") or "").strip()
            if not name:
                continue
            amount_raw = (row.get("amount") or "").strip()
            labels[name] = GroundTruth(
                filename=name,
                doc_type=(row.get("doc_type") or "").strip() or "發票",
                date=(row.get("date") or "").strip() or None,
                vendor=(row.get("vendor") or "").strip() or None,
                amount=float(amount_raw) if amount_raw else None,
                invoice_number=(row.get("invoice_number") or "").strip() or None,
                condition=(row.get("degrade") or "").strip() or CLEAN,
                qr_present=True,
                source=LABELS_CSV,
            )
    return labels


def _text(value: Any) -> str | None:
    """標準答案的字串欄位:不是字串或只有空白都當成沒有。"""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _number(value: Any) -> float | None:
    """標準答案的數字欄位:bool 不算數字(True 不是 1 元)。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def load_jsonl_labels(jsonl_path: Path) -> dict[str, GroundTruth]:
    """讀取 labels.jsonl(格式見模組說明),回傳 檔名 → GroundTruth。

    空行略過;不是 JSON 物件、沒有 file 或 doc_type 的行丟 ValueError 並寫出行號:
    標準答案壞掉時寧可停下來,不默默少評幾筆。
    """
    labels: dict[str, GroundTruth] = {}
    with open(jsonl_path, encoding="utf-8-sig") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{jsonl_path.name} 第 {lineno} 行不是合法的 JSON:{exc.msg}") from None
            if not isinstance(row, dict) or not _text(row.get("file")) or not _text(row.get("doc_type")):
                raise ValueError(f"{jsonl_path.name} 第 {lineno} 行缺少 file 或 doc_type")
            qr = row.get("qr") if isinstance(row.get("qr"), dict) else {}
            variant = row.get("variant")
            name = _text(row["file"])
            labels[name] = GroundTruth(
                filename=name,
                doc_type=_text(row["doc_type"]),
                date=_text(row.get("date")),
                vendor=_text(row.get("vendor")),
                amount=_number(row.get("amount")),
                invoice_number=_text(row.get("invoice_number")),
                fields=copy.deepcopy(row["fields"]) if isinstance(row.get("fields"), dict) else {},
                split=_text(row.get("split")) or "",
                template=_text(row.get("template")) or "",
                variant=variant if isinstance(variant, int) and not isinstance(variant, bool) else None,
                condition=_text(row.get("condition")) or CLEAN,
                qr_present=qr.get("present") is True,
                qr_readable=qr["readable"] if isinstance(qr.get("readable"), bool) else None,
                source=LABELS_JSONL,
            )
    return labels


def load_dataset(samples_dir: Path, split: str = "all") -> list[GroundTruth]:
    """讀樣本資料夾的標準答案:labels.jsonl 與 labels.csv 都讀(同一個檔名以 jsonl 為準),再依 split 過濾。

    split:dev / test 只留該 split 的樣本(舊 CSV 樣本沒有 split,只在 all 出現);all 全留。
    兩個檔都沒有就回空清單。檔名指到樣本資料夾外面(例如 ../)丟 ValueError:評測只讀資料夾裡的合成樣本。
    """
    if split not in SPLITS:
        raise ValueError(f"split 只能是 {'、'.join(SPLITS)},收到 {split!r}")
    merged: dict[str, GroundTruth] = {}
    if (samples_dir / LABELS_JSONL).exists():
        merged.update(load_jsonl_labels(samples_dir / LABELS_JSONL))
    if (samples_dir / LABELS_CSV).exists():
        for name, truth in load_labels(samples_dir / LABELS_CSV).items():
            merged.setdefault(name, truth)
    root = samples_dir.resolve()
    for name in merged:
        if not (samples_dir / name).resolve().is_relative_to(root):
            raise ValueError(f"標準答案的檔名「{name}」指到樣本資料夾外面;評測只讀樣本資料夾裡的合成樣本")
    return [t for t in merged.values() if split == "all" or t.split == split]


# ---- 逐欄位比對 ----------------------------------------------------------------------

def _norm(text: Any) -> str:
    return _NON_ALNUM.sub("", str(text or ""))


def _loose_text(value: Any) -> str:
    """NFKC 後去掉空白與標點:「收到本函後 15 日內。」與「收到本函後15日內」視為相同。"""
    text = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(ch for ch in text if not ch.isspace() and not unicodedata.category(ch).startswith("P"))


_DEADLINE_LEAD = re.compile(r"^[請應並]?於?")


def _deadline_phrase(value: Any) -> str:
    """公文期限原文的比對鍵:_loose_text 後再去掉開頭的「請」「應」「並」「於」。

    這幾個字是語氣與介詞,「請於115年10月31日前」與「於115年10月31日前」算出的期限相同;
    期限本身另由 fields.deadline(程式算出的日期)比,這裡只確認照抄的是同一句期限。
    """
    return _DEADLINE_LEAD.sub("", _loose_text(value))


def _drug_key(name: Any) -> str:
    """藥名配對用的鍵:NFKC、不分大小寫、只留英數與漢字(「範例錠A 500MG」=「範例錠a 500 mg」)。"""
    return _norm(unicodedata.normalize("NFKC", str(name or "")).casefold())


def program_due_date(result: ExtractionResult) -> str | None:
    """帳單繳費期限:用 parse_date 讀模型的 due_date(和 _plan_bill 同一個讀法,民國年也換得出來)。

    讀值不是帳單就不會有繳費提醒,回 None;讀不出日期也回 None。
    """
    if result.doc_type != "帳單" or not isinstance(result.fields, dict):
        return None
    due = parse_date(str(result.fields.get("due_date") or ""))
    return due.isoformat() if due else None


def program_deadline(result: ExtractionResult, received_on: date, cfg: AppConfig | None = None) -> str | None:
    """公文期限:對結果的深拷貝跑 plan_actions(公文就是 _plan_official),取它寫進 fields["deadline"] 的值。

    和系統實際產生提醒是同一個函式、同一個基準日:發文日期,讀不到才用 received_on(上傳日);
    模型自己填的 deadline 不採信(_plan_official 會先移除)。讀值不是公文就不會算這個期限,回 None。
    """
    if result.doc_type != "公文":
        return None
    probe = copy.deepcopy(result)
    plan_actions(probe, Decision(action="review", reason="評測"), cfg or AppConfig(), received_on=received_on)
    deadline = probe.fields.get("deadline") if isinstance(probe.fields, dict) else None
    return deadline if isinstance(deadline, str) else None


def compare_items(output: Any, truth: Any) -> dict[str, Any]:
    """藥袋品項比對:兩邊先用服藥時間表同一套清洗(normalize_items:時段別名、需要時),再以正規化藥名配對。

    同名的藥依序配對。回傳 ok(全部一致)與各項藥名清單:漏讀(標準答案有、輸出沒有)、
    多讀(輸出有、標準答案沒有,幻覺)、時段不符與需要時不符(配對到的藥)。
    """
    pool: dict[str, list[dict[str, Any]]] = {}
    for item in normalize_items(output):
        pool.setdefault(_drug_key(item["name"]), []).append(item)
    missing: list[str] = []
    timing_diff: list[str] = []
    prn_diff: list[str] = []
    for want in normalize_items(truth):
        bucket = pool.get(_drug_key(want["name"]))
        if not bucket:
            missing.append(want["name"])
            continue
        got = bucket.pop(0)
        if set(got["timing"]) != set(want["timing"]):
            timing_diff.append(want["name"])
        if got["prn"] != want["prn"]:
            prn_diff.append(want["name"])
    extra = [item["name"] for bucket in pool.values() for item in bucket]
    return {
        "ok": not (missing or extra or timing_diff or prn_diff),
        "漏讀": missing, "多讀": extra, "時段不符": timing_diff, "需要時不符": prn_diff,
    }


def compare(result: ExtractionResult, truth: GroundTruth, *, cfg: AppConfig | None = None,
            received_on: date | None = None) -> dict[str, Any]:
    """逐欄位比對辨識結果與標準答案(依標準答案的類型取關鍵欄位,見模組說明)。標準答案留空的欄位不納入評分。

    fields 含關鍵欄位與輔助欄位(invoice_number、bill_kind、帳單與藥袋的 date),correct 只看關鍵欄位。
    received_on:公文讀不到發文日期時的起算日(同 Pipeline 的上傳日),沒給就是今天。
    另外回傳 藥袋(compare_items 的明細)與 期限(標準答案 vs 程式讀出或算出的日期),沒有就是 None。
    """
    got = result.fields if isinstance(result.fields, dict) else {}
    want = truth.fields or {}
    fields: dict[str, bool] = {"doc_type": result.doc_type == truth.doc_type}
    if truth.date:
        fields["date"] = (result.date or "") == truth.date
    if truth.amount is not None:
        fields["amount"] = result.amount is not None and abs(result.amount - truth.amount) < 0.5
    if truth.invoice_number:
        fields["invoice_number"] = _norm(result.invoice_number) == _norm(truth.invoice_number)

    medication: dict[str, Any] | None = None
    deadline: dict[str, Any] | None = None
    if truth.doc_type == "帳單":
        due = _text(want.get("due_date"))
        if due:
            read = program_due_date(result)
            fields["fields.due_date"] = read == due
            deadline = {"標準答案": due, "讀值": read}
        if _text(want.get("bill_kind")):
            fields["fields.bill_kind"] = _norm(got.get("bill_kind")) == _norm(want["bill_kind"])
    elif truth.doc_type == "公文":
        wanted_text = _text(want.get("deadline_text"))
        if wanted_text:
            fields["fields.deadline_text"] = _deadline_phrase(got.get("deadline_text")) == _deadline_phrase(wanted_text)
        due = _text(want.get("deadline"))
        if due:
            computed = program_deadline(result, received_on or date.today(), cfg)
            fields["fields.deadline"] = computed == due
            deadline = {"標準答案": due, "讀值": computed}
    elif truth.doc_type == "藥袋" and isinstance(want.get("items"), list) and want["items"]:
        medication = compare_items(got.get("items"), want["items"])
        fields["fields.items"] = medication["ok"]

    # vendor 採寬鬆雙向包含(全聯實業股份有限公司 vs 全聯實業),僅供參考不納入整體
    vendor_ok: bool | None = None
    if truth.vendor:
        a, b = _norm(result.vendor), _norm(truth.vendor)
        vendor_ok = bool(a) and (a in b or b in a)

    key = KEY_FIELDS.get(truth.doc_type, DEFAULT_KEY_FIELDS)
    return {
        "fields": fields,
        "vendor_ok": vendor_ok,
        "correct": all(ok for name, ok in fields.items() if name in key),
        "confidence": result.confidence,        # 模型自評,只當參考
        "score": effective_score(result),       # 決策實際比門檻的分數
        "藥袋": medication,
        "期限": deadline,
    }


# ---- 白話解說矛盾(啟發式)、證據類別 ------------------------------------------------------

# 金額:「1,854 元」「1854元」「NT$1,854」;國字數字(一千八百元)抽不到
_AMOUNT_YUAN = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*元")
_AMOUNT_DOLLAR = re.compile(r"(?:NT\$|\$)\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?![\d,])")
# 月日:「10月15日」「10/15」、含年的「2026-10-15」「115/10/15」;「1/2 顆」這類劑量不算
_MONTH_DAY = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日號]")
_FULL_DATE = re.compile(r"(?<![\d/.\-])\d{2,4}([/.\-])(\d{1,2})\1(\d{1,2})(?![\d/.\-])")
_SLASH_DATE = re.compile(r"(?<![\d/.])(\d{1,2})/(\d{1,2})(?![\d/])(?!\s*(?:顆|錠|粒|包|匙|片|mL|ml|cc))")


def _money(value: float) -> str:
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def _summary_month_days(text: str) -> list[tuple[int, int]]:
    found = [(int(m.group(1)), int(m.group(2))) for m in _MONTH_DAY.finditer(text)]
    found += [(int(m.group(2)), int(m.group(3))) for m in _FULL_DATE.finditer(text)]
    found += [(int(m.group(1)), int(m.group(2))) for m in _SLASH_DATE.finditer(text)]
    return [(month, day) for month, day in found if 1 <= month <= 12 and 1 <= day <= 31]


def summary_conflicts(summary: str | None, truth: GroundTruth) -> list[str]:
    """白話解說矛盾(啟發式):抽出 plain_summary 裡的金額與月日,和標準答案的金額、日期與期限比,不符各記一筆。

    金額認「N 元」「NT$N」;月日認「M月D日」「M/D」與含年的日期(只比月日)。標準答案沒有金額就不比金額、
    沒有任何日期就不比日期。只是啟發式:國字數字、沒寫「元」的金額抽不到,標準答案沒有的日期
    (例如計費期間)會被誤記成矛盾,報告要註明。
    """
    text = unicodedata.normalize("NFKC", summary or "")
    conflicts: list[str] = []
    if truth.amount is not None:
        for m in [*_AMOUNT_YUAN.finditer(text), *_AMOUNT_DOLLAR.finditer(text)]:
            value = float(m.group(1).replace(",", "") + (m.group(2) or ""))
            if abs(value - truth.amount) >= 0.5:
                conflicts.append(f"金額 {_money(value)} 元(標準答案 {_money(truth.amount)} 元)")
    want = truth.fields or {}
    truth_dates = [parse_date(v) for v in (truth.date, want.get("due_date"), want.get("deadline"))
                   if isinstance(v, str)]
    known = {(d.month, d.day) for d in truth_dates if d}
    if known:
        for month, day in _summary_month_days(text):
            if (month, day) not in known:
                conflicts.append(f"日期 {month} 月 {day} 日(不是標準答案的日期或期限)")
    return list(dict.fromkeys(conflicts))


def evidence_class(result: ExtractionResult | None) -> str:
    """做法 C 的證據類別(核對之後呼叫):讀值有沒有獨立證據撐腰。

    QR 相符:INDEPENDENT_CHECKS 有 pass 且涵蓋該類型所有必要欄位(同 score_verification 給 QR 基礎分的條件);
    與 QR／條碼不符:任一 INDEPENDENT_CHECKS 為 fail——與獨立證據矛盾,優先於 QR 相符(分數上限 0.10);
    只有規則:其餘有驗證信心者(格式、檢查碼、合理性,只能把關、不能證明讀對);
    沒有驗證信心(決策退回模型自評)、核對出錯(驗證信心 0)、辨識失敗另列。
    """
    if result is None:
        return EVIDENCE_FAILED
    if result.verified_confidence is None:
        return EVIDENCE_SELF
    verification = result.verification if isinstance(result.verification, dict) else {}
    if verification.get("_summary") == VERIFY_ERROR_SUMMARY:
        return EVIDENCE_VERIFY_ERROR
    independent = [chk for name, chk in verification.items()
                   if name in INDEPENDENT_CHECKS and isinstance(chk, dict)]
    if any(chk.get("status") == "fail" for chk in independent):
        return EVIDENCE_QR_CONFLICT
    qr_passed = {f for chk in independent if chk.get("status") == "pass" for f in chk.get("fields", [])}
    required = REQUIRED_FIELDS.get(result.doc_type, ())
    if required and set(required) <= qr_passed:
        return EVIDENCE_QR
    return EVIDENCE_RULES


# ---- 三種做法 ------------------------------------------------------------------------

def _outcome(action: str, reason: str, correct: bool | None = None,
             fields: dict[str, bool] | None = None) -> dict[str, Any]:
    """一種做法對一個樣本的結果;correct 為 None 表示沒有讀值可比(B 轉人工、辨識失敗)。"""
    return {"決策": action, "理由": reason, "correct": correct, "fields": dict(fields or {})}


def qr_only(path: Path, truth: GroundTruth) -> dict[str, Any]:
    """做法 B「只解 QR」:不用模型。標準答案 qr.present 為真的才嘗試 load_image → decode_qr_texts → find_left_qr。

    解得開 → 自動接受,以 QR 的日期與總計額和標準答案比對(類型視為發票);解不開或沒有 QR 的類型 → 轉人工。
    QR 總計額記 0(境外電商)時當成沒有金額;解碼器出錯也只是解不開。
    """
    if not truth.qr_present:
        return _outcome("review", "文件沒有電子發票 QR,只解 QR 無從判讀")
    try:
        image = einvoice.load_image(path)
        qr = einvoice.find_left_qr(einvoice.decode_qr_texts(image)) if image is not None else None
    except Exception:
        qr = None
    if qr is None:
        return _outcome("review", "解不開電子發票左側 QR")
    read = ExtractionResult(doc_type="發票", date=qr.date, amount=float(qr.total_amount) or None,
                            invoice_number=qr.invoice_number)
    row = compare(read, truth)
    return _outcome(ACCEPTED, "解得開 QR,直接採用 QR 的日期與總計額", row["correct"], row["fields"])


def _unread_deadline(truth: GroundTruth) -> dict[str, Any] | None:
    """辨識失敗時的「期限」:沒有讀值,但標準答案有期限就留著,文件才不會從期限那一列的分母消失。"""
    name = DEADLINE_FIELDS.get(truth.doc_type, "").removeprefix("fields.")
    due = _text((truth.fields or {}).get(name))
    return {"標準答案": due, "讀值": None} if due else None


def evaluate_file(analyzer, path: Path, truth: GroundTruth, *, cfg: AppConfig | None = None,
                  hint: str | None = None, received_on: date | None = None) -> dict[str, Any]:
    """對一個樣本跑「辨識 → 三種做法的決策 → 與標準答案比對」,回傳逐筆紀錄。

    模型只呼叫一次:A 用讀值的深拷貝(清掉核對)跑 decide,C 先 verify_or_flag 再 decide,B 不用模型只解 QR;
    結果放在 做法[A/B/C] = {決策, 理由, correct, fields}。上層的 correct、fields、score 是模型讀值
    (A、C 共用)的比對結果:和 Pipeline 一樣先核對(讀原檔)再看分數,score 才會是決策實際比的那個值;
    核對程式出錯時也和 Pipeline 一樣把分數設成 0(轉人工),不退回模型自評。
    hint:傳給 analyzer 的類型提示(--hint label 時是標準答案的 doc_type);None 表示不給,同上傳時沒選類型。
    cfg:決策用的設定(門檻、目標類型),沒給用預設值;received_on:公文讀不到發文日期時的起算日,預設今天。
    辨識失敗(逾時、影像解不開、JSON 解析失敗)記成錯誤列:不算正確、score 0,A、C 都是 failed(轉人工);
    B 不用模型,照樣解 QR。耗時秒只計模型辨識。doc_type、split、template、condition 取自標準答案。
    """
    cfg = cfg or AppConfig()
    label = {"檔名": truth.filename, "doc_type": truth.doc_type, "split": truth.split, "template": truth.template,
             "variant": truth.variant, "condition": truth.condition, "來源": truth.source, "類型提示": hint,
             "QR": {"present": truth.qr_present, "readable": truth.qr_readable}}
    by_qr = qr_only(path, truth)
    t0 = time.perf_counter()
    try:
        result = analyzer.analyze(path) if hint is None else analyzer.analyze(path, doc_type_hint=hint)
    except Exception as exc:
        failed = decide(None, cfg)
        no_read = _outcome(failed.action, failed.reason)
        return {
            **label, "耗時秒": round(time.perf_counter() - t0, 2),
            "correct": False, "confidence": 0.0, "score": 0.0, "fields": {}, "vendor_ok": None,
            "藥袋": None, "期限": _unread_deadline(truth), "辨識結果": None, "錯誤": f"{type(exc).__name__}: {exc}",
            "做法": {"A": no_read, "B": by_qr, "C": dict(no_read)},
            "證據類別": EVIDENCE_FAILED, "白話解說矛盾": [],
        }
    elapsed = time.perf_counter() - t0

    self_only = copy.deepcopy(result)        # A:不核對,effective_score 退回模型自評
    self_only.verified_confidence = None
    self_only.verification = {}
    by_self = decide(self_only, cfg)
    verify_or_flag(result, path)             # C:現行流程
    layered = decide(result, cfg)

    row = compare(result, truth, cfg=cfg, received_on=received_on)
    row.update({
        **label, "耗時秒": round(elapsed, 2), "辨識結果": result.to_dict(), "錯誤": None,
        "做法": {
            "A": _outcome(by_self.action, by_self.reason, row["correct"], row["fields"]),
            "B": by_qr,
            "C": _outcome(layered.action, layered.reason, row["correct"], row["fields"]),
        },
        "證據類別": evidence_class(result),
        "白話解說矛盾": summary_conflicts(result.plain_summary, truth),
    })
    return row


# ---- 指標 ----------------------------------------------------------------------------

def _ratio(numerator: int, denominator: int) -> float | None:
    """比例;分母為 0 時是 None(報告顯示「—」):沒有接受任何文件不代表錯誤率是 0。"""
    return numerator / denominator if denominator else None


def _three_metrics(decided: list[tuple[str, bool]]) -> dict[str, Any]:
    """三個指標的算法(定義見模組說明);每筆是 (決策, 讀值對不對)。

    同一份文件只會是「正確且免更正完成」「錯誤自動接受」「轉人工」三者之一,分母為 0 時比例是 None。
    """
    n = len(decided)
    right = sum(1 for _, ok in decided if ok)
    accepted = [ok for action, ok in decided if action == ACCEPTED]
    wrong = accepted.count(False)
    completed = accepted.count(True)
    return {
        "樣本數": n, "關鍵欄位讀對": right, "關鍵欄位讀對率": _ratio(right, n),
        "自動接受": len(accepted), "接受錯誤": wrong, "接受錯誤比例": _ratio(wrong, len(accepted)),
        "正確且免更正完成": completed, "正確且免更正完成率": _ratio(completed, n),
    }


def method_summary(records: list[dict[str, Any]], method: str) -> dict[str, Any]:
    """一種做法在一組樣本上的指標:三個指標(_three_metrics)、自動接受比例、轉人工比例(review + failed),
    以及接受結果中各欄位錯誤數(欄位名稱 → 件數)。"""
    outcomes = [r["做法"][method] for r in records]
    accepted = [o for o in outcomes if o["決策"] == ACCEPTED]
    handed_off = sum(1 for o in outcomes if o["決策"] in HANDED_OFF)
    field_errors: dict[str, int] = {}
    for o in accepted:
        for name, ok in o["fields"].items():
            if not ok:
                field_errors[name] = field_errors.get(name, 0) + 1
    n = len(outcomes)
    return {
        **_three_metrics([(o["決策"], o["correct"] is True) for o in outcomes]),
        "轉人工": handed_off, "自動接受比例": _ratio(len(accepted), n),
        "轉人工比例": _ratio(handed_off, n), "欄位錯誤": field_errors,
    }


GROUP_ORDERS: dict[str, tuple[str, ...]] = {
    "doc_type": DOC_TYPES, "split": ("dev", "test"), "condition": (CLEAN,), "證據類別": EVIDENCE_CLASSES,
}


def group_records(records: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    """依逐筆紀錄的某個鍵分組(doc_type、split、template、condition、證據類別)。

    GROUP_ORDERS 有列的值照它排前面,其餘依名稱排序;空值歸「(未標註)」排最後。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        groups.setdefault(str(r.get(key) or "") or UNLABELED, []).append(r)
    rank = {name: i for i, name in enumerate(GROUP_ORDERS.get(key, ()))}
    names = sorted(groups, key=lambda name: (name == UNLABELED, rank.get(name, len(rank)), name))
    return {name: groups[name] for name in names}


def breakdown(records: list[dict[str, Any]], key: str,
              methods: tuple[str, ...] = METHODS) -> dict[str, dict[str, dict[str, Any]]]:
    """依 key 分組後,每組每種做法的 method_summary:{組名: {做法: 指標}}。"""
    return {name: {m: method_summary(group, m) for m in methods}
            for name, group in group_records(records, key).items()}


def deadline_summary(records: list[dict[str, Any]], method: str) -> dict[str, dict[str, Any]]:
    """接受結果中的期限錯誤,帳單(繳費期限)與公文(程式算出的期限)分開算。

    每類回傳:樣本數、自動接受、有期限答案(接受結果中標準答案有期限的件數,即分母)、期限錯誤、
    期限錯誤比例,以及依證據類別的 {類別: {"自動接受", "期限錯誤"}}(區分有沒有獨立證據)。
    """
    out: dict[str, dict[str, Any]] = {}
    for doc_type, field_name in DEADLINE_FIELDS.items():
        rows = [r for r in records if r["doc_type"] == doc_type]
        accepted = [r for r in rows if r["做法"][method]["決策"] == ACCEPTED]
        scored = wrong = 0
        by_evidence: dict[str, dict[str, int]] = {}
        for r in accepted:
            fields = r["做法"][method]["fields"]
            is_wrong = field_name in fields and not fields[field_name]
            scored += field_name in fields
            wrong += is_wrong
            entry = by_evidence.setdefault(r.get("證據類別") or EVIDENCE_FAILED, {"自動接受": 0, "期限錯誤": 0})
            entry["自動接受"] += 1
            entry["期限錯誤"] += is_wrong
        out[doc_type] = {"樣本數": len(rows), "自動接受": len(accepted), "有期限答案": scored, "期限錯誤": wrong,
                         "期限錯誤比例": _ratio(wrong, scored), "依證據類別": by_evidence}
    return out


def deadline_metrics(records: list[dict[str, Any]], method: str) -> dict[str, dict[str, Any]]:
    """三個指標只看期限那一欄,帳單、公文各一列:帳單期限不能用整體平均掩蓋。

    樣本是標準答案有期限的文件,辨識失敗的也算(讀不出來就是沒讀對);讀值不是那個類型的,期限算錯。
    金額讀錯、期限讀對的帳單,這一列算對,整份文件的結果看 method_summary。
    """
    out: dict[str, dict[str, Any]] = {}
    for doc_type, field_name in DEADLINE_FIELDS.items():
        rows = [r["做法"][method] for r in records if r["doc_type"] == doc_type and r.get("期限")]
        out[doc_type] = _three_metrics([(o["決策"], o["fields"].get(field_name) is True) for o in rows])
    return out


def degraded_summary(records: list[dict[str, Any]], method: str) -> dict[str, Any]:
    """降質影像(condition 不是 clean)上的棄答(轉人工)vs 錯了卻被接受。"""
    outcomes = [r["做法"][method] for r in records if (r.get("condition") or CLEAN) != CLEAN]
    n = len(outcomes)
    abstained = sum(1 for o in outcomes if o["決策"] in HANDED_OFF)
    wrong = sum(1 for o in outcomes if o["決策"] == ACCEPTED and not o["correct"])
    right = sum(1 for o in outcomes if o["決策"] == ACCEPTED and o["correct"])
    return {"樣本數": n, "棄答": abstained, "錯了卻被接受": wrong, "對且被接受": right,
            "棄答比例": _ratio(abstained, n), "錯了卻被接受比例": _ratio(wrong, n)}


MEDICATION_ERRORS: tuple[str, ...] = ("漏讀", "多讀", "時段不符", "需要時不符")


def medication_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    """藥袋品項的漏讀與多讀(幻覺)。

    品項:A、C 共用同一份模型讀值,統計漏讀、多讀、時段不符、需要時不符的品項數與件數
    (辨識失敗或標準答案沒有品項的不算);做法:A、C 各自接受的藥袋中,有漏讀或多讀的件數。
    B 不讀藥名(藥袋沒有電子發票 QR),不列。服藥時間表不論決策一律等家人確認,自動接受只代表文件歸檔。
    """
    bags = [r for r in records if r["doc_type"] == "藥袋"]
    read = [r["藥袋"] for r in bags if r.get("藥袋")]
    items = {kind: {"品項": sum(len(m[kind]) for m in read), "件數": sum(1 for m in read if m[kind])}
             for kind in MEDICATION_ERRORS}
    per_method = {}
    for method in MODEL_METHODS:
        accepted = [r for r in bags if r["做法"][method]["決策"] == ACCEPTED]
        flawed = sum(1 for r in accepted if r.get("藥袋") and (r["藥袋"]["漏讀"] or r["藥袋"]["多讀"]))
        per_method[method] = {"自動接受": len(accepted), "接受結果中有漏讀或多讀": flawed}
    return {"樣本數": len(bags), "有讀值": len(read), "品項": items, "做法": per_method}


def summary_conflict_summary(records: list[dict[str, Any]], method: str) -> dict[str, Any] | None:
    """白話解說矛盾(啟發式,見 summary_conflicts):模型讀值中解說矛盾的件數,
    與這種做法接受的結果中解說矛盾的件數。B 沒有白話解說,回 None。"""
    if method not in MODEL_METHODS:
        return None
    read = [r for r in records if r.get("辨識結果")]
    accepted = [r for r in records if r["做法"][method]["決策"] == ACCEPTED]
    return {
        "有解說": sum(1 for r in read if str(r["辨識結果"].get("plain_summary") or "").strip()),
        "解說矛盾": sum(1 for r in read if r.get("白話解說矛盾")),
        "自動接受": len(accepted),
        "接受結果中解說矛盾": sum(1 for r in accepted if r.get("白話解說矛盾")),
    }


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
