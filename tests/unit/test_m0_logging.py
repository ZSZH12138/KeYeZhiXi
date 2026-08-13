from __future__ import annotations

import asyncio
import io
import json
import logging
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.infrastructure.config import (
    ConfigurationError,
    LoggingSettings,
)
from course_insight.infrastructure.log_context import (
    LogContext,
    bind_log_context,
    current_log_context,
)
from course_insight.infrastructure.logging import (
    ApplicationLogSinkError,
    REDACTED,
    append_json_log,
    configure_application_logging,
    log_event,
    redact_log_value,
)


FIXED_FIELDS = {
    "timestamp",
    "level",
    "logger",
    "event",
    "run_id",
    "request_id",
    "job_id",
    "worker_id",
    "actor_id",
    "course_id",
    "class_id",
    "attempt_id",
    "duration_ms",
    "error_code",
}


def _settings(
    directory: Path,
    *,
    mode: str = "rotating_file",
    rotation_max_bytes: int = 1024 * 1024,
    backup_count: int = 2,
) -> LoggingSettings:
    return LoggingSettings(
        level="INFO",
        mode=mode,
        directory=directory,
        filename="app.log",
        rotation_max_bytes=rotation_max_bytes,
        backup_count=backup_count,
    )


def _configure(directory: Path, **overrides: object):
    return configure_application_logging(
        _settings(directory, **overrides),
        logger_name=f"course_insight.application.test.{uuid4().hex}",
    )


def _read_lines(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def test_app_log_is_jsonl_with_all_fixed_fields_and_context(
    tmp_path: Path,
) -> None:
    runtime = _configure(tmp_path / "logs")
    try:
        with bind_log_context(
            run_id="run_1",
            request_id="request_1",
            actor_id="actor_1",
            course_id="course_1",
            class_id="class_1",
            attempt_id="attempt_1",
            job_id="job_1",
            worker_id="worker_1",
        ):
            log_event(
                runtime.logger,
                "assessment.completed",
                duration_ms=12.5,
                status="completed",
            )
    finally:
        runtime.close()

    records = _read_lines(tmp_path / "logs" / "app.log")
    assert len(records) == 1
    record = records[0]
    assert FIXED_FIELDS <= record.keys()
    assert record["timestamp"].endswith("Z")
    assert record["level"] == "INFO"
    assert record["event"] == "assessment.completed"
    assert record["request_id"] == "request_1"
    assert record["worker_id"] == "worker_1"
    assert record["duration_ms"] == 12.5
    assert record["error_code"] is None
    assert record["status"] == "completed"


def test_missing_context_fields_are_null(tmp_path: Path) -> None:
    runtime = _configure(tmp_path / "logs")
    try:
        log_event(runtime.logger, "platform.started")
    finally:
        runtime.close()

    record = _read_lines(tmp_path / "logs" / "app.log")[0]
    for field in FIXED_FIELDS - {
        "timestamp",
        "level",
        "logger",
        "event",
    }:
        assert record[field] is None


def test_nested_context_restores_previous_immutable_value() -> None:
    assert current_log_context() == LogContext()
    with bind_log_context(run_id="outer", request_id="request_outer") as outer:
        assert current_log_context() is outer
        with bind_log_context(request_id="request_inner") as inner:
            assert inner.run_id == "outer"
            assert inner.request_id == "request_inner"
            assert outer.request_id == "request_outer"
        assert current_log_context() is outer
    assert current_log_context() == LogContext()


def test_context_restores_after_exception() -> None:
    with pytest.raises(RuntimeError, match="stop"):
        with bind_log_context(run_id="run_failure"):
            raise RuntimeError("stop")
    assert current_log_context() == LogContext()


def test_context_isolated_between_threads(tmp_path: Path) -> None:
    runtime = _configure(tmp_path / "logs")

    def emit(index: int) -> None:
        assert current_log_context() == LogContext()
        with bind_log_context(
            request_id=f"request_{index}",
            worker_id=f"worker_{index}",
        ):
            log_event(runtime.logger, "worker.claimed", item_count=index)

    try:
        with bind_log_context(request_id="main_request"):
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(emit, range(40)))
            assert current_log_context().request_id == "main_request"
    finally:
        runtime.close()

    records = _read_lines(tmp_path / "logs" / "app.log")
    assert len(records) == 40
    assert {
        (record["request_id"], record["worker_id"])
        for record in records
    } == {(f"request_{i}", f"worker_{i}") for i in range(40)}


