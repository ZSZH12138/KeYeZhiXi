from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.infrastructure.json_io import write_json


class _RecordingRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def restore(self, course_id: str, refs: object) -> object:
        self.calls.append((course_id, refs))
        return SimpleNamespace(course_id=course_id)


def _manifest(runtime_dir: Path, courses: list[dict[str, object]]) -> Path:
    path = runtime_dir / "snapshots" / "course_runtime_manifest.json"
    write_json(path, {"schema_version": 1, "courses": courses})
    return path


def _write_policies(runtime_dir: Path) -> None:
    write_json(
        runtime_dir / "policies" / "state.json",
        {
            "aggregation_policy_version": "1.0.0",
            "class_id": "class_1",
            "class_size": 1,
            "consolidating_threshold": 0.5,
            "mastered_threshold": 0.8,
            "minimum_assessed_count": 1,
            "minimum_coverage": 1.0,
            "misconception_activation_threshold": 0.5,
        },
    )
    write_json(
        runtime_dir / "policies" / "teacher.json",
        {
            "minimum_coverage": 0.5,
            "minimum_assessed_count": 1,
            "minimum_confidence": 0.5,
            "weak_mastery_threshold": 0.5,
            "misconception_threshold": 0.5,
            "priority_support_threshold": 0.5,
        },
    )


def _entry(course_id: str = "course_1") -> dict[str, object]:
    return {
        "course_id": course_id,
        "course_package_ref": "snapshots/course.json",
        "evidence_index_ref": "snapshots/index.json",
        "knowledge_bundle_ref": "snapshots/knowledge.json",
        "state_policy_ref": "policies/state.json",
        "teacher_threshold_policy_ref": "policies/teacher.json",
    }


def test_runtime_manifest_restores_fixed_refs_and_policy_paths(
    tmp_path: Path,
) -> None:
    from course_insight.modules.m0_platform.django_app.runtime import (
        restore_course_runtime_manifest,
    )

    _write_policies(tmp_path)
    _manifest(tmp_path, [_entry()])
    registry = _RecordingRegistry()
    container = SimpleNamespace(runtime_registry=registry)

    restored = restore_course_runtime_manifest(
        container=container,
        runtime_dir=tmp_path,
    )

    assert tuple(restored) == ("course_1",)
    assert restored["course_1"].course_context.course_id == "course_1"
    assert restored["course_1"].state_policy_path == (
        tmp_path / "policies" / "state.json"
    ).resolve()
    assert restored["course_1"].teacher_threshold_policy_path == (
        tmp_path / "policies" / "teacher.json"
    ).resolve()
    assert len(registry.calls) == 1


@pytest.mark.parametrize(
    "entry_update",
    [
        {"unknown": "value"},
        {"state_policy_ref": "../outside.json"},
        {"state_policy_ref": "C:/outside.json"},
        {"course_id": ""},
    ],
)
def test_runtime_manifest_rejects_unknown_or_unsafe_values(
    tmp_path: Path,
    entry_update: dict[str, object],
) -> None:
    from course_insight.modules.m0_platform.django_app.runtime import (
        restore_course_runtime_manifest,
    )

    _manifest(tmp_path, [{**_entry(), **entry_update}])
    with pytest.raises(DomainError) as captured:
        restore_course_runtime_manifest(
            container=SimpleNamespace(
                runtime_registry=_RecordingRegistry()
            ),
            runtime_dir=tmp_path,
        )

    assert captured.value.code == "RUNTIME_MANIFEST_INVALID"
    assert "path" not in captured.value.details


def test_runtime_manifest_rejects_duplicate_courses_atomically(
    tmp_path: Path,
) -> None:
    from course_insight.modules.m0_platform.django_app.runtime import (
        restore_course_runtime_manifest,
    )

    _manifest(tmp_path, [_entry(), _entry()])
    registry = _RecordingRegistry()
    with pytest.raises(DomainError):
        restore_course_runtime_manifest(
            container=SimpleNamespace(runtime_registry=registry),
            runtime_dir=tmp_path,
        )

    assert registry.calls == []


