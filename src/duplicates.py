"""重複上傳:這份文件是不是已經上傳過(只提示,不自動刪;要不要刪由家人決定)。

兩種情況算「可能是同一張」:
- 原件一模一樣(檔案雜湊相同):同一張照片或同一個 PDF 又傳了一次。網頁上傳時先去掉中繼資料才存,
  同一個檔案再傳一次,去完的結果一樣,雜湊也一樣。
- 兩份都是發票,發票號碼都經 QR Code 核對相符、而且一樣(同一張發票拍了兩次)。只有 AI 讀值、
  沒經機讀碼核對的號碼不算:讀錯一碼就會把兩張不同的發票當成同一張。
讀不出來的文件(failed)不比:它們的結果頁是請長輩重拍或重新辨識。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .verify.checks import normalize_invoice_number
from .verify.einvoice import CHECK_QR_INVOICE

SAME_FILE, SAME_INVOICE = "file", "invoice"   # find_duplicate 回傳的相同原因
_CHUNK = 1 << 20


def file_sha256(path: Path) -> str:
    """原件的 SHA-256(十六進位);分段讀,大檔也不會一次讀進記憶體。讀不到丟 OSError。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def verified_invoice_number(result: dict[str, Any] | None) -> str | None:
    """發票號碼經 QR Code 核對相符時,回傳正規化後的號碼(AB12345678);不是發票、沒核對或不符回 None。"""
    if not isinstance(result, dict) or result.get("doc_type") != "發票":
        return None
    verification = result.get("verification")
    check = verification.get(CHECK_QR_INVOICE) if isinstance(verification, dict) else None
    if not isinstance(check, dict) or check.get("status") != "pass":
        return None
    return normalize_invoice_number(result.get("invoice_number"))


def find_duplicate(doc: dict[str, Any], same_file: list[dict[str, Any]],
                   invoices: list[dict[str, Any]]) -> dict[str, Any] | None:
    """在候選文件裡找「可能是同一張」的另一份。

    same_file:原件雜湊和這份相同的文件;invoices:要比號碼的發票(這份的號碼沒經 QR 核對時可以給空清單)。
    兩邊都可以含這份自己,會略過。有好幾份時取最早上傳的那份(之前那一份);這份就是最早的,
    取緊接在後的那份(之後那一份)。編號照上傳順序遞增,用編號比先後。
    回傳 {"id", "created_at", "reason": SAME_FILE | SAME_INVOICE, "earlier": 另一份是不是比較早};沒有回 None。
    """
    if doc.get("action") == "failed":
        return None
    found: dict[int, tuple[dict[str, Any], str]] = {}
    number = verified_invoice_number(doc.get("result"))
    if number:
        for other in invoices:
            if verified_invoice_number(other.get("result")) == number:
                found[other["id"]] = (other, SAME_INVOICE)
    for other in same_file:   # 同一份兩個理由都成立時寫「檔案一模一樣」:比號碼相同更確定
        found[other["id"]] = (other, SAME_FILE)
    candidates = {i: hit for i, hit in found.items() if i != doc["id"] and hit[0].get("action") != "failed"}
    if not candidates:
        return None
    earlier = [i for i in candidates if i < doc["id"]]
    pick = min(earlier) if earlier else min(candidates)
    other, reason = candidates[pick]
    return {"id": pick, "created_at": other.get("created_at"), "reason": reason, "earlier": pick < doc["id"]}