def test_context_isolated_between_async_tasks(tmp_path: Path) -> None:
    runtime = _configure(tmp_path / "logs")

    async def emit(index: int) -> None:
        with bind_log_context(request_id=f"async_{index}"):
            await asyncio.sleep(0)
            log_event(runtime.logger, "request.completed")

    async def run_all() -> None:
        await asyncio.gather(*(emit(i) for i in range(10)))

    try:
        asyncio.run(run_all())
    finally:
        runtime.close()

    records = _read_lines(tmp_path / "logs" / "app.log")
    assert {record["request_id"] for record in records} == {
        f"async_{i}" for i in range(10)
    }


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "Pass-word",
        "SECRET",
        "api.key",
        "Authorization_Header",
        "cookie",
        "session-id",
        "accessToken",
        "refresh_token",
        "database-url",
        "studentAnswers",
        "raw_answer",
        "answer",
        "ANSWERS",
        "prompt",
        "rawRequestBody",
        "private.key",
    ],
)
def test_recursive_redaction_covers_sensitive_key_variants(key: str) -> None:
    value = redact_log_value(
        {"outer": [{key: "do-not-log"}, ("safe", {key: "also-secret"})]}
    )
    serialized = json.dumps(value)
    assert "do-not-log" not in serialized
    assert "also-secret" not in serialized
    assert serialized.count(REDACTED) == 2


@pytest.mark.parametrize(
    "value",
    [
        "postgresql://user:password@db.internal/course",
        "postgresql://credential@db.internal/course",
        "Bearer eyJ.secret.token",
        "Basic dXNlcjpwYXNz",
        "Authorization: opaque-credential",
        "Cookie: sessionid=opaque-credential",
        r"C:\Users\student\private.txt",
        r"\\server\share\private.txt",
        "/var/lib/course-insight/private.txt",
        "password=hunter2",
    ],
)
def test_scalar_redaction_recognizes_credentials_and_absolute_paths(
    value: str,
) -> None:
    assert redact_log_value(value) == REDACTED


def test_redaction_handles_all_container_types_and_cycles() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    result = redact_log_value(
        {
            "list": ["safe", {"password": "secret"}],
            "tuple": ("safe", {"cookie": "secret"}),
            "set": {"safe", "Bearer token"},
            "cycle": cyclic,
        }
    )
    serialized = json.dumps(result)
    assert "secret" not in serialized
    assert "Bearer" not in serialized
    assert REDACTED in serialized


def test_domain_error_does_not_log_message_details_cause_or_paths(
    tmp_path: Path,
) -> None:
    runtime = _configure(tmp_path / "logs")
    error = DomainError(
        code="ASSESSMENT_FAILED",
        module="M8",
        message=r"failed at C:\secrets\answer.txt with token-secret",
        details={
            "database_url": "postgresql://user:password@db/course",
            "student_answer": "private-answer",
        },
    )
    error.__cause__ = RuntimeError("Bearer cause-secret /var/private/file")
    try:
        log_event(runtime.logger, "assessment.failed", error=error)
    finally:
        runtime.close()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    record = json.loads(text)
    assert record["error_code"] == "ASSESSMENT_FAILED"
    assert record["exception_type"] == "DomainError"
    for forbidden in (
        "token-secret",
        "private-answer",
        "password@",
        "cause-secret",
        r"C:\secrets",
        "/var/private",
    ):
        assert forbidden not in text


