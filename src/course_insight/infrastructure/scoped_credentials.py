"""Authenticated at-rest protection for teacher-supplied scoped secrets."""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProtectedSecret:
    scheme: str
    payload: bytes


def protect_secret(value: str, *, context: str) -> ProtectedSecret:
    """Protect one non-empty secret and bind it to its exact scope."""

    if not isinstance(value, str) or not value or not context:
        raise ValueError("secret and context must not be blank")
    raw = value.encode("utf-8")
    if os.name == "nt":
        return ProtectedSecret(
            scheme="windows-dpapi-v1",
            payload=_windows_protect(raw, _entropy(context)),
        )
    return ProtectedSecret(
        scheme="fernet-v1",
        payload=_fernet(context).encrypt(raw),
    )


def unprotect_secret(protected: ProtectedSecret, *, context: str) -> str:
    """Open one scope-bound protected secret or fail without details."""

    try:
        if protected.scheme == "windows-dpapi-v1" and os.name == "nt":
            raw = _windows_unprotect(protected.payload, _entropy(context))
        elif protected.scheme == "fernet-v1":
            raw = _fernet(context).decrypt(protected.payload)
        else:
            raise ValueError
        value = raw.decode("utf-8")
        if not value:
            raise ValueError
        return value
    except Exception:
        raise ValueError("protected secret is unavailable") from None


def _entropy(context: str) -> bytes:
    return hashlib.sha256(
        f"course-insight-scoped-secret-v1\0{context}".encode("utf-8")
    ).digest()


def _fernet(context: str):
    master = os.environ.get("COURSE_INSIGHT_CREDENTIAL_MASTER_KEY", "").strip()
    if len(master) < 32:
        raise ValueError("secure credential master key is unavailable")
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise ValueError("secure credential protector is unavailable") from None
    key = hashlib.sha256(
        f"{master}\0{context}".encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _windows_protect(raw: bytes, entropy: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    data_blob, data_buffer = _windows_blob(raw)
    entropy_blob, entropy_buffer = _windows_blob(entropy)
    output = _WindowsDataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = (
        ctypes.POINTER(_WindowsDataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_WindowsDataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_WindowsDataBlob),
    )
    crypt32.CryptProtectData.restype = wintypes.BOOL
    result = crypt32.CryptProtectData(
        ctypes.byref(data_blob),
        "course-insight scoped credential",
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,
        ctypes.byref(output),
    )
    del data_buffer, entropy_buffer
    if not result:
        raise ValueError("protected secret is unavailable")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.WinDLL("kernel32", use_last_error=True).LocalFree(output.pbData)


def _windows_unprotect(payload: bytes, entropy: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    data_blob, data_buffer = _windows_blob(payload)
    entropy_blob, entropy_buffer = _windows_blob(entropy)
    output = _WindowsDataBlob()
    description = wintypes.LPWSTR()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = (
        ctypes.POINTER(_WindowsDataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_WindowsDataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_WindowsDataBlob),
    )
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    result = crypt32.CryptUnprotectData(
        ctypes.byref(data_blob),
        ctypes.byref(description),
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,
        ctypes.byref(output),
    )
    del data_buffer, entropy_buffer
    if not result:
        raise ValueError("protected secret is unavailable")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        if description:
            kernel32.LocalFree(description)
        kernel32.LocalFree(output.pbData)


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _WindowsDataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]


    def _windows_blob(value: bytes):
        buffer = ctypes.create_string_buffer(value)
        return (
            _WindowsDataBlob(
                len(value),
                ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
            ),
            buffer,
        )


__all__ = ["ProtectedSecret", "protect_secret", "unprotect_secret"]
