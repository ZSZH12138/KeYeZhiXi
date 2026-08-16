"""Immutable internal platform settings."""

from __future__ import annotations

import math
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
M6PolicyMode = Literal["rules", "shadow", "active"]
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SAFE_INTENT_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]*$")
_SAFE_MODEL_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
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


class EmbeddingSettings(_FrozenModel):
    """Portable OpenAI-compatible embedding deployment settings."""

    backend: Literal["disabled", "openai_compatible"] = "disabled"
    endpoint: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    model_name: str | None = None
    model_version: str | None = None
    dimension: int | None = Field(default=None, ge=1, le=16_000)
    timeout_seconds: Annotated[FiniteFloat, Field(gt=0, le=300)] = 10.0
    max_retries: int = Field(default=2, ge=0, le=8)
    verify_tls: bool = True

    @field_validator("api_key_env")
    @classmethod
    def _validate_api_key_env(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("invalid environment variable name")
        return value

    @model_validator(mode="after")
    def _validate_embedding(self) -> Self:
        configured = self.backend == "openai_compatible"
        if configured and any(
            value is None or not str(value).strip()
            for value in (self.endpoint, self.model_name, self.model_version)
        ):
            raise ConfigurationError(
                code="MISSING_REQUIRED_SETTING",
                fields=("embedding.endpoint", "embedding.model_name", "embedding.model_version"),
                reason="required",
            )
        if configured and self.dimension is None:
            raise ConfigurationError(
                code="MISSING_REQUIRED_SETTING",
                fields=("embedding.dimension",),
                reason="required",
            )
        if not configured and any(
            value is not None
            for value in (self.endpoint, self.model_name, self.model_version, self.dimension)
        ):
            raise ConfigurationError(
                code="INVALID_EMBEDDING_CONFIGURATION",
                fields=("embedding.backend",),
                reason="disabled_with_values",
            )
        return self


class RetrievalSettings(_FrozenModel):
    """Governed application-level M2 retrieval policy selection."""

    policy_id: str = Field(default="application-lexical-v1", min_length=1)
    strategy: Literal["lexical", "vector", "hybrid"] = "lexical"
    top_k: int = Field(default=3, ge=1, le=1000)
    lexical_weight: Annotated[FiniteFloat, Field(ge=0.0, le=1.0)] = 1.0
    vector_weight: Annotated[FiniteFloat, Field(ge=0.0, le=1.0)] = 0.0
    rerank: bool = False

    @model_validator(mode="after")
    def _validate_retrieval_policy(self) -> Self:
        required_weights = {
            "lexical": (self.lexical_weight,),
            "vector": (self.vector_weight,),
            "hybrid": (self.lexical_weight, self.vector_weight),
        }[self.strategy]
        if any(weight <= 0.0 for weight in required_weights):
            raise ConfigurationError(
                code="INVALID_RETRIEVAL_POLICY",
                fields=("retrieval.lexical_weight", "retrieval.vector_weight"),
                reason="strategy_signal_missing",
            )
        if self.strategy == "hybrid" and not math.isclose(
            self.lexical_weight + self.vector_weight,
            1.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ConfigurationError(
                code="INVALID_RETRIEVAL_POLICY",
                fields=("retrieval.lexical_weight", "retrieval.vector_weight"),
                reason="hybrid_weights_must_sum_to_one",
            )
        return self


class OCRSettings(_FrozenModel):
    """Optional M1 scanned-document OCR runtime settings."""

    backend: Literal["disabled", "tesseract"] = "disabled"
    executable: str | None = None
    language: str = "chi_sim+eng"
    dpi: int = Field(default=200, ge=72, le=600)
    timeout_seconds: Annotated[FiniteFloat, Field(gt=0, le=120)] = 30.0
    max_output_bytes: int = Field(
        default=2 * 1024 * 1024,
        ge=1,
        le=16 * 1024 * 1024,
    )

    @field_validator("executable")
    @classmethod
    def _validate_executable(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            not value.strip()
            or len(value) > 512
            or any(character in value for character in ("\r", "\n"))
        ):
            raise ValueError("ocr executable is invalid")
        return value

    @field_validator("language")
    @classmethod
    def _validate_language(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.+:-]{1,64}", value):
            raise ValueError("ocr language is invalid")
        return value

    @model_validator(mode="after")
    def _validate_backend(self) -> Self:
        if self.backend == "disabled" and self.executable is not None:
            raise ConfigurationError(
                code="INVALID_OCR_CONFIGURATION",
                fields=("ocr.backend", "ocr.executable"),
                reason="disabled_with_executable",
            )
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


class M6PolicySettings(_FrozenModel):
    """Fail-closed private M6 policy runtime configuration."""

    mode: M6PolicyMode = "rules"
    policy_id: str | None = None
    evaluation_dataset_identity: str | None = None
    rollout_percentage: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.0
    exploration_rate: Annotated[FiniteFloat, Field(ge=0, le=0.05)] = 0.0
    maximum_exploration_rate: Annotated[
        FiniteFloat,
        Field(ge=0, le=0.05),
    ] = 0.05
    gate_policy_version: str = Field(
        default="m6-active-gate-v1",
        min_length=1,
        max_length=128,
    )
    global_kill_switch: bool = False
    allowed_course_ids: tuple[str, ...] = ()
    allowed_class_ids: tuple[str, ...] = ()
    minimum_support: int = Field(default=1, ge=1)
    maximum_uncertainty: Annotated[FiniteFloat, Field(ge=0)] = 0.0
    runtime_directory: Path = Path("m6_policy")

    @field_validator(
        "policy_id",
        "evaluation_dataset_identity",
        mode="before",
    )
    @classmethod
    def _validate_optional_identity(cls, value: object) -> object:
        if value is not None and (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
        ):
            raise ValueError("policy identity must be a non-padded string")
        return value

    @field_validator(
        "gate_policy_version",
    )
    @classmethod
    def _validate_gate_version(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("gate policy version must not be padded")
        return value

    @field_validator("allowed_course_ids", "allowed_class_ids")
    @classmethod
    def _validate_allowlist(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if (
            any(not item or item != item.strip() for item in value)
            or len(value) != len(set(value))
        ):
            raise ValueError("policy allowlist entries must be unique and non-blank")
        return value

    @model_validator(mode="after")
    def _validate_policy_mode(self) -> Self:
        if self.exploration_rate > self.maximum_exploration_rate:
            raise ValueError("exploration_rate exceeds maximum_exploration_rate")
        if self.mode != "rules" and self.policy_id is None:
            raise ValueError("learned policy modes require policy_id")
        if self.mode == "active" and self.evaluation_dataset_identity is None:
            raise ValueError(
                "active policy mode requires evaluation_dataset_identity"
            )
        return self


class IntentSettings(_FrozenModel):
    """Administrator-controlled M4 intent runtime configuration."""

    mode: Literal["rules", "shadow", "active"] = "rules"
    backend: Literal["none", "sklearn"] = "none"
    model_ref: Path | None = None
    model_id: str | None = None
    model_version: str | None = None
    model_sha256: str | None = None
    min_confidence: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.70
    min_margin: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.10
    policy_version: str = Field(
        default="m4-intent-policy-v1",
        min_length=1,
        max_length=128,
    )
    fallback_to_rules: bool = True
    fail_closed: bool = True

    @field_validator("policy_version", "model_id", "model_version", mode="before")
    @classmethod
    def _normalize_identifier(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("policy_version", "model_id")
    @classmethod
    def _validate_bounded_identifier(cls, value: str | None) -> str | None:
        if (
            value is not None
            and (
                len(value) > 128
                or _SAFE_INTENT_TOKEN.fullmatch(value) is None
            )
        ):
            raise ValueError("intent identifier must be a bounded safe token")
        return value

    @field_validator("model_version")
    @classmethod
    def _validate_model_version(cls, value: str | None) -> str | None:
        if (
            value is not None
            and (
                len(value) > 56
                or _SAFE_INTENT_TOKEN.fullmatch(value) is None
            )
        ):
            raise ValueError("model version must be a bounded safe token")
        return value

    @field_validator("model_sha256")
    @classmethod
    def _validate_model_sha256(cls, value: str | None) -> str | None:
        if value is not None and _SHA256.fullmatch(value) is None:
            raise ValueError("model sha256 must be lowercase hexadecimal")
        return value

    @field_validator("model_ref", mode="before")
    @classmethod
    def _validate_model_ref(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, (str, Path)):
            raise ValueError("model reference is invalid")
        raw = str(value).strip()
        if (
            not raw
            or len(raw) > 512
            or "://" in raw
            or raw.startswith("\\\\")
            or raw.startswith("//")
        ):
            raise ValueError("model reference must be runtime-relative")
        path = Path(raw)
        if (
            path.is_absolute()
            or bool(path.drive)
            or bool(path.anchor)
            or path == Path(".")
            or any(part in {".", ".."} for part in path.parts)
            or any(
                _SAFE_MODEL_SEGMENT.fullmatch(part) is None
                for part in path.parts
            )
        ):
            raise ValueError("model reference must be runtime-relative")
        return path

    @model_validator(mode="after")
    def _validate_runtime_combination(self) -> Self:
        artifact_fields = (
            self.model_ref,
            self.model_id,
            self.model_version,
            self.model_sha256,
        )
        if self.mode == "rules":
            if self.backend != "none" or any(
                value is not None for value in artifact_fields
            ):
                raise ValueError(
                    "rules mode requires no backend or model identity"
                )
            return self
        if self.backend != "sklearn" or any(
            value is None for value in artifact_fields
        ):
            raise ValueError(
                "model-backed intent modes require sklearn and pinned artifact"
            )
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
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    ocr: OCRSettings = Field(default_factory=OCRSettings)
    logging: LoggingSettings
    outbox: OutboxSettings = Field(default_factory=OutboxSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    m6_policy: M6PolicySettings = Field(default_factory=M6PolicySettings)
    intent: IntentSettings = Field(default_factory=IntentSettings)

    @model_validator(mode="after")
    def _validate_m6_runtime_boundary(self) -> Self:
        runtime_root = self.runtime_dir.resolve()
        configured = self.m6_policy.runtime_directory
        candidate = (
            configured.resolve()
            if configured.is_absolute()
            else (runtime_root / configured).resolve()
        )
        if candidate == runtime_root or not candidate.is_relative_to(runtime_root):
            raise ConfigurationError(
                code="UNSAFE_CONFIGURATION_PATH",
                fields=("m6_policy.runtime_directory",),
                reason="path_boundary",
            )
        object.__setattr__(
            self,
            "m6_policy",
            self.m6_policy.model_copy(
                update={"runtime_directory": candidate}
            ),
        )
        return self

    @model_validator(mode="after")
    def _validate_production_security(self) -> Self:
        if self.intent.model_ref is not None:
            try:
                runtime_root = self.runtime_dir.resolve()
                model_dir = (runtime_root / self.intent.model_ref).resolve()
            except (OSError, RuntimeError):
                raise ValueError(
                    "intent model reference is unavailable"
                ) from None
            if (
                model_dir == runtime_root
                or not model_dir.is_relative_to(runtime_root)
            ):
                raise ValueError(
                    "intent model reference must remain within runtime_dir"
                )
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
        if (
            self.retrieval.strategy in {"vector", "hybrid"}
            and self.embedding.backend != "openai_compatible"
        ):
            invalid_fields.extend(
                ("retrieval.strategy", "embedding.backend")
            )
        if invalid_fields:
            raise ConfigurationError(
                code="INSECURE_PRODUCTION_CONFIGURATION",
                fields=invalid_fields,
                reason="production_security",
            )
        return self
