from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings

from course_insight.infrastructure.config import ConfigurationError
from course_insight.infrastructure.config import IntentSettings
from course_insight.infrastructure.config import PlatformSettings
from course_insight.infrastructure.config import load_platform_settings


MODEL_SHA256 = "a" * 64


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_m6_policy_defaults_are_strict_rules_without_learned_io(
    tmp_path: Path,
) -> None:
    settings = load_platform_settings(
        project_root=tmp_path,
        app_json_path=None,
        dotenv_path=None,
        environment={},
    )

    assert settings.m6_policy.mode == "rules"
    assert settings.m6_policy.policy_id is None
    assert settings.m6_policy.evaluation_dataset_identity is None
    assert settings.m6_policy.rollout_percentage == 0.0
    assert settings.m6_policy.exploration_rate == 0.0
    assert settings.m6_policy.maximum_exploration_rate == 0.05
    assert settings.m6_policy.global_kill_switch is False
    assert settings.m6_policy.allowed_course_ids == ()
    assert settings.m6_policy.allowed_class_ids == ()
    assert settings.m6_policy.runtime_directory == (
        tmp_path / "runtime" / "m6_policy"
    ).resolve()


def test_retrieval_policy_is_loaded_from_governed_environment_settings(
    tmp_path: Path,
) -> None:
    settings = load_platform_settings(
        project_root=tmp_path,
        app_json_path=None,
        dotenv_path=None,
        environment={
            "COURSE_INSIGHT_RETRIEVAL__POLICY_ID": "production-hybrid-v2",
            "COURSE_INSIGHT_RETRIEVAL__STRATEGY": "hybrid",
            "COURSE_INSIGHT_RETRIEVAL__TOP_K": "5",
            "COURSE_INSIGHT_RETRIEVAL__LEXICAL_WEIGHT": "0.4",
            "COURSE_INSIGHT_RETRIEVAL__VECTOR_WEIGHT": "0.6",
        },
    )

    assert settings.retrieval.policy_id == "production-hybrid-v2"
    assert settings.retrieval.strategy == "hybrid"
    assert settings.retrieval.top_k == 5
    assert settings.retrieval.lexical_weight == 0.4
    assert settings.retrieval.vector_weight == 0.6


def test_live_database_guard_variables_are_not_platform_settings(
    tmp_path: Path,
) -> None:
    settings = load_platform_settings(
        project_root=tmp_path,
        app_json_path=None,
        dotenv_path=None,
        environment={
            "COURSE_INSIGHT_DATABASE__BACKEND": "postgresql",
            "COURSE_INSIGHT_DATABASE__URL_ENV": "COURSE_INSIGHT_TEST_DATABASE_URL",
            "COURSE_INSIGHT_TEST_DATABASE_URL": (
                "postgresql://postgres:postgres@localhost:5432/course_insight_test_ci"
            ),
            "COURSE_INSIGHT_TEST_DATABASE_NAME": "course_insight_test_ci",
        },
    )

    assert settings.database.backend == "postgresql"
    assert settings.database.url is not None
    assert settings.database.url.get_secret_value().endswith(
        "/course_insight_test_ci"
    )


def test_hybrid_retrieval_weights_must_sum_to_one(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=None,
            dotenv_path=None,
            environment={
                "COURSE_INSIGHT_RETRIEVAL__STRATEGY": "hybrid",
                "COURSE_INSIGHT_RETRIEVAL__LEXICAL_WEIGHT": "0.3",
                "COURSE_INSIGHT_RETRIEVAL__VECTOR_WEIGHT": "0.3",
            },
        )

    assert captured.value.code == "INVALID_RETRIEVAL_POLICY"


def test_embedding_dimension_matches_pgvector_schema_limit(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=None,
            dotenv_path=None,
            environment={},
            overrides={
                "embedding": {
                    "backend": "openai_compatible",
                    "endpoint": "https://embeddings.example",
                    "model_name": "embedding-model",
                    "model_version": "v1",
                    "dimension": 16_001,
                }
            },
        )

    assert "embedding.dimension" in captured.value.fields


