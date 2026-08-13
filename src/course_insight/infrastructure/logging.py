"""Separate application JSONL logging and canonical domain-audit appends.

``rotating_file`` is deliberately a development/test, single-process mode.
Production configuration is validated elsewhere to use only ``stdout`` so
multiple processes never share a plain ``RotatingFileHandler``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from course_insight.infrastructure.config.errors import ConfigurationError
from course_insight.infrastructure.config.models import LoggingSettings
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.log_context import current_log_context
from course_insight.infrastructure.log_sink import (
    ApplicationLogSinkError,
    SafeRotatingFileHandler,
    SafeStreamHandler,
    WriterLockUnavailable,
)


REDACTED = "[REDACTED]"
_MAX_COLLECTION_ITEMS = 64
_MAX_REDACTION_DEPTH = 8
_MAX_SAFE_TEXT_LENGTH = 256
_SAFE_EVENT = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_SAFE_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_MAPPING_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_. -]{0,63}$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
_SAFE_CLASS_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_CREDENTIAL_DSN = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://[^/@\s]+(?::[^@\s]*)?@"
)
_AUTHORIZATION_VALUE = re.compile(r"(?i)\b(?:bearer|basic)\s+\S+")
_SECRET_HEADER_VALUE = re.compile(
    r"(?i)\b(?:authorization|cookie|set-cookie)\s*:\s*\S+"
)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?:^|[\s\"'])[a-z]:[\\/]")
_UNC_ABSOLUTE_PATH = re.compile(r"(?:^|[\s\"'])\\\\[^\\\s]+\\")
_POSIX_ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s\"'])/(?:[^/\s]+/)*[^/\s]*"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:password|secret|api[\s_.-]*key|access[\s_.-]*token|"
    r"refresh[\s_.-]*token|private[\s_.-]*key)\s*[:=]\s*\S+"
)
_SENSITIVE_KEY_FRAGMENTS = frozenset(
    {
        "password",
        "secret",
        "apikey",
        "authorization",
        "cookie",
        "session",
        "accesstoken",
        "refreshtoken",
        "databaseurl",
        "studentanswer",
        "rawanswer",
        "answer",
        "answers",
        "prompt",
        "rawrequestbody",
        "privatekey",
    }
)
_FIXED_CONTEXT_FIELDS = (
    "run_id",
    "request_id",
    "job_id",
    "worker_id",
    "actor_id",
    "course_id",
    "class_id",
    "attempt_id",
)
_FIXED_FIELDS = frozenset(
    {
        "timestamp",
        "level",
        "logger",
        "event",
        *_FIXED_CONTEXT_FIELDS,
        "duration_ms",
        "error_code",
    }
)
_SAFE_EXTENSION_NAMES = frozenset(
    {
        "status",
        "state",
        "phase",
        "operation",
        "result",
        "failure_stage",
        "contract_type",
        "exception_type",
    }
)
_SAFE_EXTENSION_SUFFIXES = (
    "_count",
    "_version",
    "_checksum",
    "_status",
    "_stage",
    "_ms",
)
_HANDLER_MARKER = "_course_insight_application_handler"
_EVENT_ATTRIBUTE = "_course_insight_event"
_CONTEXT_ATTRIBUTE = "_course_insight_context"
_FIELDS_ATTRIBUTE = "_course_insight_fields"
_DURATION_ATTRIBUTE = "_course_insight_duration_ms"
_ERROR_CODE_ATTRIBUTE = "_course_insight_error_code"
_EXCEPTION_TYPE_ATTRIBUTE = "_course_insight_exception_type"
_CONFIGURATION_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class _LoggerState:
    token: object
    handler: logging.Handler
    fingerprint: tuple[object, ...]
    path: Path | None
    baseline_level: int
    baseline_propagate: bool
    baseline_disabled: bool


_LOGGER_STATES: dict[str, _LoggerState] = {}


def _normalized_key(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _is_sensitive_key(value: str) -> bool:
    normalized = _normalized_key(value)
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _redact_string(value: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return REDACTED
    if (
        len(encoded) > 4096
        or _CONTROL_CHARACTERS.search(value)
        or _CREDENTIAL_DSN.search(value)
        or _AUTHORIZATION_VALUE.search(value)
        or _SECRET_HEADER_VALUE.search(value)
        or _WINDOWS_ABSOLUTE_PATH.search(value)
        or _UNC_ABSOLUTE_PATH.search(value)
        or _POSIX_ABSOLUTE_PATH.search(value)
        or _SECRET_ASSIGNMENT.search(value)
    ):
        return REDACTED
    if len(value) > _MAX_SAFE_TEXT_LENGTH:
        return value[:_MAX_SAFE_TEXT_LENGTH]
    return value


def _safe_exception(value: BaseException) -> dict[str, str | None]:
    exception_type = type(value).__name__
    if not _SAFE_CLASS_NAME.fullmatch(exception_type):
        exception_type = "Exception"
    error_code: str | None = None
    try:
        candidate = vars(value).get("code")
        if type(candidate) is str and _SAFE_IDENTIFIER.fullmatch(candidate):
            error_code = candidate
    except Exception:
        error_code = None
    return {
        "exception_type": exception_type,
        "error_code": error_code,
    }


def redact_log_value(value: Any) -> Any:
    """Recursively make a value JSON-safe without invoking object strings."""

    return _redact_log_value(value, active_ids=set(), depth=0)


def _redact_log_value(
    value: Any,
    *,
    active_ids: set[int],
    depth: int,
) -> Any:
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        return value if -(2**63) <= value <= (2**63 - 1) else REDACTED
    if type(value) is float:
        return value if math.isfinite(value) else REDACTED
    if type(value) is str:
        return _redact_string(value)
    if isinstance(value, BaseException):
        return _safe_exception(value)
    if depth >= _MAX_REDACTION_DEPTH:
        return REDACTED

    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_ids:
            return REDACTED
        active_ids.add(identity)
        try:
            result: dict[str, Any] = {}
            for index, item in enumerate(value.items()):
                if index >= _MAX_COLLECTION_ITEMS:
                    break
                key, nested_value = item
                if (
                    type(key) is not str
                    or not _SAFE_MAPPING_KEY.fullmatch(key)
                ):
                    continue
                if _is_sensitive_key(key):
                    result[key] = REDACTED
                else:
                    result[key] = _redact_log_value(
                        nested_value,
                        active_ids=active_ids,
                        depth=depth + 1,
                    )
            return result
        except Exception:
            return REDACTED
        finally:
            active_ids.discard(identity)

    if type(value) in {list, tuple, set, frozenset}:
        identity = id(value)
        if identity in active_ids:
            return REDACTED
        active_ids.add(identity)
        try:
            return [
                _redact_log_value(
                    item,
                    active_ids=active_ids,
                    depth=depth + 1,
                )
                for index, item in enumerate(value)
                if index < _MAX_COLLECTION_ITEMS
            ]
        except Exception:
            return REDACTED
        finally:
            active_ids.discard(identity)
    return REDACTED


def _safe_identifier(value: Any) -> str | None:
    if type(value) is str and _SAFE_IDENTIFIER.fullmatch(value):
        return value
    return None


def _safe_duration(value: Any) -> int | float | None:
    if type(value) not in {int, float}:
        return None
    if value < 0 or value > 1_000_000_000_000:
        return None
    if type(value) is float and not math.isfinite(value):
        return None
    return value


def _safe_extension_name(name: str) -> bool:
    if (
        not _SAFE_FIELD.fullmatch(name)
        or name in _FIXED_FIELDS
        or _is_sensitive_key(name)
    ):
        return False
    return (
        name in _SAFE_EXTENSION_NAMES
        or name.endswith(_SAFE_EXTENSION_SUFFIXES)
    )


def _safe_extension_value(name: str, value: Any) -> Any:
    if name.endswith(("_count", "_version")):
        if type(value) is int and value >= 0:
            return value
        return None
    if name.endswith("_ms"):
        return _safe_duration(value)
    if type(value) is not str:
        return None
    return _safe_identifier(value)


def _extract_exception(
    record: logging.LogRecord,
) -> tuple[str | None, str | None]:
    exception_type = getattr(record, _EXCEPTION_TYPE_ATTRIBUTE, None)
    error_code = getattr(record, _ERROR_CODE_ATTRIBUTE, None)
    if (
        type(exception_type) is not str
        or not _SAFE_CLASS_NAME.fullmatch(exception_type)
    ):
        exception_type = None
    error_code = _safe_identifier(error_code)

    exc_info = record.exc_info
    if exc_info and exception_type is None:
        try:
            exception_type = exc_info[0].__name__
            if not _SAFE_CLASS_NAME.fullmatch(exception_type):
                exception_type = "Exception"
            safe_error = _safe_exception(exc_info[1])
            error_code = error_code or safe_error["error_code"]
        except Exception:
            exception_type = "Exception"
    return exception_type, error_code


def _build_payload(record: logging.LogRecord) -> dict[str, Any]:
    raw_context = getattr(record, _CONTEXT_ATTRIBUTE, None)
    if type(raw_context) is not dict:
        raw_context = current_log_context().to_dict()
    context = {
        field: _safe_identifier(raw_context.get(field))
        for field in _FIXED_CONTEXT_FIELDS
    }

    raw_event = getattr(record, _EVENT_ATTRIBUTE, None)
    event = (
        raw_event
        if type(raw_event) is str and _SAFE_EVENT.fullmatch(raw_event)
        else "unstructured_log"
    )
    logger_name = (
        record.name
        if type(record.name) is str and _SAFE_EVENT.fullmatch(record.name)
        else "course_insight.application"
    )
    level = (
        record.levelname
        if record.levelname in logging._nameToLevel  # noqa: SLF001
        else "INFO"
    )
    exception_type, error_code = _extract_exception(record)
    timestamp = datetime.fromtimestamp(
        record.created,
        tz=timezone.utc,
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    payload: dict[str, Any] = {
        "timestamp": timestamp,
        "level": level,
        "logger": logger_name,
        "event": event,
        **context,
        "duration_ms": _safe_duration(
            getattr(record, _DURATION_ATTRIBUTE, None)
        ),
        "error_code": error_code,
    }
    if exception_type is not None:
        payload["exception_type"] = exception_type

    raw_fields = getattr(record, _FIELDS_ATTRIBUTE, None)
    if type(raw_fields) is dict:
        for name, value in raw_fields.items():
            if _safe_extension_name(name):
                safe_value = _safe_extension_value(name, value)
                if safe_value is not None:
                    payload[name] = safe_value

    redacted = redact_log_value(payload)
    return redacted if type(redacted) is dict else {
        **payload,
        "event": "logging.redaction_failed",
    }


class RedactionFilter(logging.Filter):
    """Remove unsafe structured extensions before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        raw_fields = getattr(record, _FIELDS_ATTRIBUTE, None)
        if type(raw_fields) is not dict:
            return True
        sanitized = {
            name: _safe_extension_value(name, value)
            for name, value in raw_fields.items()
            if _safe_extension_name(name)
        }
        setattr(
            record,
            _FIELDS_ATTRIBUTE,
            {
                name: value
                for name, value in sanitized.items()
                if value is not None
            },
        )
        return True