def test_malicious_str_and_unstructured_logger_message_fail_closed(
    tmp_path: Path,
) -> None:
    class Hostile:
        def __str__(self) -> str:
            raise AssertionError("__str__ must not be called")

    runtime = _configure(tmp_path / "logs")
    try:
        assert redact_log_value(Hostile()) == REDACTED
        runtime.logger.error(
            Hostile(),
            extra={"password": "direct-secret"},
        )
        runtime.logger.error(
            "Bearer message-secret at C:\\private\\file",
            RuntimeError("argument-secret"),
        )
    finally:
        runtime.close()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert len(text.splitlines()) == 2
    assert "direct-secret" not in text
    assert "message-secret" not in text
    assert "argument-secret" not in text
    assert all(
        json.loads(line)["event"] == "unstructured_log"
        for line in text.splitlines()
    )


def test_rotation_produces_only_complete_json_lines(tmp_path: Path) -> None:
    runtime = _configure(
        tmp_path / "logs",
        rotation_max_bytes=1024,
        backup_count=3,
    )
    try:
        for index in range(80):
            log_event(
                runtime.logger,
                "rotation.record",
                item_count=index,
                failure_stage="x" * 120,
            )
    finally:
        runtime.close()

    paths = list((tmp_path / "logs").glob("app.log*"))
    assert len(paths) >= 2
    assert len(paths) <= 4
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            assert json.loads(line)["event"] == "rotation.record"


def test_more_than_one_hundred_threads_never_break_json_lines(
    tmp_path: Path,
) -> None:
    runtime = _configure(tmp_path / "logs")

    def emit(index: int) -> None:
        with bind_log_context(request_id=f"request_{index}"):
            log_event(runtime.logger, "thread.record", item_count=index)

    try:
        with ThreadPoolExecutor(max_workers=24) as executor:
            list(executor.map(emit, range(150)))
    finally:
        runtime.close()

    records = _read_lines(tmp_path / "logs" / "app.log")
    assert len(records) == 150
    assert {record["item_count"] for record in records} == set(range(150))


def test_unavailable_log_directory_fails_closed_without_path(
    tmp_path: Path,
) -> None:
    unavailable = tmp_path / "not-a-directory"
    unavailable.write_text("occupied", encoding="utf-8")

    with pytest.raises(ConfigurationError) as captured:
        _configure(unavailable)

    assert captured.value.code == "LOG_SINK_UNAVAILABLE"
    assert captured.value.fields == ("logging.directory",)
    assert str(tmp_path) not in str(captured.value)


def test_stdout_mode_does_not_touch_log_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    untouched = tmp_path / "must-not-exist" / "logs"
    runtime = _configure(untouched, mode="stdout")
    try:
        log_event(runtime.logger, "production.ready")
    finally:
        runtime.close()

    assert not untouched.exists()
    record = json.loads(capsys.readouterr().out)
    assert record["event"] == "production.ready"


def test_repeated_configuration_does_not_duplicate_output(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "logs")
    logger_name = f"course_insight.application.test.{uuid4().hex}"
    first = configure_application_logging(settings, logger_name=logger_name)
    second = configure_application_logging(settings, logger_name=logger_name)
    try:
        first.close()
        log_event(second.logger, "configured.once")
    finally:
        second.close()

    records = _read_lines(tmp_path / "logs" / "app.log")
    assert [record["event"] for record in records] == ["configured.once"]


def test_configuration_rejects_foreign_target_handler_without_mutating_it(
    tmp_path: Path,
) -> None:
    logger_name = f"course_insight.application.test.{uuid4().hex}"
    logger = logging.getLogger(logger_name)
    foreign_stream = io.StringIO()
    foreign = logging.StreamHandler(foreign_stream)
    logger.addHandler(foreign)
    try:
        with pytest.raises(ConfigurationError) as captured:
            configure_application_logging(
                _settings(tmp_path / "logs"),
                logger_name=logger_name,
            )
        assert captured.value.code == "LOG_HANDLER_CONFLICT"
        assert foreign in logger.handlers
        assert foreign_stream.getvalue() == ""
    finally:
        logger.removeHandler(foreign)
        foreign.close()