def test_production_vector_strategy_requires_embedding_backend(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=None,
            dotenv_path=None,
            environment={},
            overrides={
                "environment": "production",
                "retrieval": {
                    "strategy": "vector",
                    "lexical_weight": 0.0,
                    "vector_weight": 1.0,
                },
            },
        )

    assert captured.value.code == "INSECURE_PRODUCTION_CONFIGURATION"
    assert "embedding.backend" in captured.value.fields


@pytest.mark.parametrize(
    "m6_policy",
    [
        {"mode": "shadow"},
        {"mode": "active", "policy_id": "policy-v1"},
        {
            "mode": "active",
            "policy_id": "policy-v1",
            "evaluation_dataset_identity": "dataset-v1",
            "maximum_exploration_rate": 0.051,
        },
        {
            "mode": "shadow",
            "policy_id": "policy-v1",
            "exploration_rate": 0.02,
            "maximum_exploration_rate": 0.01,
        },
        {
            "mode": "shadow",
            "policy_id": "policy-v1",
            "rollout_percentage": 1.01,
        },
        {
            "mode": "shadow",
            "policy_id": "policy-v1",
            "allowed_course_ids": ["course-1", "course-1"],
        },
    ],
)
def test_m6_policy_rejects_incomplete_or_unsafe_configuration(
    tmp_path: Path,
    m6_policy: dict[str, object],
) -> None:
    with pytest.raises(ConfigurationError):
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=None,
            dotenv_path=None,
            environment={},
            overrides={"m6_policy": m6_policy},
        )


def test_m6_policy_runtime_directory_must_stay_below_runtime_root(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=None,
            dotenv_path=None,
            environment={},
            overrides={
                "m6_policy": {
                    "runtime_directory": "../outside",
                }
            },
        )

    assert captured.value.fields == ("m6_policy.runtime_directory",)


def test_active_policy_may_be_configured_fail_closed_with_empty_rollout_and_scopes(
    tmp_path: Path,
) -> None:
    settings = load_platform_settings(
        project_root=tmp_path,
        app_json_path=None,
        dotenv_path=None,
        environment={},
        overrides={
            "m6_policy": {
                "mode": "active",
                "policy_id": "policy-v1",
                "evaluation_dataset_identity": "dataset-v1",
            }
        },
    )

    assert settings.m6_policy.mode == "active"
    assert settings.m6_policy.rollout_percentage == 0.0
    assert settings.m6_policy.allowed_course_ids == ()
    assert settings.m6_policy.allowed_class_ids == ()


def test_intent_defaults_are_rules_only(tmp_path: Path) -> None:
    settings = load_platform_settings(
        project_root=tmp_path,
        app_json_path=None,
        dotenv_path=None,
        environment={},
    )

    assert settings.intent.mode == "rules"
    assert settings.intent.backend == "none"
    assert settings.intent.model_ref is None
    assert settings.intent.model_id is None
    assert settings.intent.model_version is None
    assert settings.intent.model_sha256 is None
    assert settings.intent.min_confidence == 0.70
    assert settings.intent.min_margin == 0.10
    assert settings.intent.policy_version == "m4-intent-policy-v1"
    assert settings.intent.fallback_to_rules is True
    assert settings.intent.fail_closed is True


def test_postgres_live_test_variables_are_not_platform_configuration(
    tmp_path: Path,
) -> None:
    settings = load_platform_settings(
        project_root=tmp_path,
        app_json_path=None,
        dotenv_path=None,
        environment={
            "COURSE_INSIGHT_TEST_DATABASE_URL": (
                "postgresql://tester:secret@localhost/course_insight_test"
            ),
            "COURSE_INSIGHT_TEST_DATABASE_NAME": "course_insight_test",
        },
    )

    assert settings.database.backend == "sqlite"


