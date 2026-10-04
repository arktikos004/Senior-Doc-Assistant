"""錯誤紀錄裡怎麼稱呼正在核對的那份文件。

預設用檔名:上傳檔名是系統重新產生的,不含文件內容。家人更正時核對讀的是已歸檔的原檔,檔名有日期、商家與金額
(藥袋的商家是醫療院所),由呼叫端改給「文件 N」(verify_or_flag 的 log_name)。用 ContextVar 帶著走,
verify_result、collect_checks 與各檢查的簽名都不必多一個參數。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

_name: ContextVar[str | None] = ContextVar("verify_log_name", default=None)


def log_label(file_path: Path) -> str:
    """紀錄裡指這份文件的名字:呼叫端有給就用它,沒有就用檔名。"""
    return _name.get() or file_path.name


@contextmanager
def logged_as(name: str | None) -> Iterator[None]:
    """這段期間的核對紀錄改用 name 稱呼文件(None = 照舊用檔名)。"""
    token = _name.set(name)
    try:
        yield
    finally:
        _name.reset(token)