@pytest.mark.parametrize("failure", ["missing", "invalid"])
def test_runtime_manifest_policy_files_fail_closed_before_restore(
    tmp_path: Path,
    failure: str,
) -> None:
    from course_insight.modules.m0_platform.django_app.runtime import (
        restore_course_runtime_manifest,
    )

    _write_policies(tmp_path)
    if failure == "missing":
        (tmp_path / "policies" / "state.json").unlink()
    else:
        write_json(
            tmp_path / "policies" / "teacher.json",
            {"minimum_coverage": "not-a-number"},
        )
    _manifest(tmp_path, [_entry()])
    registry = _RecordingRegistry()

    with pytest.raises(DomainError) as captured:
        restore_course_runtime_manifest(
            container=SimpleNamespace(runtime_registry=registry),
            runtime_dir=tmp_path,
        )

    assert captured.value.code == "RUNTIME_MANIFEST_INVALID"
    assert registry.calls == []


def test_container_is_closed_when_initialize_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from course_insight.modules.m0_platform.django_app import runtime

    closed: list[bool] = []

    class _FailingM0:
        @staticmethod
        def initialize() -> None:
            raise RuntimeError("database unavailable")

    container = SimpleNamespace(
        m0_service=_FailingM0(),
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(runtime, "_CONTAINER", None)
    monkeypatch.setattr(runtime, "_WEB_RUNTIME", None)
    monkeypatch.setattr(
        runtime,
        "_web_service_overrides",
        lambda settings, logging_filename: None,
    )
    monkeypatch.setattr(
        runtime,
        "build_application",
        lambda settings: container,
    )
    monkeypatch.setattr(
        runtime,
        "configure_application_logging",
        lambda settings: SimpleNamespace(close=lambda: None),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        runtime.get_application_container()

    assert closed == [True]
    assert runtime._CONTAINER is None


def test_web_process_injects_course_class_scoped_m7_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from course_insight.modules.m0_platform.django_app import runtime

    reviewer = object()
    adapter = object()
    monkeypatch.setattr(
        runtime,
        "build_m7_privacy_reviewer",
        lambda **_kwargs: reviewer,
    )
    monkeypatch.setattr(
        runtime,
        "build_scoped_deepseek_m7_adapter",
        lambda **kwargs: adapter
        if kwargs["privacy_reviewer"] is reviewer
        else None,
    )

    overrides = runtime._web_service_overrides(  # noqa: SLF001
        SimpleNamespace(environment="development", runtime_dir=tmp_path),
        logging_filename=None,
    )

    assert overrides is not None
    assert overrides.m7_rubric_adapter is adapter


def test_container_can_use_a_separate_rotating_log_filename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django.conf import settings as django_settings

    from course_insight.infrastructure.config.models import LoggingSettings
    from course_insight.modules.m0_platform.django_app import runtime

    captured: list[str] = []

    class _ReadyM0:
        @staticmethod
        def initialize() -> None:
            return None

    container = SimpleNamespace(
        m0_service=_ReadyM0(),
        close=lambda: None,
    )
    monkeypatch.setattr(runtime, "_CONTAINER", None)
    monkeypatch.setattr(runtime, "_WEB_RUNTIME", None)
    monkeypatch.setattr(runtime, "_LOGGING_RUNTIME", None)
    monkeypatch.setattr(
        django_settings,
        "PLATFORM_SETTINGS",
        SimpleNamespace(
            logging=LoggingSettings(
                level="INFO",
                mode="rotating_file",
                directory=Path("logs"),
                filename="app.log",
            )
        ),
    )
    monkeypatch.setattr(
        runtime,
        "build_application",
        lambda settings: container,
    )

    def _capture_logging(settings: object) -> object:
        captured.append(getattr(settings, "filename"))
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(runtime, "configure_application_logging", _capture_logging)

    built = runtime.get_application_container(logging_filename="outbox.log")

    assert built is container
    assert captured == ["outbox.log"]
    runtime.close_application_container()
