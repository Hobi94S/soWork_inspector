from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path


class ProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.seek(0)
            existing = self._handle.read().strip() or "unknown"
            self._handle.close()
            self._handle = None
            raise RuntimeError(f"Another inspector instance is already running: {existing}") from exc

        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(
            f"pid={os.getpid()} started_at={datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}\n"
        )
        self._handle.flush()

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
