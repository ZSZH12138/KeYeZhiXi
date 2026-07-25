"""Immutable internal platform settings."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    PostgresDsn,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from course_insight.infrastructure.config.errors import ConfigurationError


EnvironmentName = Literal["development", "test", "production"]
DatabaseBackend = Literal["sqlite", "postgresql"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogMode = Literal["rotating_file", "stdout"]
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_POSTGRES_DSN_ADAPTER = TypeAdapter(PostgresDsn)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )


class DatabaseSettings(_FrozenModel):
    backend: DatabaseBackend = "sqlite"
    sqlite_path: Path
    url_env: str = "DATABASE_URL"
    url: SecretStr | None = None
    pool_min_size: int = Field(default=1, ge=1, le=100)
    pool_max_size: int = Field(default=10, ge=1, le=100)
    connect_timeout_seconds: Annotated[FiniteFloat, Field(gt=0, le=300)] = 10.0

    @field_validator("url_env")
    @classmethod
    def _validate_url_env(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("invalid environment variable name")
        return value

    @model_validator(mode="after")
    def _validate_database(self) -> Self:
        if self.pool_min_size > self.pool_max_size:
            raise ValueError("pool_min_size exceeds pool_max_size")
        missing_url = (
            self.url is None
            or not self.url.get_secret_value().strip()
        )
        if self.backend == "postgresql" and missing_url:
            raise ConfigurationError(
                code="MISSING_REQUIRED_SETTING",
                fields=("database.url",),
                reason="required",
            )
        if self.backend == "postgresql" and self.url is not None:
            try:
                _POSTGRES_DSN_ADAPTER.validate_python(
                    self.url.get_secret_value()
                )
            except ValidationError:
                raise ConfigurationError(
                    code="INVALID_DATABASE_URL",
                    fields=("database.url",),
                    reason="invalid_url",
                ) from None
        return self


class LoggingSettings(_FrozenModel):
    level: LogLevel = "INFO"
    mode: LogMode = "rotating_file"
    directory: Path
    filename: str = Field(default="app.log", min_length=1, max_length=128)
    rotation_max_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1024,
        le=1024 * 1024 * 1024,
    )
    backup_count: int = Field(default=5, ge=1, le=100)

    @field_validator("filename")
    @classmethod
    def _validate_filename(cls, value: str) -> str:
        if Path(value).name != value or value in {".", ".."}:
            raise ValueError("filename must be a basename")
        return value


class OutboxSettings(_FrozenModel):
    batch_size: int = Field(default=100, ge=1, le=10_000)
    poll_interval_seconds: Annotated[FiniteFloat, Field(gt=0, le=3600)] = 1.0
    lease_seconds: Annotated[FiniteFloat, Field(gt=0, le=86_400)] = 30.0
    max_retries: int = Field(default=8, ge=1, le=1000)
    retry_base_seconds: Annotated[FiniteFloat, Field(gt=0, le=86_400)] = 1.0
    retry_max_seconds: Annotated[FiniteFloat, Field(gt=0, le=604_800)] = 300.0
    retry_jitter_ratio: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.2
    heartbeat_interval_seconds: Annotated[
        FiniteFloat,
        Field(gt=0, le=86_400),
    ] = 10.0

    @model_validator(mode="after")
    def _validate_worker_timing(self) -> Self:
        if self.retry_base_seconds > self.retry_max_seconds:
            raise ConfigurationError(
                code="INVALID_OUTBOX_TIMING",
                fields=(
                    "outbox.retry_base_seconds",
                    "outbox.retry_max_seconds",
                ),
                reason="retry_range",
            )
        if self.heartbeat_interval_seconds >= self.lease_seconds:
            raise ConfigurationError(
                code="INVALID_OUTBOX_TIMING",
                fields=(
                    "outbox.heartbeat_interval_seconds",
                    "outbox.lease_seconds",
                ),
                reason="heartbeat_range",
            )
        return self


class WebSettings(_FrozenModel):
    secret_key_env: str = "DJANGO_SECRET_KEY"
    secret_key: SecretStr | None = None
    allowed_hosts_env: str = "DJANGO_ALLOWED_HOSTS"
    csrf_trusted_origins_env: str = "DJANGO_CSRF_TRUSTED_ORIGINS"
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    csrf_trusted_origins: tuple[str, ...] = ()
    secure_cookie: bool = False
    session_timeout_seconds: int = Field(default=3600, ge=60, le=2_592_000)

    @field_validator(
        "secret_key_env",
        "allowed_hosts_env",
        "csrf_trusted_origins_env",
    )
    @classmethod
    def _validate_env_name(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("invalid environment variable name")
        return value

    @field_validator("allowed_hosts", "csrf_trusted_origins")
    @classmethod
    def _validate_non_blank_values(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(not item or item != item.strip() for item in value):
            raise ValueError("blank or padded entry")
        if len(value) != len(set(value)):
            raise ValueError("duplicate entry")
        return value


class SecuritySettings(_FrozenModel):
    session_cookie_httponly: bool = True
    session_cookie_samesite: Literal["Lax", "Strict"] = "Lax"
    max_request_body_bytes: int = Field(
        default=1024 * 1024,
        ge=1024,
        le=100 * 1024 * 1024,
    )
    login_failure_limit: int = Field(default=5, ge=1, le=100)
    login_failure_window_seconds: int = Field(default=300, ge=1, le=86_400)

    @model_validator(mode="after")
    def _validate_security(self) -> Self:
        if not self.session_cookie_httponly:
            raise ValueError("session cookies must remain HttpOnly")
        return self


class PlatformSettings(BaseSettings):
    """Complete immutable settings assembled by the explicit loader."""

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        env_prefix="COURSE_INSIGHT_",
        env_nested_delimiter="__",
    )

    environment: EnvironmentName = "development"
    runtime_dir: Path
    config_dir: Path
    database: DatabaseSettings
    logging: LoggingSettings
    outbox: OutboxSettings = Field(default_factory=OutboxSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)

    @model_validator(mode="after")
    def _validate_production_security(self) -> Self:
        if self.environment != "production":
            return self

        invalid_fields: list[str] = []
        if self.database.backend != "postgresql":
            invalid_fields.append("database.backend")
        if (
            self.database.url is None
            or not self.database.url.get_secret_value().strip()
        ):
            invalid_fields.append("database.url")
        secret_value = (
            ""
            if self.web.secret_key is None
            else self.web.secret_key.get_secret_value()
        )
        if (
            len(secret_value) < 50
            or len(set(secret_value)) < 5
            or secret_value.startswith("django-insecure-")
        ):
            invalid_fields.append("web.secret_key")
        if not self.web.allowed_hosts or "*" in self.web.allowed_hosts:
            invalid_fields.append("web.allowed_hosts")
        if not self.web.secure_cookie:
            invalid_fields.append("web.secure_cookie")
        if self.logging.mode != "stdout":
            invalid_fields.append("logging.mode")
        if invalid_fields:
            raise ConfigurationError(
                code="INSECURE_PRODUCTION_CONFIGURATION",
                fields=invalid_fields,
                reason="production_security",
            )
        return self
