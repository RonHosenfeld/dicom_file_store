"""In-process send-job registry with lock-guarded progress tracking.

Same threading conventions as ReceiverManager and study_index: plain
``threading.Lock`` everywhere, daemon worker threads, no asyncio.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque

MAX_JOBS = 20
MAX_ERRORS = 20


class SendProgress:
    """Thread-safe counters for one send job."""

    def __init__(self, total_instances: int, total_series: int) -> None:
        self._lock = threading.Lock()
        self.total_instances = total_instances
        self.total_series = total_series
        self.sent_ok = 0
        self.failed = 0
        self.series_done = 0
        self.current_series = ""
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self._errors: deque[dict] = deque(maxlen=MAX_ERRORS)

    def start(self) -> None:
        with self._lock:
            self.started_at = time.monotonic()

    def record(self, status: int, error: str | None = None) -> None:
        with self._lock:
            if status == 0x0000:
                self.sent_ok += 1
            else:
                self.failed += 1
                if error:
                    self._errors.append({"status": f"0x{status:04X}", "error": error})

    def series_started(self, label: str) -> None:
        with self._lock:
            self.current_series = label

    def series_finished(self) -> None:
        with self._lock:
            self.series_done += 1
            self.current_series = ""

    def finish(self) -> None:
        with self._lock:
            self.finished_at = time.monotonic()

    @property
    def state_note(self) -> str:
        with self._lock:
            return self.current_series

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = None
            rate = None
            if self.started_at is not None:
                end = self.finished_at if self.finished_at is not None else time.monotonic()
                elapsed = round(end - self.started_at, 2)
                done = self.sent_ok + self.failed
                if elapsed > 0 and done > 0:
                    rate = round(done / elapsed, 1)
            return {
                "total_instances": self.total_instances,
                "total_series": self.total_series,
                "sent_ok": self.sent_ok,
                "failed": self.failed,
                "series_done": self.series_done,
                "current_series": self.current_series,
                "elapsed_seconds": elapsed,
                "rate_per_sec": rate,
                "errors": list(self._errors),
            }


class SendJob:
    """One background send: progress, cancel flag, and its worker thread."""

    def __init__(self, label: str, progress: SendProgress, runner) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.label = label
        self.progress = progress
        self.cancel_event = threading.Event()
        self.state = "running"
        self._runner = runner
        self._thread = threading.Thread(
            target=self._run, name=f"send-{self.id}", daemon=True
        )

    def _run(self) -> None:
        try:
            self._runner(self.progress, self.cancel_event)
            if self.cancel_event.is_set():
                self.state = "cancelled"
            elif self.state == "running":
                self.state = "completed"
        except Exception as exc:  # never let a job thread die silently
            self.state = "failed"
            self.progress.record(0xC000, error=str(exc))
        finally:
            self.progress.finish()

    def start(self) -> None:
        self.progress.start()
        self._thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()


class JobRegistry:
    """Keeps the most recent jobs, evicting oldest finished ones first."""

    def __init__(self, max_jobs: int = MAX_JOBS) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, SendJob] = {}
        self._max_jobs = max_jobs

    def create(self, label: str, total_instances: int, total_series: int,
               runner) -> SendJob:
        job = SendJob(label, SendProgress(total_instances, total_series), runner)
        with self._lock:
            self._evict_locked()
            self._jobs[job.id] = job
        job.start()
        return job

    def _evict_locked(self) -> None:
        if len(self._jobs) < self._max_jobs:
            return
        finished = [j for j in self._jobs.values() if j.state != "running"]
        for job in sorted(finished, key=lambda j: j.id)[: len(self._jobs) - self._max_jobs + 1]:
            del self._jobs[job.id]

    def get(self, job_id: str) -> SendJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[SendJob]:
        with self._lock:
            return list(self._jobs.values())