@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": "active", "backend": "none"},
        {"mode": "shadow", "backend": "sklearn", "model_ref": None},
        {"mode": "rules", "backend": "sklearn", "model_ref": "model"},
        {"mode": "rules", "backend": "none", "model_ref": "model"},
        {
            "mode": "active",
            "backend": "sklearn",
            "model_ref": "models/intent",
        },
        {
            "mode": "rules",
            "model_sha256": MODEL_SHA256,
        },
        {"min_confidence": float("nan")},
        {"min_margin": 1.01},
        {"policy_version": "   "},
    ],
)
def test_invalid_intent_settings_fail_closed(
    overrides: dict[str, object],
) -> None:
    with pytest.raises((ValidationError, ConfigurationError)):
        IntentSettings(**overrides)


@pytest.mark.parametrize("threshold", [0.0, 1.0])
def test_intent_threshold_boundaries_are_valid(threshold: float) -> None:
    settings = IntentSettings(
        min_confidence=threshold,
        min_margin=threshold,
        policy_version="  governed-policy-v2  ",
    )

    assert settings.min_confidence == threshold
    assert settings.min_margin == threshold
    assert settings.policy_version == "governed-policy-v2"


def test_intent_nested_environment_overrides_and_relative_model_path(
    tmp_path: Path,
) -> None:
    settings = load_platform_settings(
        project_root=tmp_path,
        app_json_path=None,
        dotenv_path=None,
        environment={
            "COURSE_INSIGHT_CONFIG_DIR": "governance",
            "COURSE_INSIGHT_RUNTIME_DIR": "runtime",
            "COURSE_INSIGHT_INTENT__MODE": "active",
            "COURSE_INSIGHT_INTENT__BACKEND": "sklearn",
            "COURSE_INSIGHT_INTENT__MODEL_REF": "models/intent-model-v2",
            "COURSE_INSIGHT_INTENT__MODEL_ID": "m4-intent",
            "COURSE_INSIGHT_INTENT__MODEL_VERSION": "2.0.0",
            "COURSE_INSIGHT_INTENT__MODEL_SHA256": MODEL_SHA256,
            "COURSE_INSIGHT_INTENT__MIN_CONFIDENCE": "0.83",
            "COURSE_INSIGHT_INTENT__MIN_MARGIN": "0.24",
            "COURSE_INSIGHT_INTENT__POLICY_VERSION": " policy-v2 ",
            "COURSE_INSIGHT_INTENT__FALLBACK_TO_RULES": "false",
            "COURSE_INSIGHT_INTENT__FAIL_CLOSED": "true",
        },
    )

    assert settings.intent.mode == "active"
    assert settings.intent.backend == "sklearn"
    assert settings.intent.model_ref == Path("models/intent-model-v2")
    assert settings.intent.model_id == "m4-intent"
    assert settings.intent.model_version == "2.0.0"
    assert settings.intent.model_sha256 == MODEL_SHA256
    assert settings.intent.min_confidence == 0.83
    assert settings.intent.min_margin == 0.24
    assert settings.intent.policy_version == "policy-v2"
    assert settings.intent.fallback_to_rules is False
    assert settings.intent.fail_closed is True


def test_intent_file_settings_keep_runtime_relative_model_reference(
    tmp_path: Path,
) -> None:
    app_json = tmp_path / "config" / "app.json"
    _write_json(
        app_json,
        {
            "config_dir": "deployment-config",
            "intent": {
                "mode": "shadow",
                "backend": "sklearn",
                "model_ref": "models/intent",
                "model_id": "m4-intent",
                "model_version": "1.0.0",
                "model_sha256": MODEL_SHA256,
            },
        },
    )

    settings = load_platform_settings(
        project_root=tmp_path,
        app_json_path=app_json,
        dotenv_path=None,
        environment={},
    )

    assert settings.intent.model_ref == Path("models/intent")
    assert (
        settings.runtime_dir / settings.intent.model_ref
    ).resolve().is_relative_to(settings.runtime_dir)


