"""Deterministic, side-effect-free configuration source helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from course_insight.infrastructure.config.errors import ConfigurationError


_DATABASE_DSN = re.compile(
    r"^(?:postgres(?:ql)?|mysql|mariadb|oracle|mssql|sqlite)(?:\+\w+)?://",
    re.IGNORECASE,
)
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "database_url",
        "password",
        "secret",
        "secret_key",
        "token",
        "url",
    }
)
_LEGACY_ENV_PATHS: dict[str, tuple[str, ...]] = {
    "ENVIRONMENT": ("environment",),
    "RUNTIME_DIR": ("runtime_dir",),
    "CONFIG_DIR": ("config_dir",),
    "DATABASE_PATH": ("database", "sqlite_path"),
    "LOG_LEVEL": ("logging", "level"),
}


def safe_defaults() -> dict[str, Any]:
    return {
        "environment": "development",
        "runtime_dir": "runtime",
        "config_dir": "config",
        "database": {
            "backend": "sqlite",
            "sqlite_path": "course_insight.db",
            "url_env": "DATABASE_URL",
            "pool_min_size": 1,
            "pool_max_size": 10,
            "connect_timeout_seconds": 10.0,
        },
        "logging": {
            "level": "INFO",
            "mode": "rotating_file",
            "directory": "logs",
            "filename": "app.log",
            "rotation_max_bytes": 10 * 1024 * 1024,
            "backup_count": 5,
        },
        "outbox": {
            "batch_size": 100,
            "poll_interval_seconds": 1.0,
            "lease_seconds": 30.0,
            "max_retries": 8,
            "retry_base_seconds": 1.0,
            "retry_max_seconds": 300.0,
            "retry_jitter_ratio": 0.2,
            "heartbeat_interval_seconds": 10.0,
        },
        "web": {
            "secret_key_env": "DJANGO_SECRET_KEY",
            "allowed_hosts_env": "DJANGO_ALLOWED_HOSTS",
            "csrf_trusted_origins_env": "DJANGO_CSRF_TRUSTED_ORIGINS",
            "allowed_hosts": ["localhost", "127.0.0.1"],
            "csrf_trusted_origins": [],
            "secure_cookie": False,
            "session_timeout_seconds": 3600,
        },
        "security": {
            "session_cookie_httponly": True,
            "session_cookie_samesite": "Lax",
            "max_request_body_bytes": 1024 * 1024,
            "login_failure_limit": 5,
            "login_failure_window_seconds": 300,
        },
    }


def deep_merge(
    base: Mapping[str, Any],
    overlay: Mapping[str, Any],
) -> dict[str, Any]:
    merged = deepcopy(dict(base))
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = deepcopy(value)
    return merged


def load_app_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as source:
            payload = json.load(
                source,
                object_pairs_hook=_reject_duplicate_pairs,
            )
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigurationError(
            code="APP_JSON_UNREADABLE",
            fields=("app_json",),
            reason="unreadable",
        ) from None
    if not isinstance(payload, dict):
        raise ConfigurationError(
            code="APP_JSON_INVALID",
            fields=("app_json",),
            reason="object_required",
        )
    _reject_app_secrets(payload)
    return payload


def _reject_app_secrets(
    value: Any,
    *,
    path: tuple[str, ...] = (),
) -> None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key)
            field_path = (*path, key)
            normalized = key.lower()
            if _is_secret_key(normalized):
                raise ConfigurationError(
                    code="SECRET_IN_APP_JSON",
                    fields=(".".join(field_path),),
                    reason="secret_value_forbidden",
                )
            _reject_app_secrets(nested, path=field_path)
        return
    if isinstance(value, list):
        for item in value:
            _reject_app_secrets(item, path=path)
        return
    if isinstance(value, str) and _DATABASE_DSN.match(value):
        raise ConfigurationError(
            code="DATABASE_DSN_IN_APP_JSON",
            fields=(".".join(path) or "app_json",),
            reason="database_dsn_forbidden",
        )


def _reject_duplicate_pairs(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(
                code="APP_JSON_INVALID",
                fields=("app_json",),
                reason="duplicate_key",
            )
        result[key] = value
    return result


def _is_secret_key(normalized: str) -> bool:
    if normalized.endswith("_env"):
        return False
    if normalized in _SECRET_KEYS:
        return True
    underscored = re.sub(r"[^a-z0-9]+", "_", normalized)
    parts = set(underscored.split("_"))
    return bool(
        parts.intersection({"password", "passwd", "secret", "token"})
        or "api_key" in underscored
        or "apikey" in underscored
    )


def load_dotenv(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    try:
        loaded = dotenv_values(path, encoding="utf-8", interpolate=False)
    except (OSError, UnicodeError):
        raise ConfigurationError(
            code="DOTENV_UNREADABLE",
            fields=("dotenv",),
            reason="unreadable",
        ) from None
    return {
        key: value
        for key, value in loaded.items()
        if value is not None
    }


def environment_to_settings(
    environment: Mapping[str, str],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_key, raw_value in environment.items():
        if raw_key in _LEGACY_ENV_PATHS:
            _assign(result, _LEGACY_ENV_PATHS[raw_key], _parse_value(raw_value))
            continue
        prefix = "COURSE_INSIGHT_"
        if not raw_key.startswith(prefix):
            continue
        suffix = raw_key[len(prefix) :]
        path = tuple(segment.lower() for segment in suffix.split("__"))
        if not path or any(not segment for segment in path):
            continue
        _assign(result, path, _parse_value(raw_value))
    return result


def _parse_value(value: str) -> Any:
    stripped = value.strip()
    if stripped.startswith(("[", "{")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return value
    lowered = stripped.lower()
    if lowered in {"true", "false", "null"}:
        return json.loads(lowered)
    if re.fullmatch(
        r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?",
        stripped,
    ):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return value
    return value


def _assign(
    target: dict[str, Any],
    path: tuple[str, ...],
    value: Any,
) -> None:
    current = target
    for segment in path[:-1]:
        nested = current.setdefault(segment, {})
        if not isinstance(nested, dict):
            nested = {}
            current[segment] = nested
        current = nested
    current[path[-1]] = value
