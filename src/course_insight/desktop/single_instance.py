"""Windows named-mutex ownership for one desktop launcher per user session."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Protocol


_MUTEX_NAME = r"Local\KeYeZhiXi.Desktop"
_ERROR_ALREADY_EXISTS = 183


class DesktopSingleInstanceError(RuntimeError):
    """Raised when Windows cannot create the desktop ownership mutex."""


class MutexApi(Protocol):
    def create_mutex(self, name: str) -> tuple[int, bool]: ...

    def close_handle(self, handle: int) -> None: ...


class SingleInstance:
    """Acquire and release the launcher mutex without affecting children."""

    def __init__(self, *, api: MutexApi | None = None) -> None:
        self._api = api or _WindowsMutexApi()
        self._handle: int | None = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        handle, already_exists = self._api.create_mutex(_MUTEX_NAME)
        if already_exists:
            self._api.close_handle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        self._api.close_handle(self._handle)
        self._handle = None


class _WindowsMutexApi:
    def __init__(self) -> None:
        if not hasattr(ctypes, "WinDLL"):
            raise DesktopSingleInstanceError("当前系统不支持 Windows 单实例锁")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._create = kernel32.CreateMutexW
        self._create.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        self._create.restype = wintypes.HANDLE
        self._close = kernel32.CloseHandle
        self._close.argtypes = (wintypes.HANDLE,)
        self._close.restype = wintypes.BOOL

    def create_mutex(self, name: str) -> tuple[int, bool]:
        ctypes.set_last_error(0)
        handle = self._create(None, False, name)
        if not handle:
            raise DesktopSingleInstanceError("无法创建应用单实例锁")
        return int(handle), ctypes.get_last_error() == _ERROR_ALREADY_EXISTS

    def close_handle(self, handle: int) -> None:
        self._close(wintypes.HANDLE(handle))