def test_relative_intent_model_path_cannot_escape_runtime_dir(
    tmp_path: Path,
) -> None:
    private_model_dir = tmp_path / "private-model"

    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=None,
            dotenv_path=None,
            environment={
                "COURSE_INSIGHT_INTENT__MODE": "active",
                "COURSE_INSIGHT_INTENT__BACKEND": "sklearn",
                "COURSE_INSIGHT_INTENT__MODEL_REF": "../private-model",
                "COURSE_INSIGHT_INTENT__MODEL_ID": "m4-intent",
                "COURSE_INSIGHT_INTENT__MODEL_VERSION": "1.0.0",
                "COURSE_INSIGHT_INTENT__MODEL_SHA256": MODEL_SHA256,
            },
        )

    assert captured.value.fields == ("intent.model_ref",)
    assert str(private_model_dir) not in str(captured.value)


@pytest.mark.parametrize(
    "model_ref",
    [
        "https://example.invalid/model",
        "file:///private/model",
        "file:/private/model",
        "C:/private/model",
        "C:private-model",
        "\\rooted-model",
        "\\\\server\\private\\model",
        "models/$private",
    ],
)
def test_intent_model_reference_rejects_url_and_absolute_paths(
    model_ref: str,
) -> None:
    with pytest.raises(ValidationError):
        IntentSettings(
            mode="active",
            backend="sklearn",
            model_ref=model_ref,
            model_id="m4-intent",
            model_version="1.0.0",
            model_sha256=MODEL_SHA256,
        )


