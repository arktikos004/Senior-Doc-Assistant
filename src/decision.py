"""決策代理模組:依信心分數與欄位完整度,決定文件的處理方式。

信心分數優先採用自我驗證引擎(src/verify)算出的 verified_confidence;
沒有任何檢查可用時才退回模型自評信心(文獻與實測都顯示自評過度自信)。
比門檻的分數由 effective_score 決定,評測(src/evaluation.py、tools/evaluate.py)也用它掃門檻。
"""
from __future__ import annotations

from .config import AppConfig
from .models import FIELD_LABELS, REQUIRED_FIELDS, Decision, ExtractionResult

IDENTITY_REVIEW_REASON = "身分證明類文件目前不自動判讀,需人工確認"
MANUAL_REVIEW_REASON = "「{label}」初賽還不能自動判讀,需人工確認"   # label:家人選的名稱(保單、身分證…)


def _missing_required(result: ExtractionResult) -> list[str]:
    """回傳缺漏或被模型標記為讀不清的必要欄位(中文標籤)。"""
    missing = []
    for name in REQUIRED_FIELDS.get(result.doc_type, ("date", "amount")):
        if not result.get_field(name) or name in result.unreadable:
            missing.append(FIELD_LABELS.get(name, name))
    return missing


def effective_score(result: ExtractionResult) -> float:
    """比門檻用的分數:有驗證信心就用它,否則退回模型自評信心。

    決策與評測共用這一個函式,評測掃出來的門檻才是決策實際比的分數。
    """
    if result.verified_confidence is not None:
        return result.verified_confidence
    return result.confidence


def _describe_score(result: ExtractionResult) -> str:
    """決策理由裡的分數說明:寫明用的是驗證信心(附依據)還是未經驗證的模型自評。"""
    score = effective_score(result)
    if result.verified_confidence is not None:
        basis = result.verification.get("_summary") if isinstance(result.verification, dict) else None
        label = f"驗證信心 {score:.2f}"
        return f"{label}({basis})" if basis else label
    return f"模型自評信心 {score:.2f}(未經驗證)"


def decide(result: ExtractionResult | None, cfg: AppConfig) -> Decision:
    """決策規則(由上而下逐條檢查,命中即回傳):

    1. 辨識失敗(result 為 None)            → failed
    2. 文件類型不在目標範圍內                → review
    3. 信心分數低於自動歸檔門檻              → review
       (有 verified_confidence 時用它,否則用模型自評 confidence;理由寫明用了哪一種)
    4. 缺少該類型的必要欄位(或讀不清)       → review
    5. 全部通過                              → archive
    """
    if result is None:
        return Decision(action="failed", reason="AI 辨識失敗或回應無法解析")

    if result.doc_type not in cfg.target_doc_types:
        return Decision(
            action="review",
            reason=f"文件類型「{result.doc_type}」不在目標範圍,需人工判斷",
        )

    score, described = effective_score(result), _describe_score(result)
    if score < cfg.auto_threshold:
        return Decision(
            action="review",
            reason=f"{described}低於門檻 {cfg.auto_threshold:.2f},需人工確認",
        )

    missing = _missing_required(result)
    if missing:
        return Decision(
            action="review",
            reason=f"缺少必要欄位:{'、'.join(missing)},需人工補齊",
        )

    return Decision(action="archive", reason=f"{described}達標,自動歸檔")


def decide_manual(result: ExtractionResult | None, cfg: AppConfig, label: str | None = None) -> Decision:
    """上傳時選了初賽不能自動判讀的文件(清單上沒有類型提示的保單、存摺…,以及身分證明整類):
    不論模型讀成什麼(就算讀成欄位齊全的收據)都轉人工。

    label 是家人選的名稱(取自固定清單),原因照實寫「「保單」初賽還不能自動判讀」;身分證明選「不確定」
    沒有名稱,沿用身分證明類的原因。讀不出來的照一般規則記成 failed;其餘一律 review,
    不靠模型把它判成「其他」才轉人工。
    """
    if result is None:
        return decide(None, cfg)
    reason = MANUAL_REVIEW_REASON.format(label=label) if label else IDENTITY_REVIEW_REASON
    return Decision(action="review", reason=reason)
