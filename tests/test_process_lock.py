from __future__ import annotations

from pathlib import Path

import pytest

from szs_hub.storage.process_lock import ProcessLock, ProcessLockUnavailableError


def test_process_lock_is_exclusive_and_recoverable(tmp_path: Path) -> None:
    path = tmp_path / "runtime.lock"
    first = ProcessLock(path)
    second = ProcessLock(path)
    first.acquire()
    try:
        with pytest.raises(ProcessLockUnavailableError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()