def test_intent_model_reference_rejects_symlink_escape(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    outside = tmp_path / "outside"
    runtime.mkdir()
    outside.mkdir()
    link = runtime / "linked-model"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    with pytest.raises(ValidationError):
        PlatformSettings(
            environment="test",
            runtime_dir=runtime,
            config_dir=tmp_path / "config",
            database={
                "backend": "sqlite",
                "sqlite_path": runtime / "course_insight.db",
            },
            logging={"directory": runtime / "logs"},
            intent={
                "mode": "active",
                "backend": "sklearn",
                "model_ref": "linked-model",
                "model_id": "m4-intent",
                "model_version": "1.0.0",
                "model_sha256": MODEL_SHA256,
            },
        )


def test_configuration_sources_use_deterministic_field_level_precedence(
    tmp_path: Path,
) -> None:
    app_json = tmp_path / "config" / "app.json"
    dotenv = tmp_path / ".env.test"
    _write_json(
        app_json,
        {
            "logging": {"level": "INFO", "backup_count": 7},
            "outbox": {"batch_size": 11},
        },
    )
    dotenv.write_text(
        "\n".join(
            [
                "COURSE_INSIGHT_LOGGING__LEVEL=WARNING",
                "COURSE_INSIGHT_OUTBOX__BATCH_SIZE=22",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_platform_settings(
        project_root=tmp_path,
        app_json_path=app_json,
        dotenv_path=dotenv,
        environment={
            "COURSE_INSIGHT_LOGGING__LEVEL": "ERROR",
            "COURSE_INSIGHT_OUTBOX__BATCH_SIZE": "33",
        },
        overrides={"logging": {"level": "CRITICAL"}},
    )

    assert settings.logging.level == "CRITICAL"
    assert settings.logging.backup_count == 7
    assert settings.outbox.batch_size == 33


def test_app_json_rejects_embedded_database_secret_without_echoing_it(
    tmp_path: Path,
) -> None:
    app_json = tmp_path / "config" / "app.json"
    secret_dsn = "postgresql://private-user:private-password@db/private"
    _write_json(app_json, {"database": {"url": secret_dsn}})

    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=app_json,
            dotenv_path=None,
            environment={},
        )

    rendered = str(captured.value)
    assert "database.url" in rendered
    assert secret_dsn not in rendered
    assert "private-password" not in rendered


def test_explicitly_disabled_dotenv_never_reads_project_or_developer_file(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "COURSE_INSIGHT_OUTBOX__BATCH_SIZE=999\n",
        encoding="utf-8",
    )

    settings = load_platform_settings(
        project_root=tmp_path,
        app_json_path=None,
        dotenv_path=None,
        environment={},
    )

    assert settings.outbox.batch_size == 100


def test_omitted_dotenv_path_does_not_read_ambient_project_file(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(
        "COURSE_INSIGHT_OUTBOX__BATCH_SIZE=998\n",
        encoding="utf-8",
    )

    settings = load_platform_settings(
        project_root=tmp_path,
        app_json_path=None,
        environment={},
    )

    assert settings.outbox.batch_size == 100


@pytest.mark.parametrize(
    ("field", "path_argument"),
    [
        ("app_json", "config/missing.json"),
        ("dotenv", ".env.missing"),
    ],
)
def test_explicit_missing_source_path_fails_closed(
    tmp_path: Path,
    field: str,
    path_argument: str,
) -> None:
    arguments: dict[str, object] = {
        "project_root": tmp_path,
        "environment": {},
        "app_json_path": None,
        "dotenv_path": None,
    }
    arguments[f"{field}_path"] = path_argument

    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(**arguments)

    assert captured.value.fields == (field,)


def test_dynamic_secret_environment_names_are_resolved_by_source(
    tmp_path: Path,
) -> None:
    app_json = tmp_path / "config" / "app.json"
    _write_json(
        app_json,
        {
            "database": {
                "backend": "postgresql",
                "url_env": "CUSTOM_DATABASE_URL",
            },
            "web": {
                "secret_key_env": "CUSTOM_DJANGO_SECRET",
                "allowed_hosts_env": "CUSTOM_ALLOWED_HOSTS",
                "csrf_trusted_origins_env": "CUSTOM_CSRF_ORIGINS",
            },
        },
    )
    database_url = "postgresql://opaque-user:opaque-pass@db.example/app"
    django_secret = "safe-production-secret-" + ("a7" * 20)

    settings = load_platform_settings(
        project_root=tmp_path,
        app_json_path=app_json,
        dotenv_path=None,
        environment={
            "CUSTOM_DATABASE_URL": database_url,
            "CUSTOM_DJANGO_SECRET": django_secret,
            "CUSTOM_ALLOWED_HOSTS": "app.example,admin.example",
            "CUSTOM_CSRF_ORIGINS": "https://app.example,https://admin.example",
        },
    )

    assert settings.database.url is not None
    assert settings.database.url.get_secret_value() == database_url
    assert settings.web.secret_key is not None
    assert settings.web.secret_key.get_secret_value() == django_secret
    assert settings.web.allowed_hosts == ("app.example", "admin.example")
    assert settings.web.csrf_trusted_origins == (
        "https://app.example",
        "https://admin.example",
    )
    assert database_url not in repr(settings)
    assert django_secret not in repr(settings)


def test_environment_direct_field_beats_same_source_indirect_name(
    tmp_path: Path,
) -> None:
    settings = load_platform_settings(
        project_root=tmp_path,
        app_json_path=None,
        dotenv_path=None,
        environment={
            "DJANGO_ALLOWED_HOSTS": "indirect.example",
            "COURSE_INSIGHT_WEB__ALLOWED_HOSTS": '["direct.example"]',
        },
    )

    assert settings.web.allowed_hosts == ("direct.example",)


@pytest.mark.parametrize(
    ("field", "payload"),
    [
        ("outbox.poll_interval_seconds", {"outbox": {"poll_interval_seconds": "NaN"}}),
        ("outbox.lease_seconds", {"outbox": {"lease_seconds": "Infinity"}}),
        (
            "outbox.retry_jitter_ratio",
            {"outbox": {"retry_jitter_ratio": "NaN"}},
        ),
        (
            "database.connect_timeout_seconds",
            {"database": {"connect_timeout_seconds": "-Infinity"}},
        ),
        ("outbox.batch_size", {"outbox": {"batch_size": "not-an-integer"}}),
    ],
)
def test_invalid_numeric_configuration_fails_safely(
    tmp_path: Path,
    field: str,
    payload: dict[str, object],
) -> None:
    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=None,
            dotenv_path=None,
            environment={},
            overrides=payload,
        )

    assert field in captured.value.fields
    assert "NaN" not in str(captured.value)
    assert "Infinity" not in str(captured.value)


@pytest.mark.parametrize(
    ("field", "overrides"),
    [
        ("runtime_dir", {"runtime_dir": "../outside"}),
        ("config_dir", {"config_dir": "../outside"}),
        (
            "database.sqlite_path",
            {"database": {"sqlite_path": "../outside.db"}},
        ),
        (
            "logging.directory",
            {"logging": {"directory": "../outside-logs"}},
        ),
    ],
)
def test_runtime_paths_cannot_escape_their_authorized_roots(
    tmp_path: Path,
    field: str,
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=None,
            dotenv_path=None,
            environment={},
            overrides=overrides,
        )

    assert field in captured.value.fields
    assert str(tmp_path.parent) not in str(captured.value)


def test_app_json_path_cannot_escape_config_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    _write_json(outside, {})

    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=outside,
            dotenv_path=None,
            environment={},
        )

    assert captured.value.fields == ("app_json",)


@pytest.mark.parametrize(
    "payload",
    [
        {"unexpected": "private-value"},
        {"database": {"password": "private-password"}},
        {"web": {"secret_key": "private-secret"}},
        {"database": {"host": "postgresql://user:pass@db/name"}},
    ],
)
def test_app_json_rejects_unknown_or_secret_valued_content_without_echo(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    app_json = tmp_path / "config" / "app.json"
    _write_json(app_json, payload)

    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=app_json,
            dotenv_path=None,
            environment={},
        )

    rendered = str(captured.value)
    assert "private-value" not in rendered
    assert "private-password" not in rendered
    assert "private-secret" not in rendered
    assert "user:pass" not in rendered


def test_production_configuration_fails_closed_with_safe_field_names(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=None,
            dotenv_path=None,
            environment={},
            overrides={
                "environment": "production",
                "web": {"allowed_hosts": []},
            },
        )

    assert captured.value.code == "INSECURE_PRODUCTION_CONFIGURATION"
    assert set(captured.value.fields) == {
        "database.backend",
        "database.url",
        "logging.mode",
        "web.allowed_hosts",
        "web.secret_key",
        "web.secure_cookie",
    }


def test_valid_production_settings_are_grouped_frozen_and_safe(
    tmp_path: Path,
) -> None:
    database_url = "postgresql://opaque-user:opaque-pass@db.example/app"
    settings = load_platform_settings(
        project_root=tmp_path,
        app_json_path=None,
        dotenv_path=None,
        environment={
            "DATABASE_URL": database_url,
            "DJANGO_SECRET_KEY": "safe-production-secret-" + ("a7" * 20),
            "DJANGO_ALLOWED_HOSTS": "app.example",
        },
        overrides={
            "environment": "production",
            "database": {"backend": "postgresql"},
            "logging": {"mode": "stdout"},
            "web": {"secure_cookie": True},
        },
    )

    assert isinstance(settings, BaseSettings)
    assert isinstance(settings, PlatformSettings)
    assert settings.environment == "production"
    assert settings.database.pool_min_size == 1
    assert settings.database.pool_max_size == 10
    assert settings.logging.rotation_max_bytes == 10 * 1024 * 1024
    assert settings.outbox.max_retries == 8
    assert settings.outbox.retry_base_seconds == 1.0
    assert settings.outbox.retry_max_seconds == 300.0
    assert settings.outbox.retry_jitter_ratio == 0.2
    assert settings.outbox.heartbeat_interval_seconds == 10.0
    assert settings.security.session_cookie_httponly is True
    with pytest.raises(Exception):
        settings.outbox.batch_size = 1


@pytest.mark.parametrize(
    ("field", "outbox"),
    [
        (
            "outbox.retry_base_seconds",
            {"retry_base_seconds": 20, "retry_max_seconds": 10},
        ),
        (
            "outbox.heartbeat_interval_seconds",
            {"heartbeat_interval_seconds": 30, "lease_seconds": 30},
        ),
    ],
)
def test_outbox_worker_timing_relationships_fail_closed(
    tmp_path: Path,
    field: str,
    outbox: dict[str, object],
) -> None:
    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=None,
            dotenv_path=None,
            environment={},
            overrides={"outbox": outbox},
        )

    assert field in captured.value.fields


@pytest.mark.parametrize(
    ("field", "overrides"),
    [
        ("database", {"database": "not-a-group"}),
        ("web", {"web": ["not-a-group"]}),
        ("runtime_dir", {"runtime_dir": 42}),
    ],
)
def test_malformed_structure_is_wrapped_as_safe_configuration_error(
    tmp_path: Path,
    field: str,
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=None,
            dotenv_path=None,
            environment={},
            overrides=overrides,
        )

    assert field in captured.value.fields
    assert "not-a-group" not in str(captured.value)


def test_duplicate_app_json_keys_are_rejected(tmp_path: Path) -> None:
    app_json = tmp_path / "config" / "app.json"
    app_json.parent.mkdir(parents=True)
    app_json.write_text(
        '{"outbox":{"batch_size":10,"batch_size":20}}',
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="duplicate_key"):
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=app_json,
            dotenv_path=None,
            environment={},
        )


