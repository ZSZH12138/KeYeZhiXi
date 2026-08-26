"""Run the dedicated knowledge-ingestion worker with explicit lock diagnostics."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from course_insight.application.knowledge_ingestion import KnowledgeIngestionProcessor
from course_insight.contracts.errors import DomainError
from course_insight.contracts.intelligence import LLMModelRef
from course_insight.infrastructure.deepseek import DeepSeekClient
from course_insight.infrastructure.deepseek_secrets import public_deepseek_status
from course_insight.modules.m0_platform.django_app import runtime
from course_insight.modules.m0_platform.django_app.scoped_deepseek import (
    resolve_scoped_deepseek_settings,
    scoped_deepseek_status,
)
from course_insight.modules.m0_platform.ingestion_worker import KnowledgeIngestionWorker
from course_insight.modules.m1_course_governance.legacy_powerpoint import (
    PowerPointComConverter,
)
from course_insight.modules.m7_local_model.knowledge_extraction import (
    DeepSeekKnowledgeExtractionAdapter,
)
from course_insight.modules.m7_local_model.policy import (
    DEFAULT_M7_EXECUTION_POLICY,
    M7ExecutionPolicy,
)
from course_insight.modules.m7_local_model.question_linking import (
    DeepSeekQuestionLinkingAdapter,
)


_THINKING_MAX_TOKENS = 16_384
_THINKING_TIMEOUT_SECONDS = 120.0
_NON_THINKING_TIMEOUT_SECONDS = 30.0
_SAFE_EXCEPTION_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class Command(BaseCommand):
    help = "Process queued course-knowledge ingestion jobs."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--poll-seconds", type=float, default=2.0)

    def handle(self, *args, **options):
        del args
        status_dir = Path(settings.COURSE_INSIGHT_INGESTION_STATUS_DIR)
        status_dir.mkdir(parents=True, exist_ok=True)
        lock_path = status_dir / "worker.lock"
        _acquire_lock(lock_path)
        try:
            runtime.get_application_container(
                logging_filename=runtime.INGESTION_WORKER_LOG_FILENAME,
            )
            worker = KnowledgeIngestionWorker(
                processor_factory=lambda job=None: _build_processor(
                    course_id=None if job is None else job.course_id,
                    class_id=None if job is None else job.class_id,
                    heartbeat=lambda: _write_lock(lock_path),
                )
            )
            if options["once"]:
                worker.run_once()
                return
            poll_seconds = float(options["poll_seconds"])
            if not 0.1 <= poll_seconds <= 60.0:
                raise CommandError("poll seconds must be between 0.1 and 60")
            while True:
                _write_lock(lock_path)
                try:
                    processed = worker.run_once()
                except Exception as error:
                    self.stderr.write(
                        "ingestion job failed safely: "
                        f"{_safe_error_code(error)} "
                        f"({_safe_exception_type(error)})"
                    )
                    continue
                if not processed:
                    time.sleep(poll_seconds)
        except KeyboardInterrupt:
            return
        finally:
            lock_path.unlink(missing_ok=True)
            runtime.close_application_container()


def _build_processor(
    *,
    course_id: str | None = None,
    class_id: str | None = None,
    heartbeat=None,
) -> KnowledgeIngestionProcessor:
    runtime_dir = Path(settings.COURSE_INSIGHT_RUNTIME_DIR)
    scoped = course_id is not None and class_id is not None
    if scoped:
        resolved = resolve_scoped_deepseek_settings(course_id, class_id)
        status = scoped_deepseek_status(course_id, class_id)
        api_key = "" if resolved is None else resolved.api_key
    else:
        status = public_deepseek_status(runtime_dir)
        api_key = None
    policy = M7ExecutionPolicy(
        model_name=status.model_name,
        thinking_enabled=status.thinking_enabled,
        max_tokens=(
            _THINKING_MAX_TOKENS
            if status.thinking_enabled
            else DEFAULT_M7_EXECUTION_POLICY.max_tokens
        ),
    )
    client = DeepSeekClient(
        model_name=policy.model_name,
        model_version=policy.model_version,
        max_tokens=policy.max_tokens,
        thinking_enabled=policy.thinking_enabled,
        temperature=policy.temperature,
        timeout_seconds=(
            _THINKING_TIMEOUT_SECONDS
            if policy.thinking_enabled
            else _NON_THINKING_TIMEOUT_SECONDS
        ),
        runtime_dir=runtime_dir,
        api_key=api_key,
    )
    fallback_client = (
        DeepSeekClient(
            model_name=policy.model_name,
            model_version=policy.model_version,
            max_tokens=policy.max_tokens,
            thinking_enabled=False,
            temperature=policy.temperature,
            timeout_seconds=_NON_THINKING_TIMEOUT_SECONDS,
            runtime_dir=runtime_dir,
            api_key=api_key,
        )
        if policy.thinking_enabled
        else None
    )
    return KnowledgeIngestionProcessor(
        storage_root=Path(settings.COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR),
        extraction_adapter=DeepSeekKnowledgeExtractionAdapter(
            client,
            policy,
            fallback_client=fallback_client,
        ),
        question_linking_adapter=DeepSeekQuestionLinkingAdapter(client, policy),
        model_ref=LLMModelRef(
            model_name=client.model_name,
            model_version=client.model_version,
            status="configured",
        ),
        heartbeat=heartbeat,
        legacy_powerpoint_converter=PowerPointComConverter(),
    )


def _safe_error_code(error: Exception) -> str:
    if isinstance(error, DomainError):
        return error.code
    return "KNOWLEDGE_INGESTION_FAILED"


def _safe_exception_type(error: Exception) -> str:
    candidate = type(error).__name__
    return candidate if _SAFE_EXCEPTION_TYPE.fullmatch(candidate) else "Exception"


def _acquire_lock(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        record = _read_lock(path)
        pid = record.get("pid")
        heartbeat = record.get("heartbeat_at", "unknown")
        if isinstance(pid, int) and _pid_is_live(pid):
            raise CommandError(
                f"ingestion worker already active: PID {pid}, heartbeat {heartbeat}"
            ) from None
        path.unlink(missing_ok=True)
        return _acquire_lock(path)
    else:
        os.close(descriptor)
        _write_lock(path)


def _write_lock(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _read_lock(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _pid_is_live(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_is_live(pid)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _windows_pid_is_live(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    error_access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = open_process(process_query_limited_information, False, pid)
    if handle:
        close_handle(handle)
        return True
    return ctypes.get_last_error() == error_access_denied
