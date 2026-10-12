"""Cross-process file lock with stale detection (dead pid or too old)."""
from __future__ import annotations

import os
import time
from pathlib import Path


class LockBusy(Exception):
    pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class FileLock:
    def __init__(self, path: str | Path, stale_after_s: float = 1800):
        self.path = Path(path)
        self.stale_after_s = stale_after_s

    def _is_stale(self) -> bool:
        try:
            pid_s, ts_s = self.path.read_text().split(":")
            return not _alive(int(pid_s)) or time.time() - float(ts_s) > self.stale_after_s
        except (FileNotFoundError, ValueError):
            return True

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._is_stale():
                    self.path.unlink(missing_ok=True)
                    continue
                raise LockBusy(str(self.path))
            with os.fdopen(fd, "w") as f:
                f.write(f"{os.getpid()}:{time.time()}")
            return
        raise LockBusy(str(self.path))

    def release(self) -> None:
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
