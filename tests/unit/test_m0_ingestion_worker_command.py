from __future__ import annotations

import json
import os
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from course_insight.contracts.errors import DomainError
from course_insight.infrastructure.deepseek import DEEPSEEK_API_KEY_ENV
from course_insight.infrastructure.deepseek_secrets import (
    save_teacher_deepseek_settings,
)
from course_insight.modules.m1_course_governance.legacy_powerpoint import (
    PowerPointComConverter,
)


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _isolate_worker_command_logs(tmp_path, settings):
    logging_settings = settings.PLATFORM_SETTINGS.logging.model_copy(
        update={"directory": tmp_path / "logs"}
    )
    platform_settings = settings.PLATFORM_SETTINGS.model_copy(
        update={"logging": logging_settings}
    )
    with override_settings(PLATFORM_SETTINGS=platform_settings):
        yield


def test_once_exits_cleanly_when_queue_is_empty(tmp_path) -> None:
    with override_settings(
        COURSE_INSIGHT_INGESTION_STATUS_DIR=tmp_path / "status",
        COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path / "storage",
    ):
        call_command("run_ingestion_worker", "--once", verbosity=0)


def test_worker_initializes_dedicated_structured_logging(
    tmp_path,
    monkeypatch,
) -> None:
    from course_insight.modules.m0_platform.django_app import runtime
    from course_insight.modules.m0_platform.django_app.management.commands import (
        run_ingestion_worker,
    )

    lifecycle: list[str] = []

    class _EmptyWorker:
        def __init__(self, *, processor_factory) -> None:
            del processor_factory

        def run_once(self) -> bool:
            return False

    monkeypatch.setattr(
        runtime,
        "get_application_container",
        lambda *, logging_filename=None: lifecycle.append(str(logging_filename)),
    )
    monkeypatch.setattr(
        runtime,
        "close_application_container",
        lambda: lifecycle.append("closed"),
    )
    monkeypatch.setattr(run_ingestion_worker, "KnowledgeIngestionWorker", _EmptyWorker)

    with override_settings(COURSE_INSIGHT_INGESTION_STATUS_DIR=tmp_path / "status"):
        call_command("run_ingestion_worker", "--once", verbosity=0)

    assert lifecycle == ["ingestion.log", "closed"]


def test_duplicate_worker_error_contains_safe_pid_and_heartbeat(tmp_path) -> None:
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    (status_dir / "worker.lock").write_text(
        json.dumps({"pid": os.getpid(), "heartbeat_at": "2026-08-25T00:00:00+00:00"}),
        encoding="utf-8",
    )

    with override_settings(
        COURSE_INSIGHT_INGESTION_STATUS_DIR=status_dir,
        COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path / "storage",
    ), pytest.raises(CommandError, match=f"PID {os.getpid()}.*heartbeat"):
        call_command("run_ingestion_worker", "--once", verbosity=0)


def test_processor_uses_teacher_model_and_thinking_settings(
    tmp_path,
    monkeypatch,
) -> None:
    from course_insight.modules.m0_platform.django_app.management.commands import (
        run_ingestion_worker,
    )

    monkeypatch.delenv(DEEPSEEK_API_KEY_ENV, raising=False)
    save_teacher_deepseek_settings(
        tmp_path,
        api_key="sk-private-worker-test-key",
        model_name="deepseek-v4-pro",
        thinking_enabled=True,
    )

    with override_settings(
        COURSE_INSIGHT_RUNTIME_DIR=tmp_path,
        COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path / "storage",
    ):
        processor = run_ingestion_worker._build_processor()

    policy = processor._extractor._client.policy
    assert policy.model_name == "deepseek-v4-pro"
    assert policy.thinking_enabled is True


def test_processor_injects_legacy_powerpoint_converter(tmp_path) -> None:
    from course_insight.modules.m0_platform.django_app.management.commands import (
        run_ingestion_worker,
    )

    with override_settings(
        COURSE_INSIGHT_RUNTIME_DIR=tmp_path,
        COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path / "storage",
    ):
        processor = run_ingestion_worker._build_processor()

    assert isinstance(
        processor._legacy_powerpoint_converter,
        PowerPointComConverter,
    )


