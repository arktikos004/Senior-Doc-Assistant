"""自我驗證引擎:用獨立於 VLM 的確定性檢查核對辨識結果。

目前的檢查:電子發票左側 QR 解碼比對(唯一的獨立證據)、統編檢查碼、字軌格式、期別/日期合理性、
繳費期限、金額合理性、主旨與藥品清單完整。一維條碼(Code 39)與明細加總目前一律 skip:
OpenCV 解不了 Code 39,ExtractionResult 也還沒有明細欄位(見 einvoice.py、checks.py)。
對外只有 verify_result 一個入口,Pipeline 在「辨識」與「決策」之間呼叫它。

契約:
- 就地填寫 result.verification:{檢查名稱: {"status": "pass"|"fail"|"skip", "detail": str, "fields": [...]}}
- 就地填寫 result.verified_confidence:0~1;沒有任何檢查可用時維持 None,
  決策會退回使用模型自評信心。
- 不得修改 result 的欄位值本身(驗證只判斷,不改答案)。

補充:
- 檢查名稱是中文(如「QR 總計額」「統編檢查碼」),可直接顯示在介面上;fields 用
  src.models.REQUIRED_FIELDS 的命名。以底線開頭的鍵不是檢查項目,介面逐項顯示時要略過:
  - "_coverage":{必要欄位: bool},該欄位有沒有被任何 pass/fail 的檢查涵蓋;
  - "_summary":一句話說明 verified_confidence 的依據,決策理由會引用。
- 各檢查的實作:電子發票 QR 見 einvoice.py,格式/檢查碼/合理性見 checks.py。
- INDEPENDENT_CHECKS(屬於獨立證據的檢查名稱)由本套件重新匯出,介面要區分證據強弱時從這裡取。
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

from ..models import REQUIRED_FIELDS, ExtractionResult
from . import checks as c
from . import einvoice
from .einvoice import INDEPENDENT_CHECKS

log = logging.getLogger(__name__)

QR_ALL_MATCH = 0.95         # QR 與模型在字軌、日期、總額三者一致
QR_REQUIRED_MATCH = 0.90    # QR 只證實必要欄位(日期、總額),模型沒讀出字軌
QR_EXTRA_BONUS = 0.01       # 每多一項 QR 相符的輔助欄位(賣方統編、隨機碼、買方統編)
QR_MAX = 0.98               # 永遠不給 1.0:QR 也可能是別張發票的
RULE_FLOOR = 0.60           # 只有規則檢查時:0.60 + 0.25 × 必要欄位覆蓋率
RULE_SPAN = 0.25
CAP_QR_CONFLICT = 0.10      # 與 QR 矛盾:幾乎確定讀錯
CAP_REQUIRED_FAIL = 0.30    # 必要欄位違反規則:很可能讀錯
CAP_OTHER_FAIL = 0.60       # 其他欄位違反規則:整份結果打折,不自動歸檔

_QR_EXTRA_FIELDS = ("fields.seller_tax_id", "fields.random_code", "fields.buyer_tax_id")


def verify_result(result: ExtractionResult, file_path: Path) -> None:
    """依文件類型執行適用的檢查,填入 result.verification 與 result.verified_confidence。"""
    run_verification(result, file_path, today=date.today())


# 核對程式本身出錯時的說明;web 的轉人工原因也認這句(src.verify 是唯一定義處)
VERIFY_ERROR_SUMMARY = "核對程式發生錯誤,沒有完成核對"


def verify_or_flag(result: ExtractionResult, file_path: Path) -> None:
    """跑 verify_result;核對程式本身出錯時不丟例外,而是把驗證信心設成 0,讓決策轉人工(fail-closed)。

    若出錯時退回模型自評,等於「沒核對反而比較容易自動接受」(原則 1、5,使用者 10/2 決定)。
    Pipeline 與評測都走這裡,兩邊對「核對壞掉」的處理才會一致。
    """
    try:
        verify_result(result, file_path)
    except Exception as exc:
        log.error("核對失敗:%s(%s)", file_path.name, exc)
        result.verification = {"_summary": VERIFY_ERROR_SUMMARY}
        result.verified_confidence = 0.0


def run_verification(result: ExtractionResult, file_path: Path, today: date) -> None:
    """verify_result 的本體;today 由參數傳入,測試才能固定「今天」。"""
    found = collect_checks(result, file_path, today)
    score, summary = score_verification(found, result.doc_type, result.confidence)
    result.verification = {
        **found,
        "_coverage": coverage(found, result.doc_type),
        "_summary": summary,
    }
    result.verified_confidence = score


def collect_checks(result: ExtractionResult, file_path: Path, today: date) -> dict[str, dict[str, Any]]:
    """依 doc_type 決定要跑哪些檢查;沒有對應檢查的類型(其他)回空 dict。"""
    fields = result.fields or {}
    found: dict[str, dict[str, Any]] = {}
    doc_type = result.doc_type

    if doc_type == "發票":
        try:
            found.update(einvoice.verify_einvoice(result, file_path))
        except Exception as exc:  # 解碼器壞掉只是少一項證據,不能讓其他檢查跟著失敗
            log.error("QR 驗證失敗:%s(%s)", file_path.name, exc)
            found[einvoice.CHECK_QR] = c.entry("skip", f"QR 解碼發生錯誤:{type(exc).__name__}", [])
        found[einvoice.CHECK_CODE39] = einvoice.code39_entry()
        found[c.CHECK_INVOICE_FORMAT] = c.check_invoice_number_format(result.invoice_number)
        found[c.CHECK_SELLER_TAX_ID] = c.check_tax_id(
            fields.get("seller_tax_id"), "fields.seller_tax_id", "賣方統編")
        found[c.CHECK_BUYER_TAX_ID] = c.check_tax_id(
            fields.get("buyer_tax_id"), "fields.buyer_tax_id", "買方統編")
        found[c.CHECK_DATE] = c.check_date_plausible(result.date, today)
        found[c.CHECK_PERIOD] = c.check_date_in_period(result.date, fields.get("period"))
        found[c.CHECK_AMOUNT] = c.check_amount_plausible(result.amount, result.currency)
        found[c.CHECK_AMOUNT_SUM] = c.check_amount_sum(result.amount, None)
    elif doc_type == "收據":
        found[c.CHECK_DATE] = c.check_date_plausible(result.date, today)
        found[c.CHECK_AMOUNT] = c.check_amount_plausible(result.amount, result.currency)
        found[c.CHECK_AMOUNT_SUM] = c.check_amount_sum(result.amount, None)
    elif doc_type == "帳單":
        found[c.CHECK_DUE_DATE] = c.check_due_date(fields.get("due_date"), result.date)
        found[c.CHECK_AMOUNT] = c.check_amount_plausible(result.amount, result.currency)
        found[c.CHECK_DATE] = c.check_date_plausible(result.date, today)
    elif doc_type == "公文":
        found[c.CHECK_DATE] = c.check_date_plausible(result.date, today)
        found[c.CHECK_SUBJECT] = c.check_subject(fields.get("subject"))
    elif doc_type == "藥袋":
        found[c.CHECK_DATE] = c.check_date_plausible(result.date, today)
        found[c.CHECK_MEDICATION_ITEMS] = c.check_medication_items(fields.get("items"))
    return found


def coverage(found: dict[str, dict[str, Any]], doc_type: str) -> dict[str, bool]:
    """每個必要欄位有沒有被任何 pass/fail 的檢查涵蓋(skip 不算涵蓋)。"""
    checked = {f for chk in found.values() if chk["status"] != "skip" for f in chk["fields"]}
    return {name: name in checked for name in REQUIRED_FIELDS.get(doc_type, ())}


def score_verification(
    found: dict[str, dict[str, Any]], doc_type: str, model_confidence: float,
) -> tuple[float | None, str]:
    """由檢查結果算出 verified_confidence,回傳 (分數, 一句話依據)。

    規則(單調:多一項 pass 只會持平或變高,多一項 fail 只會持平或變低):

    0. 沒有任何 pass/fail 的檢查 → None(決策退回模型自評信心)。

    1. 基礎分,二選一:
       A. QR 佐證——該類型所有必要欄位都被 QR 比對為 pass:
            字軌、日期、總額三者相符                  0.95
            只有必要欄位(日期、總額)相符,模型沒讀出字軌  0.90
            每多一項 QR 相符的輔助欄位(賣方統編、隨機碼、買方統編)+0.01,上限 0.98
          模型自評信心完全不參與:QR 相符時即使模型只說 0.6 也採信。
       B. 只有規則檢查(格式、檢查碼、合理性)——
            min(模型自評信心, 0.60 + 0.25 × 必要欄位覆蓋率)
          覆蓋率 = 有 pass 且沒有 fail 的必要欄位數 ÷ 必要欄位數。
          規則通過只代表「沒有明顯讀錯」,不能證明讀對,所以只能把關、不能加分:
          分數不超過模型自評,模型自己說沒把握(低分)時不會被規則抬高。

    2. 上限(有矛盾就壓低,取最嚴的一條):
            任何 QR 比對 fail(與獨立證據矛盾)     ≤ 0.10
            必要欄位的規則檢查 fail               ≤ 0.30
            其他欄位的規則檢查 fail(如統編檢查碼) ≤ 0.60

    例:QR 總計額 ≠ 模型金額 → 0.10,即使模型自評 0.99 也轉人工;
        帳單的金額、繳費期限都通過規則檢查、模型自評 0.92 → min(0.92, 0.85) = 0.85。
    """
    active = {name: chk for name, chk in found.items()
              if not name.startswith("_") and chk["status"] != "skip"}
    if not active:
        return None, "沒有可用的檢查"

    required = REQUIRED_FIELDS.get(doc_type, ())
    failed = {name: chk for name, chk in active.items() if chk["status"] == "fail"}
    failed_fields = {f for chk in failed.values() for f in chk["fields"]}
    passed_fields = {f for chk in active.values() if chk["status"] == "pass" for f in chk["fields"]}
    qr_passed = {f for name, chk in active.items()
                 if name in INDEPENDENT_CHECKS and chk["status"] == "pass"
                 for f in chk["fields"]}

    # 1. 基礎分
    if required and set(required) <= qr_passed:
        base = QR_ALL_MATCH if "invoice_number" in qr_passed else QR_REQUIRED_MATCH
        extras = [f for f in _QR_EXTRA_FIELDS if f in qr_passed]
        base = min(base + QR_EXTRA_BONUS * len(extras), QR_MAX)
        matched = [name for name in (einvoice.CHECK_QR_INVOICE, einvoice.CHECK_QR_DATE,
                                     einvoice.CHECK_QR_TOTAL) if active.get(name, {}).get("status") == "pass"]
        summary = f"QR 相符:{'、'.join(n.removeprefix('QR ') for n in matched)}"
    else:
        covered = [f for f in required if f in passed_fields and f not in failed_fields]
        ratio = len(covered) / len(required) if required else 0.0
        ceiling = RULE_FLOOR + RULE_SPAN * ratio
        base = min(float(model_confidence), ceiling)
        summary = f"規則檢查,必要欄位覆蓋 {len(covered)}/{len(required)}"
        if model_confidence < ceiling:
            summary += f",受模型自評 {model_confidence:.2f} 限制"

    # 2. 上限
    score = base
    qr_conflicts = [name for name in failed if name in INDEPENDENT_CHECKS]
    required_fails = [name for name, chk in failed.items()
                      if name not in INDEPENDENT_CHECKS and set(chk["fields"]) & set(required)]
    other_fails = [name for name in failed if name not in qr_conflicts and name not in required_fails]
    if qr_conflicts:
        score = min(score, CAP_QR_CONFLICT)
        summary = f"與 QR 不符:{'、'.join(n.removeprefix('QR ') for n in qr_conflicts)}"
    elif required_fails:
        score = min(score, CAP_REQUIRED_FAIL)
        summary = f"必要欄位檢查不通過:{'、'.join(required_fails)}"
    elif other_fails:
        score = min(score, CAP_OTHER_FAIL)
        summary = f"檢查不通過:{'、'.join(other_fails)}"
    return round(score, 4), summary
