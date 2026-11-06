"""上傳後的背景辨識與進度(UX-3):收下檔案就回應,辨識在背景做,進度頁來問做到哪一步。

- 模型一次只讀一張:這台電腦只有一張 GPU,兩張同時送會互搶顯示記憶體,兩張都變慢、甚至逾時。
  後來的照上傳順序排隊,進度頁寫前面還有幾張。
- 每一步是程式真的做完才記下(Pipeline 的 on_stage),畫面不會先亮。
- 進度只放在記憶體,只給上傳的那個人看:服務重開就沒了。文件不會跟著不見:收件時就先建好「處理中」的那一筆
  (src.pipeline.receive_document),開機時改成「還沒讀」,在文件櫃找得到、可以重新辨識;進度頁請家人到文件櫃找。
- 背景執行緒是 daemon,沒事做就結束;一個 app 一份 JobBoard。
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from src.pipeline import STAGE_CHECKED, STAGE_READ, error_kind

log = logging.getLogger(__name__)

# 進度頁的四步:收到照片 → AI 照抄文字 → 程式核對 → 決定怎麼處理
STAGE_RECEIVED, STAGE_DECIDED = "received", "decided"
STAGES = (STAGE_RECEIVED, STAGE_READ, STAGE_CHECKED, STAGE_DECIDED)
JOB_TTL = 30 * 60   # 秒;做完放超過就忘掉(進度頁的網址跟著失效,文件在文件櫃)

# work(report) 做完一份文件並回傳文件編號;report(stage) 在每一步做完時呼叫
Work = Callable[[Callable[[str], None]], int]


@dataclass
class Job:
    token: str
    seq: int                       # 上傳順序(排隊用)
    owner: str                     # 誰上傳的(principal.actor);進度只給本人看
    provider: str                  # 上傳當下的辨識模式(進度頁說明「誰在讀」)
    is_pdf: bool
    label: str                     # 家人選的類型或文件名稱;沒選是空字串
    photo: bytes | None            # 進度頁的小縮圖(JPEG,只在記憶體);PDF 或讀不到就沒有
    seconds: dict[str, float] = field(default_factory=dict)   # 做完的步驟 → 花了幾秒
    started: float | None = None   # 輪到這張的時間;None 是還在排隊
    stage_since: float = 0.0       # 目前這一步從什麼時候開始
    doc_id: int | None = None
    failed: bool = False
    finished: float | None = None
    failed_doc: int | None = None  # 失敗時文件沒有動(改類型):錯誤頁連回這份,不叫人重新上傳
    for_doc: int | None = None     # 處理的是哪一份文件(收件時建好的、重新辨識的):開機恢復不動還在做的這份


class JobBoard:
    """背景辨識的佇列與進度。時間一律用 time.monotonic()(clock 可以換掉,測試用)。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._cond = threading.Condition()
        self._jobs: dict[str, Job] = {}
        self._queue: deque[tuple[Job, Work]] = deque()
        self._worker: threading.Thread | None = None
        self._seq = 0

    def submit(self, work: Work, *, owner: str, provider: str, is_pdf: bool, label: str,
               photo: bytes | None, received: float, failed_doc: int | None = None,
               for_doc: int | None = None) -> Job:
        """排進佇列;received 是「收到照片」那一步花的秒數(檢查格式、去掉拍攝資訊)。"""
        with self._cond:
            self._sweep()
            self._seq += 1
            job = Job(secrets.token_urlsafe(24), self._seq, owner, provider, is_pdf, label, photo,
                      seconds={STAGE_RECEIVED: received}, failed_doc=failed_doc, for_doc=for_doc)
            self._jobs[job.token] = job
            self._queue.append((job, work))
            if self._worker is None:
                self._worker = threading.Thread(target=self._pump, name="kanyou-upload", daemon=True)
                self._worker.start()
        return job

    def get(self, token: str, owner: str) -> Job | None:
        """token 對應的進度;不是本人上傳的、過期或不存在都回 None。"""
        with self._cond:
            self._sweep()
            job = self._jobs.get(token)
            return job if job is not None and job.owner == owner else None

    def wait(self, job: Job, seconds: float) -> bool:
        """最多等 seconds 秒;做完(成功或失敗)回 True。"""
        with self._cond:
            return self._cond.wait_for(lambda: job.finished is not None, timeout=seconds)

    def busy_docs(self) -> set[int]:
        """還在排隊或正在做的工作處理的是哪幾份文件(開機恢復跳過它們:它們沒有中斷)。"""
        with self._cond:
            return {j.for_doc for j in self._jobs.values() if j.finished is None and j.for_doc is not None}

    def snapshot(self, job: Job) -> dict[str, object]:
        """進度頁要的資料(複本,不會邊讀邊變):做完的步驟秒數、目前這一步做了幾秒、前面還有幾張。"""
        with self._cond:
            now = self._clock()
            ahead = sum(1 for j in self._jobs.values() if j.seq < job.seq and j.finished is None)
            return {"seconds": dict(job.seconds), "queued": job.started is None, "ahead": ahead,
                    "elapsed": now - job.stage_since if job.started is not None else 0.0,
                    "doc_id": job.doc_id, "failed": job.failed}

    def _pump(self) -> None:
        while True:
            with self._cond:
                if not self._queue:
                    self._worker = None
                    return
                job, work = self._queue.popleft()
            self._run(job, work)

    def _run(self, job: Job, work: Work) -> None:
        with self._cond:
            job.started = job.stage_since = self._clock()

        def report(stage: str) -> None:
            with self._cond:
                now = self._clock()
                job.seconds[stage] = now - job.stage_since
                job.stage_since = now

        try:
            doc_id = work(report)
        except Exception as exc:
            # 只寫例外種類:訊息可能帶歸檔路徑(檔名有日期、商家與金額)
            log.error("背景辨識失敗:%s", error_kind(exc))
            doc_id = None
        with self._cond:
            job.finished = self._clock()
            if doc_id is None:
                job.failed = True
            else:
                job.seconds[STAGE_DECIDED] = job.finished - job.stage_since
                job.doc_id = doc_id
            job.photo = None   # 做完就不留縮圖
            self._cond.notify_all()

    def _sweep(self) -> None:
        now = self._clock()
        for token in [t for t, j in self._jobs.items() if j.finished is not None and now - j.finished > JOB_TTL]:
            del self._jobs[token]
