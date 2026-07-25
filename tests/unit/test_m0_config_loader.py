from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic_settings import BaseSettings

from course_insight.infrastructure.config import ConfigurationError
from course_insight.infrastructure.config import PlatformSettings
from course_insight.infrastructure.config import load_platform_settings


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


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
