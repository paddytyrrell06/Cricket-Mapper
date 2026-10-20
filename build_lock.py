"""Nonblocking process lock for the scanner's shared staging/publication paths."""
from __future__ import annotations

import errno
import os
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class BuildBusy(RuntimeError):
    """Another process is currently collecting or publishing this terminal."""


class BuildLock:
    """Hold one OS lock until exit, close, or process termination.

    The lock file stays in place after release. Removing it would let another
    process create and lock a different inode while an existing waiter still
    holds the original file open.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> BuildLock:
        if self._handle is not None:
            raise RuntimeError("BuildLock is already acquired")
        # Do not truncate a current owner's diagnostic before obtaining its lock.
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BuildBusy("Another Market Terminal scan is running; "
                                "wait for it to finish before refreshing again") from None
            raise
        try:
            handle.seek(0)
            handle.write(f"{os.getpid()}\n".encode("ascii"))
            handle.truncate()
        except Exception:
            handle.close()
            raise
        self._handle = handle
        return self

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> BuildLock:
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()