class JsonLineFormatter(logging.Formatter):
    """Format only explicitly selected fields; never render message/args."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            return json.dumps(
                _build_payload(record),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except Exception:
            fallback = {
                "timestamp": datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "level": "ERROR",
                "logger": "course_insight.application",
                "event": "logging.format_failed",
                **dict.fromkeys(_FIXED_CONTEXT_FIELDS),
                "duration_ms": None,
                "error_code": "LOG_FORMAT_FAILED",
            }
            return json.dumps(fallback, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class LoggingRuntime:
    """Owned application handler with idempotent cleanup."""

    logger: logging.Logger
    handler: logging.Handler
    path: Path | None
    _token: object

    def close(self) -> None:
        with _CONFIGURATION_LOCK:
            state = _LOGGER_STATES.get(self.logger.name)
            if state is None or state.token is not self._token:
                return
            if self.handler in self.logger.handlers:
                self.logger.removeHandler(self.handler)
            self.logger.setLevel(state.baseline_level)
            self.logger.propagate = state.baseline_propagate
            self.logger.disabled = state.baseline_disabled
            _LOGGER_STATES.pop(self.logger.name, None)
            self.handler.close()


def _logging_destination(
    settings: LoggingSettings,
    stream: Any | None,
) -> tuple[tuple[object, ...], Path | None, Any | None]:
    if settings.mode == "stdout":
        actual_stream = stream if stream is not None else sys.stdout
        return (
            ("stdout", settings.level, id(actual_stream)),
            None,
            actual_stream,
        )
    try:
        path = (settings.directory / settings.filename).resolve()
    except OSError:
        raise ConfigurationError(
            code="LOG_SINK_UNAVAILABLE",
            fields=("logging.directory",),
            reason="unavailable",
        ) from None
    return (
        (
            "rotating_file",
            settings.level,
            os.path.normcase(os.fspath(path)),
            settings.rotation_max_bytes,
            settings.backup_count,
        ),
        path,
        None,
    )


def _build_application_handler(
    settings: LoggingSettings,
    *,
    path: Path | None,
    stream: Any | None,
) -> logging.Handler:
    try:
        if settings.mode == "stdout":
            handler: logging.Handler = SafeStreamHandler(stream)
        else:
            if path is None:
                raise ValueError("missing file destination")
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = SafeRotatingFileHandler(
                path,
                writer_lock_path=path.parent / f".{path.name}.writer.lock",
                maxBytes=settings.rotation_max_bytes,
                backupCount=settings.backup_count,
                encoding="utf-8",
                delay=False,
            )
    except WriterLockUnavailable:
        raise ConfigurationError(
            code="LOG_SINK_IN_USE",
            fields=("logging.directory",),
            reason="writer_conflict",
        ) from None
    except (OSError, ValueError):
        raise ConfigurationError(
            code="LOG_SINK_UNAVAILABLE",
            fields=("logging.directory",),
            reason="unavailable",
        ) from None
    setattr(handler, _HANDLER_MARKER, True)
    handler.addFilter(RedactionFilter())
    handler.setFormatter(JsonLineFormatter())
    return handler


def configure_application_logging(
    settings: LoggingSettings,
    *,
    logger_name: str = "course_insight.application",
    stream: Any | None = None,
) -> LoggingRuntime:
    """Configure one isolated logger; file mode requires one process."""

    if not _SAFE_EVENT.fullmatch(logger_name):
        raise ValueError("logger_name must be a safe logger identifier")

    fingerprint, path, actual_stream = _logging_destination(settings, stream)
    logger = logging.getLogger(logger_name)
    with _CONFIGURATION_LOCK:
        active = _LOGGER_STATES.get(logger_name)
        active_handler = None if active is None else active.handler
        foreign_handlers = [
            current
            for current in logger.handlers
            if current is not active_handler
        ]
        if foreign_handlers:
            raise ConfigurationError(
                code="LOG_HANDLER_CONFLICT",
                fields=("logging.handlers",),
                reason="conflict",
            )
        if active is not None and active.handler not in logger.handlers:
            raise ConfigurationError(
                code="LOG_HANDLER_CONFLICT",
                fields=("logging.handlers",),
                reason="owned_handler_missing",
            )

        baseline_level = (
            logger.level if active is None else active.baseline_level
        )
        baseline_propagate = (
            logger.propagate if active is None else active.baseline_propagate
        )
        baseline_disabled = (
            logger.disabled if active is None else active.baseline_disabled
        )
        token = object()
        if active is not None and active.fingerprint == fingerprint:
            logger.setLevel(settings.level)
            logger.propagate = False
            logger.disabled = False
            _LOGGER_STATES[logger_name] = _LoggerState(
                token=token,
                handler=active.handler,
                fingerprint=fingerprint,
                path=active.path,
                baseline_level=baseline_level,
                baseline_propagate=baseline_propagate,
                baseline_disabled=baseline_disabled,
            )
            return LoggingRuntime(
                logger=logger,
                handler=active.handler,
                path=active.path,
                _token=token,
            )
        if (
            active is not None
            and active.path is not None
            and path == active.path
        ):
            raise ConfigurationError(
                code="LOG_RECONFIGURATION_CONFLICT",
                fields=("logging",),
                reason="same_file_changed",
            )

        handler = _build_application_handler(
            settings,
            path=path,
            stream=actual_stream,
        )
        if active is not None:
            logger.removeHandler(active.handler)
        logger.setLevel(settings.level)
        logger.propagate = False
        logger.disabled = False
        logger.addHandler(handler)
        _LOGGER_STATES[logger_name] = _LoggerState(
            token=token,
            handler=handler,
            fingerprint=fingerprint,
            path=path,
            baseline_level=baseline_level,
            baseline_propagate=baseline_propagate,
            baseline_disabled=baseline_disabled,
        )
        if active is not None:
            active.handler.close()
    return LoggingRuntime(
        logger=logger,
        handler=handler,
        path=path,
        _token=token,
    )


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    duration_ms: int | float | None = None,
    error_code: str | None = None,
    error: BaseException | None = None,
    **safe_fields: Any,
) -> None:
    """Emit an application event without accepting a free-text message."""

    if type(event) is not str or not _SAFE_EVENT.fullmatch(event):
        raise ValueError("event must be a safe event identifier")
    if type(level) is not int or level not in logging._levelToName:  # noqa: SLF001
        raise ValueError("level must be a standard logging level")

    exception_type: str | None = None
    if isinstance(error, BaseException):
        safe_error = _safe_exception(error)
        exception_type = safe_error["exception_type"]
        error_code = error_code or safe_error["error_code"]
    fields = {
        name: value
        for name, value in safe_fields.items()
        if _safe_extension_name(name)
    }
    logger.log(
        level,
        "",
        extra={
            _EVENT_ATTRIBUTE: event,
            _CONTEXT_ATTRIBUTE: current_log_context().to_dict(),
            _FIELDS_ATTRIBUTE: fields,
            _DURATION_ATTRIBUTE: _safe_duration(duration_ms),
            _ERROR_CODE_ATTRIBUTE: _safe_identifier(error_code),
            _EXCEPTION_TYPE_ATTRIBUTE: exception_type,
        },
    )


def append_json_log(
    path: str | os.PathLike[str],
    record: Mapping[str, Any],
) -> None:
    """Durably append one canonical UTF-8 JSON object as a single line."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = dumps_json(dict(record))
    with target.open(mode="a", encoding="utf-8", newline="") as output:
        output.write(f"{line}\n")
        output.flush()
        os.fsync(output.fileno())


__all__ = [
    "ApplicationLogSinkError",
    "JsonLineFormatter",
    "LoggingRuntime",
    "REDACTED",
    "RedactionFilter",
    "append_json_log",
    "configure_application_logging",
    "log_event",
    "redact_log_value",
]
