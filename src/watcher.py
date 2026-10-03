"""資料夾監控模組:以 watchdog 監控 inbox,新檔案進入即觸發處理管線。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import AppConfig
from .pipeline import Pipeline

log = logging.getLogger(__name__)


def wait_until_stable(
    file_path: Path, checks: int = 3, interval: float = 1.0, timeout: float = 60.0
) -> bool:
    """等待檔案大小連續數次不變,確保掃描器/傳輸已寫入完畢。"""
    deadline = time.monotonic() + timeout
    last_size = -1
    stable_count = 0
    while time.monotonic() < deadline:
        try:
            size = file_path.stat().st_size
        except OSError:
            return False  # 檔案被移走或刪除
        if size == last_size and size > 0:
            stable_count += 1
            if stable_count >= checks:
                return True
        else:
            stable_count = 0
            last_size = size
        time.sleep(interval)
    return False


class InboxHandler(FileSystemEventHandler):
    def __init__(self, cfg: AppConfig, pipeline: Pipeline):
        self.cfg = cfg
        self.pipeline = pipeline

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(Path(str(event.src_path)))

    def on_moved(self, event: FileSystemEvent) -> None:
        # 有些程式先寫暫存檔再改名,改名後才算真正進入 inbox
        if not event.is_directory:
            self._handle(Path(str(event.dest_path)))

    def _handle(self, file_path: Path) -> None:
        if file_path.suffix.lower() not in self.cfg.supported_extensions:
            log.info("忽略不支援的檔案:%s", file_path.name)
            return
        log.info("偵測到新檔案:%s,等待寫入完成…", file_path.name)
        if not wait_until_stable(file_path):
            log.warning("檔案未穩定或已被移除,略過:%s", file_path.name)
            return
        self.pipeline.process_file(file_path)


def run_watch(cfg: AppConfig, pipeline: Pipeline) -> None:
    """啟動監控(先處理 inbox 既有檔案,再持續監聽新檔案)。"""
    inbox = cfg.paths.inbox
    inbox.mkdir(parents=True, exist_ok=True)

    existing = [
        f for f in sorted(inbox.iterdir())
        if f.is_file() and f.suffix.lower() in cfg.supported_extensions
    ]
    if existing:
        log.info("inbox 內已有 %d 個檔案,先行處理", len(existing))
        for f in existing:
            pipeline.process_file(f)

    observer = Observer()
    observer.schedule(InboxHandler(cfg, pipeline), str(inbox), recursive=False)
    observer.start()
    log.info("開始監控資料夾:%s(按 Ctrl+C 停止)", inbox)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("收到停止指令,正在關閉監控…")
    finally:
        observer.stop()
        observer.join()
