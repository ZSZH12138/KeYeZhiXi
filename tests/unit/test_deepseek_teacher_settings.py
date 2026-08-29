from __future__ import annotations

import os
from pathlib import Path

from course_insight.infrastructure.deepseek import DEEPSEEK_API_KEY_ENV
from course_insight.infrastructure.deepseek_secrets import (
    public_deepseek_status,
    resolve_deepseek_api_key,
    save_teacher_deepseek_settings,
)


def test_teacher_file_key_is_masked_and_env_wins(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(DEEPSEEK_API_KEY_ENV, raising=False)
    status = save_teacher_deepseek_settings(
        tmp_path,
        api_key="sk-test-deepseek-secret-key",
        model_name="deepseek-v4-flash",
        thinking_enabled=False,
    )
    assert status.configured is True
    assert status.key_source == "file"
    assert "sk-test-deepseek-secret-key" not in status.masked_key
    assert status.endpoint == "https://api.deepseek.com"
    assert resolve_deepseek_api_key(tmp_path) == "sk-test-deepseek-secret-key"

    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "sk-env-key-value-1234")
    overridden = public_deepseek_status(tmp_path)
    assert overridden.key_source == "env"
    assert overridden.env_overrides_file is True
    assert resolve_deepseek_api_key(tmp_path) == "sk-env-key-value-1234"
    assert os.environ[DEEPSEEK_API_KEY_ENV] == "sk-env-key-value-1234"
    assert status.privacy_gate_ready is False
    assert status.scoring_ready is False
    assert overridden.scoring_ready is False


def test_scoring_ready_requires_pinned_privacy_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv(DEEPSEEK_API_KEY_ENV, raising=False)
    monkeypatch.delenv("COURSE_INSIGHT_M7_PRIVACY_MODEL_SHA256", raising=False)
    monkeypatch.delenv("COURSE_INSIGHT_M7_PRIVACY_MANIFEST_SHA256", raising=False)
    status = save_teacher_deepseek_settings(
        tmp_path,
        api_key="sk-test-deepseek-secret-key",
        model_name="deepseek-v4-flash",
        thinking_enabled=False,
    )
    assert status.configured is True
    assert status.privacy_gate_ready is False
    assert status.scoring_ready is False
    assert status.privacy_gate_reason == "missing_pinned_checksums"


def test_test_environment_does_not_wire_deepseek_even_with_api_key(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from course_insight.application.factory import build_application
    from course_insight.infrastructure.config import (
        DatabaseSettings,
        LoggingSettings,
        PlatformSettings,
    )
    from course_insight.modules.m7_local_model.adapter import (
        PlaceholderRubricAdapter,
    )

    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "sk-must-not-leave-test-environment")
    runtime_dir = tmp_path / "runtime"
    settings = PlatformSettings(
        environment="test",
        runtime_dir=runtime_dir,
        config_dir=tmp_path / "config",
        database=DatabaseSettings(
            backend="sqlite",
            sqlite_path=runtime_dir / "course_insight.sqlite3",
        ),
        logging=LoggingSettings(directory=runtime_dir / "logs"),
    )
    container = build_application(settings)
    try:
        adapter = container.m7_service._local_model_adapter
        assert isinstance(adapter, PlaceholderRubricAdapter)
    finally:
        container.close()


def test_development_wires_deepseek_with_local_presidio_when_artifact_is_absent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from course_insight.application.factory import build_application
    from course_insight.infrastructure import deepseek_secrets
    from course_insight.infrastructure.config import (
        DatabaseSettings,
        LoggingSettings,
        PlatformSettings,
    )
    from course_insight.modules.m7_local_model import privacy_reviewer, runtime

    class _AllowingDevelopmentReviewer:
        reviewer_id = "development-presidio-fixture"

    reviewer = _AllowingDevelopmentReviewer()
    adapter = object()
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "sk-development-scoring-key")
    monkeypatch.setattr(
        deepseek_secrets,
        "inspect_m7_privacy_artifacts",
        lambda runtime_dir: (False, "missing_pinned_checksums"),
    )
    monkeypatch.setattr(
        privacy_reviewer,
        "build_presidio_spacy_reviewer",
        lambda **kwargs: reviewer,
    )
    monkeypatch.setattr(
        runtime,
        "build_deepseek_m7_adapter",
        lambda **kwargs: adapter,
    )
    runtime_dir = tmp_path / "runtime"
    settings = PlatformSettings(
        environment="development",
        runtime_dir=runtime_dir,
        config_dir=tmp_path / "config",
        database=DatabaseSettings(
            backend="sqlite",
            sqlite_path=runtime_dir / "course_insight.sqlite3",
        ),
        logging=LoggingSettings(directory=runtime_dir / "logs"),
    )

    container = build_application(settings)
    try:
        assert container.m7_service._local_model_adapter is adapter
    finally:
        container.close()
