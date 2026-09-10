"""Cross-platform advisory lock for one live SQLite owner or offline restore."""

from __future__ import annotations

import os
from importlib import import_module
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Protocol, cast


class ProcessLockUnavailableError(RuntimeError):
    """Another process currently owns the runtime lock."""


class _FcntlModule(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, file_descriptor: int, operation: int) -> None: ...


class _MsvcrtModule(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, file_descriptor: int, mode: int, number_of_bytes: int) -> None: ...


class ProcessLock:
    """Hold an operating-system lock; a stale lock file is harmless after a crash."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self._stream: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._stream is not None

    def acquire(self) -> None:
        if self._stream is not None:
            raise RuntimeError("process lock is already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            _lock_nonblocking(stream)
        except OSError as exc:
            stream.close()
            raise ProcessLockUnavailableError("runtime lock is already held") from exc
        self._stream = stream

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        self._stream = None
        try:
            _unlock(stream)
        finally:
            stream.close()

    def __enter__(self) -> ProcessLock:
        self.acquire()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()


def database_lock_path(database_path: Path) -> Path:
    resolved = database_path.expanduser().resolve()
    return resolved.with_name(f".{resolved.name}.runtime.lock")


def _lock_nonblocking(stream: BinaryIO) -> None:
    if os.name == "nt":
        msvcrt = cast(_MsvcrtModule, import_module("msvcrt"))
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        return
    fcntl = cast(_FcntlModule, import_module("fcntl"))
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        msvcrt = cast(_MsvcrtModule, import_module("msvcrt"))
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl = cast(_FcntlModule, import_module("fcntl"))
    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