def test_secret_key_name_variants_in_app_json_are_rejected(
    tmp_path: Path,
) -> None:
    app_json = tmp_path / "config" / "app.json"
    _write_json(
        app_json,
        {"database": {"db_password": "private-password"}},
    )

    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=app_json,
            dotenv_path=None,
            environment={},
        )

    assert captured.value.code == "SECRET_IN_APP_JSON"
    assert "private-password" not in str(captured.value)


def test_repository_app_example_is_valid_without_real_environment() -> None:
    project_root = Path(__file__).resolve().parents[2]

    settings = load_platform_settings(
        project_root=project_root,
        app_json_path=project_root / "config" / "app.example.json",
        dotenv_path=None,
        environment={},
    )

    assert settings.environment == "development"
    assert settings.database.backend == "sqlite"
    assert settings.database.sqlite_path == (
        project_root / "runtime" / "course_insight.db"
    ).resolve()


def test_production_rejects_sqlite_even_when_database_url_is_present(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=None,
            dotenv_path=None,
            environment={
                "DATABASE_URL": "postgresql://user:pass@db.example/app",
                "DJANGO_SECRET_KEY": "safe-production-secret-" + ("a7" * 20),
                "DJANGO_ALLOWED_HOSTS": "app.example",
            },
            overrides={
                "environment": "production",
                "logging": {"mode": "stdout"},
                "web": {"secure_cookie": True},
            },
        )

    assert "database.backend" in captured.value.fields


