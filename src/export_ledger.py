"""對帳彙整匯出:把處理紀錄(records.jsonl)整理成 Excel(.xlsx)對帳表。

每個原始檔案取**最新一筆**紀錄(重跑會覆蓋前次結果),依日期排序;**依幣別分別小計**
(NTD/USD 等不可混算),並把「待確認 / 低信心」整列醒目標示,方便核銷對帳時優先複查。
可用 doc_type 只匯出發票或收據(國外訂閱常同時有 Invoice 與 Receipt,擇一避免重複計)。
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any

# 對帳表欄位(順序即欄序);「用途/核銷類別」留白供使用者對帳時填寫
COLUMNS = [
    "日期", "類型", "商家", "金額", "幣別", "發票號碼", "信心",
    "狀態", "用途/核銷類別", "原始檔名", "歸檔檔名", "備註",
]
_STATUS = {"archive": "已歸檔", "review": "待確認", "failed": "失敗"}
_DEFAULT_LOW_CONF = 0.80  # 未傳入設定時的預設;正式呼叫應傳 cfg.auto_threshold


def build_rows(records: list[dict[str, Any]], doc_type: str | None = None) -> list[dict[str, Any]]:
    """records.jsonl 內容 → 對帳列。每個原始檔案取最新一筆(依「時間」),再依日期排序。

    doc_type 若指定(如 "發票"/"收據")則只保留該類型。
    """
    latest: dict[str, dict] = {}
    for r in records:
        key = r.get("原始檔案")
        if not key:
            continue
        prev = latest.get(key)
        if prev is None or str(r.get("時間", "")) >= str(prev.get("時間", "")):
            latest[key] = r

    rows: list[dict[str, Any]] = []
    for key, r in latest.items():
        ai = r.get("AI辨識結果") or {}
        if doc_type and ai.get("doc_type") != doc_type:
            continue
        target = r.get("目標路徑") or ""
        rows.append({
            "日期": ai.get("date") or "",
            "類型": ai.get("doc_type") or "",
            "商家": ai.get("vendor") or "",
            "金額": ai.get("amount"),
            "幣別": ai.get("currency") or "NTD",
            "發票號碼": ai.get("invoice_number") or "",
            "信心": ai.get("confidence"),
            "狀態": _STATUS.get(r.get("動作"), r.get("動作") or ""),
            "用途/核銷類別": "",
            "原始檔名": key,
            "歸檔檔名": Path(target).name if target else "",
            "備註": ai.get("notes") or "",
        })
    rows.sort(key=lambda x: (x["幣別"] or "", x["日期"] or "9999-99-99", x["商家"] or ""))
    return rows


def totals_by_currency(rows: list[dict[str, Any]]) -> "OrderedDict[str, float]":
    """依幣別加總金額(略過非數字/缺漏);不同幣別分開、不可混算。"""
    out: OrderedDict[str, float] = OrderedDict()
    for r in rows:
        amt = r.get("金額")
        if isinstance(amt, (int, float)):
            cur = r.get("幣別") or "NTD"
            out[cur] = out.get(cur, 0.0) + float(amt)
    return out


def export_ledger(records, out_path, doc_type: str | None = None,
                  low_conf: float = _DEFAULT_LOW_CONF):
    """把紀錄彙整寫成 .xlsx 對帳表。回傳 (輸出路徑, 筆數, {幣別: 合計})。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    rows = build_rows(records, doc_type=doc_type)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "對帳明細"

    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(bold=True, color="FFFFFF")
    warn_fill = PatternFill("solid", fgColor="FFF2CC")  # 待確認/低信心醒目色
    bold = Font(bold=True)
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    amt_col = COLUMNS.index("金額") + 1
    conf_col = COLUMNS.index("信心") + 1
    vendor_col = COLUMNS.index("商家") + 1

    ws.append(COLUMNS)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

    for row in rows:
        ws.append([row.get(c, "") for c in COLUMNS])
        ridx = ws.max_row
        ws.cell(ridx, amt_col).number_format = "#,##0.00"
        conf = row.get("信心")
        if isinstance(conf, (int, float)):
            ws.cell(ridx, conf_col).number_format = "0.00"
        low = row.get("狀態") == "待確認" or (isinstance(conf, (int, float)) and conf < low_conf)
        for col in range(1, len(COLUMNS) + 1):
            c = ws.cell(ridx, col)
            c.border = border
            if low:
                c.fill = warn_fill

    # 依幣別小計(不同幣別分列、不可混算)
    totals = totals_by_currency(rows)
    ws.append([])
    for cur, tot in totals.items():
        ws.append(["", "", f"合計 ({cur})", tot] + [""] * (len(COLUMNS) - 4))
        tr = ws.max_row
        ws.cell(tr, vendor_col).font = bold
        ws.cell(tr, amt_col).font = bold
        ws.cell(tr, amt_col).number_format = "#,##0.00"

    # 版面:凍結表頭、篩選、欄寬
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{1 + len(rows)}"
    widths = {
        "日期": 12, "類型": 6, "商家": 22, "金額": 11, "幣別": 6, "發票號碼": 14, "信心": 6,
        "狀態": 8, "用途/核銷類別": 16, "原始檔名": 24, "歸檔檔名": 30, "備註": 20,
    }
    for i, c in enumerate(COLUMNS, 1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(c, 12)

    wb.save(out_path)
    return out_path, len(rows), totals
