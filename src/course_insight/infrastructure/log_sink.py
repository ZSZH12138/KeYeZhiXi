"""Fail-closed application log handlers and exclusive file-writer locks."""

from __future__ import annotations

import logging
import os
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from course_insight.infrastructure.config.errors import ConfigurationError


_LOCKS_GUARD = threading.RLock()
_LOCKED_FILE_PATHS: set[Path] = set()


class ApplicationLogSinkError(ConfigurationError):
    """A safe, path-free failure raised when an application sink cannot write."""

    def __init__(self) -> None:
        super().__init__(
            code="LOG_SINK_WRITE_FAILED",
            fields=("logging.sink",),
            reason="write_failed",
        )


class _FailClosedEmit:
    def handleError(self, record: logging.LogRecord) -> None:
        del record
        raise ApplicationLogSinkError() from None


class SafeStreamHandler(_FailClosedEmit, logging.StreamHandler):
    """A stream handler that never lets stdlib logging swallow write errors."""

    def emit(self, record: logging.LogRecord) -> None:
        failed = False
        try:
            message = self.format(record)
            self.stream.write(message + self.terminator)
            self.flush()
        except RecursionError:
            raise
        except Exception:
            failed = True
        if failed:
            self.handleError(record)


class WriterLockUnavailable(RuntimeError):
    """Raised without a path when another process owns the file-writer lock."""


class _ExclusiveWriterLock:
    def __init__(self, path: Path, handle: Any) -> None:
        self._path = path
        self._handle = handle

    @classmethod
    def acquire(cls, path: Path) -> _ExclusiveWriterLock:
        resolved_path = path.resolve()
        with _LOCKS_GUARD:
            if resolved_path in _LOCKED_FILE_PATHS:
                raise WriterLockUnavailable

            handle = path.open("a+b")
            try:
                cls._prepare_and_lock(handle)
            except (OSError, ValueError):
                handle.close()
                raise WriterLockUnavailable from None
            _LOCKED_FILE_PATHS.add(resolved_path)
        return cls(resolved_path, handle)

    @staticmethod
    def _prepare_and_lock(handle: Any) -> None:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return

        import fcntl

        fcntl.flock(
            handle.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )

    def close(self) -> None:
        with _LOCKS_GUARD:
            handle = self._handle
            if handle is None:
                return
            self._handle = None
            try:
                self._unlock(handle)
            except (OSError, ValueError):
                pass
            finally:
                handle.close()
                _LOCKED_FILE_PATHS.discard(self._path)

    @staticmethod
    def _unlock(handle: Any) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SafeRotatingFileHandler(_FailClosedEmit, RotatingFileHandler):
    """Single-writer rotating handler guarded across OS processes."""

    def __init__(
        self,
        filename: Path,
        *,
        writer_lock_path: Path,
        maxBytes: int,
        backupCount: int,
        encoding: str,
        delay: bool,
    ) -> None:
        self._writer_lock = _ExclusiveWriterLock.acquire(writer_lock_path)
        try:
            super().__init__(
                filename,
                maxBytes=maxBytes,
                backupCount=backupCount,
                encoding=encoding,
                delay=delay,
            )
        except Exception:
            self._writer_lock.close()
            raise

    def emit(self, record: logging.LogRecord) -> None:
        failed = False
        try:
            if self.shouldRollover(record):
                self.doRollover()
            if self.stream is None:
                self.stream = self._open()
            message = self.format(record)
            self.stream.write(message + self.terminator)
            self.flush()
        except RecursionError:
            raise
        except Exception:
            failed = True
        if failed:
            self.handleError(record)

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._writer_lock.close()


__all__ = [
    "ApplicationLogSinkError",
    "SafeRotatingFileHandler",
    "SafeStreamHandler",
    "WriterLockUnavailable",
]