def test_processor_expands_output_budget_for_thinking_mode(
    tmp_path,
    monkeypatch,
) -> None:
    from course_insight.modules.m0_platform.django_app.management.commands import (
        run_ingestion_worker,
    )

    monkeypatch.delenv(DEEPSEEK_API_KEY_ENV, raising=False)
    save_teacher_deepseek_settings(
        tmp_path,
        api_key="sk-private-worker-test-key",
        model_name="deepseek-v4-flash",
        thinking_enabled=True,
    )

    with override_settings(
        COURSE_INSIGHT_RUNTIME_DIR=tmp_path,
        COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path / "storage",
    ):
        processor = run_ingestion_worker._build_processor()

    assert processor._extractor._client.policy.max_tokens == 16_384


def test_processor_expands_timeout_for_thinking_mode(
    tmp_path,
    monkeypatch,
) -> None:
    from course_insight.modules.m0_platform.django_app.management.commands import (
        run_ingestion_worker,
    )

    monkeypatch.delenv(DEEPSEEK_API_KEY_ENV, raising=False)
    save_teacher_deepseek_settings(
        tmp_path,
        api_key="sk-private-worker-test-key",
        model_name="deepseek-v4-flash",
        thinking_enabled=True,
    )

    with override_settings(
        COURSE_INSIGHT_RUNTIME_DIR=tmp_path,
        COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path / "storage",
    ):
        processor = run_ingestion_worker._build_processor()

    assert processor._extractor._client.policy.timeout_seconds == 120.0


def test_continuous_worker_reloads_teacher_settings_for_each_job(
    tmp_path,
    monkeypatch,
) -> None:
    from course_insight.modules.m0_platform.django_app.management.commands import (
        run_ingestion_worker,
    )

    monkeypatch.delenv(DEEPSEEK_API_KEY_ENV, raising=False)
    save_teacher_deepseek_settings(
        tmp_path,
        api_key="sk-private-worker-test-key",
        model_name="deepseek-v4-flash",
        thinking_enabled=True,
    )
    observed: list[bool] = []

    class _Worker:
        def __init__(self, *, processor_factory) -> None:
            self._processor_factory = processor_factory

        def run_once(self) -> bool:
            observed.append(
                self._processor_factory()._extractor._client.policy.thinking_enabled
            )
            save_teacher_deepseek_settings(
                tmp_path,
                api_key="sk-private-worker-test-key",
                model_name="deepseek-v4-flash",
                thinking_enabled=False,
            )
            observed.append(
                self._processor_factory()._extractor._client.policy.thinking_enabled
            )
            raise KeyboardInterrupt

    monkeypatch.setattr(run_ingestion_worker, "KnowledgeIngestionWorker", _Worker)

    with override_settings(
        COURSE_INSIGHT_RUNTIME_DIR=tmp_path,
        COURSE_INSIGHT_INGESTION_STATUS_DIR=tmp_path / "status",
        COURSE_INSIGHT_KNOWLEDGE_STORAGE_DIR=tmp_path / "storage",
    ):
        call_command("run_ingestion_worker", verbosity=0)

    assert observed == [True, False]


def test_continuous_worker_reports_failed_job_and_keeps_running(
    tmp_path,
    monkeypatch,
) -> None:
    from course_insight.modules.m0_platform.django_app.management.commands import (
        run_ingestion_worker,
    )

    calls: list[int] = []

    class _Worker:
        def __init__(self, *, processor_factory) -> None:
            del processor_factory

        def run_once(self) -> bool:
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                raise DomainError(
                    code="KNOWLEDGE_EXTRACTION_OUTPUT_INVALID",
                    module="m7",
                    message="private model output must not be printed",
                )
            raise KeyboardInterrupt

    monkeypatch.setattr(run_ingestion_worker, "_build_processor", lambda **kwargs: object())
    monkeypatch.setattr(run_ingestion_worker, "KnowledgeIngestionWorker", _Worker)
    stderr = StringIO()

    with override_settings(COURSE_INSIGHT_INGESTION_STATUS_DIR=tmp_path / "status"):
        call_command("run_ingestion_worker", stderr=stderr, verbosity=0)

    assert calls == [1, 2]
    assert "KNOWLEDGE_EXTRACTION_OUTPUT_INVALID" in stderr.getvalue()
    assert "DomainError" in stderr.getvalue()
    assert "private model output" not in stderr.getvalue()