def test_runtime_close_restores_logger_state_and_old_runtime_cannot_revert(
    tmp_path: Path,
) -> None:
    logger_name = f"course_insight.application.test.{uuid4().hex}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.ERROR)
    logger.propagate = True
    logger.disabled = True

    first = configure_application_logging(
        _settings(tmp_path / "logs"),
        logger_name=logger_name,
    )
    second = configure_application_logging(
        _settings(tmp_path / "logs"),
        logger_name=logger_name,
    )
    assert logger.level == logging.INFO
    assert not logger.propagate
    assert not logger.disabled

    first.close()
    assert not logger.propagate
    second.close()
    assert logger.level == logging.ERROR
    assert logger.propagate
    assert logger.disabled


def test_failed_reconfiguration_keeps_existing_safe_handler_atomic(
    tmp_path: Path,
) -> None:
    logger_name = f"course_insight.application.test.{uuid4().hex}"
    first = configure_application_logging(
        _settings(tmp_path / "working"),
        logger_name=logger_name,
    )
    unavailable = tmp_path / "not-a-directory"
    unavailable.write_text("occupied", encoding="utf-8")
    root = logging.getLogger()
    root_stream = io.StringIO()
    root_handler = logging.StreamHandler(root_stream)
    root.addHandler(root_handler)
    try:
        with pytest.raises(ConfigurationError) as captured:
            configure_application_logging(
                _settings(unavailable),
                logger_name=logger_name,
            )
        assert captured.value.code == "LOG_SINK_UNAVAILABLE"
        assert first.handler in first.logger.handlers
        assert not first.logger.propagate
        first.logger.error(
            "Bearer raw-secret",
            RuntimeError("argument-secret"),
        )
    finally:
        first.close()
        root.removeHandler(root_handler)
        root_handler.close()

    assert root_stream.getvalue() == ""
    text = (tmp_path / "working" / "app.log").read_text(encoding="utf-8")
    assert "raw-secret" not in text
    assert "argument-secret" not in text
    assert json.loads(text)["event"] == "unstructured_log"


