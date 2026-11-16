"""Background computation worker.

Each web instance may run one of these.  Work claiming and publication are
guarded in SQLite, so running the worker in every instance is safe: a queued
run is claimed exactly once, and a losing instance simply finds no work.
"""

from __future__ import annotations

import logging
import threading

from . import service

log = logging.getLogger("forest.worker")


class Worker(threading.Thread):
    def __init__(
        self,
        db_path: str,
        poll_interval: float = 0.2,
        stale_seconds: int = 120,
        chunk_size: int = 2000,
    ) -> None:
        super().__init__(name="forest-worker", daemon=True)
        self.db_path = db_path
        self.poll_interval = poll_interval
        self.stale_seconds = stale_seconds
        self.chunk_size = chunk_size
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._last_recovery = 0.0

    def kick(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake.set()

    def run(self) -> None:  # pragma: no cover - exercised through integration tests
        log.info("worker started (db=%s)", self.db_path)
        while not self._stop_event.is_set():
            try:
                claimed = service.claim_next_run(self.db_path)
                if claimed is None:
                    self._maybe_recover()
                    self._wake.wait(self.poll_interval)
                    self._wake.clear()
                    continue
                run_id, attempt_token = claimed
                log.info("claimed run %s", run_id)
                service.execute_run(
                    self.db_path, run_id, attempt_token=attempt_token, chunk_size=self.chunk_size
                )
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("worker loop error")
                self._stop_event.wait(max(self.poll_interval, 1.0))
        log.info("worker stopped")

    def _maybe_recover(self) -> None:
        import time

        now = time.monotonic()
        if now - self._last_recovery < 30:
            return
        self._last_recovery = now
        try:
            recovered = service.recover_stale_runs(self.db_path, self.stale_seconds)
            if recovered:
                log.warning("requeued %s stale run(s)", recovered)
        except Exception:  # noqa: BLE001
            log.exception("stale-run recovery failed")