def test_postgresql_empty_database_url_is_treated_as_missing(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=None,
            dotenv_path=None,
            environment={"DATABASE_URL": ""},
            overrides={"database": {"backend": "postgresql"}},
        )

    assert "database.url" in captured.value.fields


def test_relative_dotenv_path_is_resolved_from_project_root(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env.test").write_text(
        "COURSE_INSIGHT_OUTBOX__BATCH_SIZE=321\n",
        encoding="utf-8",
    )

    settings = load_platform_settings(
        project_root=tmp_path,
        app_json_path=None,
        dotenv_path=".env.test",
        environment={},
    )

    assert settings.outbox.batch_size == 321


def test_postgresql_backend_rejects_non_postgresql_url(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=None,
            dotenv_path=None,
            environment={"DATABASE_URL": "sqlite:///unsafe.db"},
            overrides={"database": {"backend": "postgresql"}},
        )

    assert "database.url" in captured.value.fields
    assert "sqlite:///unsafe.db" not in str(captured.value)
    assert captured.value.__cause__ is None


def test_production_rejects_low_entropy_django_secret(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError) as captured:
        load_platform_settings(
            project_root=tmp_path,
            app_json_path=None,
            dotenv_path=None,
            environment={
                "DATABASE_URL": "postgresql://user:pass@db.example/app",
                "DJANGO_SECRET_KEY": "x" * 60,
                "DJANGO_ALLOWED_HOSTS": "app.example",
            },
            overrides={
                "environment": "production",
                "database": {"backend": "postgresql"},
                "logging": {"mode": "stdout"},
                "web": {"secure_cookie": True},
            },
        )

    assert "web.secret_key" in captured.value.fields
    assert "x" * 60 not in str(captured.value)