def test_same_file_with_changed_rotation_is_rejected_without_disruption(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "logs"
    logger_name = f"course_insight.application.test.{uuid4().hex}"
    first = configure_application_logging(
        _settings(directory, rotation_max_bytes=1024),
        logger_name=logger_name,
    )
    try:
        with pytest.raises(ConfigurationError) as captured:
            configure_application_logging(
                _settings(directory, rotation_max_bytes=2048),
                logger_name=logger_name,
            )
        assert captured.value.code == "LOG_RECONFIGURATION_CONFLICT"
        assert first.handler in first.logger.handlers
        assert not first.logger.propagate
        log_event(first.logger, "configuration.preserved")
    finally:
        first.close()

    record = _read_lines(directory / "app.log")[0]
    assert record["event"] == "configuration.preserved"


def test_runtime_write_failure_is_safe_and_propagates_when_logging_swallows(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingStream:
        def write(self, value: str) -> None:
            raise OSError("unsafe stream path C:\\private\\app.log")

        def flush(self) -> None:
            return None

    old_raise_exceptions = logging.raiseExceptions
    logging.raiseExceptions = False
    runtime = configure_application_logging(
        _settings(tmp_path / "untouched", mode="stdout"),
        logger_name=f"course_insight.application.test.{uuid4().hex}",
        stream=FailingStream(),
    )
    try:
        with pytest.raises(ApplicationLogSinkError) as captured:
            runtime.logger.error(
                "Bearer raw-secret",
                RuntimeError("argument-secret"),
            )
        assert captured.value.code == "LOG_SINK_WRITE_FAILED"
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
    finally:
        runtime.close()
        logging.raiseExceptions = old_raise_exceptions

    captured_output = capsys.readouterr()
    assert "raw-secret" not in captured_output.err
    assert "argument-secret" not in captured_output.err
    assert str(tmp_path) not in str(captured.value)


def test_rollover_failure_is_safe_and_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _configure(
        tmp_path / "logs",
        rotation_max_bytes=1024,
    )

    def fail_rollover() -> None:
        raise OSError(r"unsafe C:\private\app.log")

    monkeypatch.setattr(runtime.handler, "shouldRollover", lambda record: True)
    monkeypatch.setattr(runtime.handler, "doRollover", fail_rollover)
    try:
        with pytest.raises(ApplicationLogSinkError) as captured:
            log_event(runtime.logger, "rotation.failed")
        assert captured.value.code == "LOG_SINK_WRITE_FAILED"
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        assert r"C:\private" not in str(captured.value)
    finally:
        runtime.close()


def test_rotating_file_allows_only_one_writer_per_path(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "logs"
    first = _configure(directory)
    try:
        with pytest.raises(ConfigurationError) as captured:
            _configure(directory)
        assert captured.value.code == "LOG_SINK_IN_USE"
        assert str(tmp_path) not in str(captured.value)
    finally:
        first.close()

    replacement = _configure(directory)
    replacement.close()


def test_rotating_file_os_lock_rejects_a_second_process(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "logs"
    runtime = _configure(directory)
    child_code = "\n".join(
        (
            "import logging, sys",
            "from pathlib import Path",
            "from course_insight.infrastructure.config import "
            "ConfigurationError, LoggingSettings",
            "from course_insight.infrastructure.logging import "
            "configure_application_logging",
            "settings = LoggingSettings(",
            "    directory=Path(sys.argv[1]),",
            "    mode='rotating_file',",
            ")",
            "try:",
            "    configure_application_logging(",
            "        settings, logger_name='course_insight.child'",
            "    )",
            "except ConfigurationError as error:",
            "    print(error.code)",
            "    raise SystemExit(0 if error.code == 'LOG_SINK_IN_USE' else 2)",
            "raise SystemExit(3)",
        )
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", child_code, str(directory)],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        runtime.close()

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "LOG_SINK_IN_USE"


def test_invalid_event_id_and_duration_are_rejected_or_discarded(
    tmp_path: Path,
) -> None:
    runtime = _configure(tmp_path / "logs")
    try:
        with pytest.raises(ValueError, match="event"):
            log_event(runtime.logger, "bad event\nBearer secret")
        with pytest.raises(ValueError, match="request_id"):
            with bind_log_context(request_id=r"C:\private\request"):
                pass
        log_event(runtime.logger, "duration.invalid", duration_ms=float("nan"))
        log_event(runtime.logger, "duration.huge", duration_ms=10**10_000)
    finally:
        runtime.close()

    records = _read_lines(tmp_path / "logs" / "app.log")
    assert [record["duration_ms"] for record in records] == [None, None]


def test_audit_append_semantics_remain_canonical_and_separate(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit" / "learning_events.jsonl"
    record = {
        "event_id": "event_1",
        "payload": {"student_answer": "domain-owned-value"},
    }
    append_json_log(audit_path, record)

    assert _read_lines(audit_path) == [record]
    assert not (tmp_path / "logs" / "app.log").exists()
    assert not (FIXED_FIELDS & _read_lines(audit_path)[0].keys())


def test_log_level_and_safe_extension_boundaries(tmp_path: Path) -> None:
    runtime = _configure(tmp_path / "logs")
    try:
        log_event(
            runtime.logger,
            "worker.failed",
            level=logging.ERROR,
            error_code="OUTBOX_RETRY",
            status="retrying",
            password="must-disappear",
            arbitrary_object=object(),
        )
    finally:
        runtime.close()

    text = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    record = json.loads(text)
    assert record["level"] == "ERROR"
    assert record["error_code"] == "OUTBOX_RETRY"
    assert record["status"] == "retrying"
    assert "must-disappear" not in text
    assert "password" not in record
    assert "arbitrary_object" not in record
