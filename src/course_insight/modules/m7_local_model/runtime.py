"""Safe M7 runtime assembly for global and course/class API credentials."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from course_insight.contracts.assessment import RubricScoringTask
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceBundle
from course_insight.infrastructure.deepseek import (
    DEEPSEEK_API_KEY_ENV,
    DeepSeekClient,
    DeepSeekTransport,
)
from course_insight.modules.m7_local_model.adapter import (
    DeepSeekM7Adapter,
    M7ScoringOutcome,
)
from course_insight.modules.m7_local_model.policy import (
    DEFAULT_M7_EXECUTION_POLICY,
    M7ExecutionPolicy,
)
from course_insight.modules.m7_local_model.privacy import (
    DEFAULT_M7_OUTBOUND_PRIVACY_POLICY,
    M7OutboundPrivacyPolicy,
)
from course_insight.modules.m7_local_model.privacy_reviewer import PrivacyReviewer


@dataclass(frozen=True, slots=True)
class ScopedDeepSeekM7Settings:
    """Decrypted settings for one exact course/class scoring request."""

    api_key: str = field(repr=False)
    model_name: str
    thinking_enabled: bool
    api_revision: int

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("scoped DeepSeek API key must not be blank")
        if type(self.api_revision) is not int or self.api_revision < 0:
            raise ValueError("scoped DeepSeek API revision is invalid")


class ScopedDeepSeekM7SettingsResolver(Protocol):
    """Resolve a write-only course/class credential at invocation time."""

    def __call__(
        self,
        course_id: str,
        class_id: str,
    ) -> ScopedDeepSeekM7Settings | None:
        """Return current settings for the exact scope, or ``None``."""


@dataclass(frozen=True, slots=True)
class ScopedDeepSeekM7Adapter:
    """Route each rubric task through its own course/class DeepSeek settings."""

    settings_resolver: ScopedDeepSeekM7SettingsResolver
    privacy_reviewer: PrivacyReviewer
    privacy_policy: M7OutboundPrivacyPolicy = DEFAULT_M7_OUTBOUND_PRIVACY_POLICY
    transport: DeepSeekTransport | None = None
    timeout_seconds: float = 30.0
    max_attempts: int = 3
    retry_base_seconds: float = 0.25
    max_response_bytes: int = 1024 * 1024
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    clock: Callable[[], datetime] | None = None
    runtime_dir: Path | None = None

    def score_governed(
        self,
        task: RubricScoringTask,
        evidence_bundle: EvidenceBundle,
    ) -> M7ScoringOutcome:
        """Resolve the exact class key and delegate to the governed scorer."""

        if task.course_id is None or task.class_id is None:
            raise DomainError(
                code="MODEL_ADAPTER_UNCONFIGURED",
                module="m7",
                message="rubric scoring task has no course/class API scope",
                details={"scoring_task_id": task.scoring_task_id},
                recoverable=True,
            )
        settings = self.settings_resolver(task.course_id, task.class_id)
        if settings is None:
            raise DomainError(
                code="MODEL_ADAPTER_UNCONFIGURED",
                module="m7",
                message="the course/class DeepSeek API is not configured",
                details={
                    "scoring_task_id": task.scoring_task_id,
                    "course_id": task.course_id,
                    "class_id": task.class_id,
                },
                recoverable=True,
            )
        policy = replace(
            DEFAULT_M7_EXECUTION_POLICY,
            model_name=settings.model_name,
            thinking_enabled=settings.thinking_enabled,
        )
        client = DeepSeekClient(
            model_name=policy.model_name,
            model_version=policy.model_version,
            timeout_seconds=self.timeout_seconds,
            max_attempts=self.max_attempts,
            retry_base_seconds=self.retry_base_seconds,
            max_response_bytes=self.max_response_bytes,
            max_tokens=policy.max_tokens,
            thinking_enabled=policy.thinking_enabled,
            temperature=policy.temperature,
            transport=self.transport,
            sleep=self.sleep,
            monotonic=self.monotonic,
            clock=self.clock,
            runtime_dir=self.runtime_dir,
            api_key=settings.api_key,
        )
        return DeepSeekM7Adapter(
            client,
            policy=policy,
            privacy_policy=self.privacy_policy,
            privacy_reviewer=self.privacy_reviewer,
        ).score_governed(task, evidence_bundle)


def required_deepseek_api_key_env() -> str:
    """Return the only supported secret-injection interface, never its value."""

    return DEEPSEEK_API_KEY_ENV


def build_deepseek_m7_adapter(
    *,
    privacy_reviewer: PrivacyReviewer,
    policy: M7ExecutionPolicy = DEFAULT_M7_EXECUTION_POLICY,
    privacy_policy: M7OutboundPrivacyPolicy = DEFAULT_M7_OUTBOUND_PRIVACY_POLICY,
    transport: DeepSeekTransport | None = None,
    timeout_seconds: float = 30.0,
    max_attempts: int = 1,
    retry_base_seconds: float = 0.25,
    max_response_bytes: int = 1024 * 1024,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    clock: Callable[[], datetime] | None = None,
    runtime_dir: Path | None = None,
) -> DeepSeekM7Adapter:
    """Assemble a policy-matched scorer without accepting raw key material.

    ``DeepSeekClient`` resolves ``DEEPSEEK_API_KEY`` only when a request is
    invoked.  The factory deliberately has no ``api_key`` argument and never
    reads or returns the environment value.
    """

    if not isinstance(policy, M7ExecutionPolicy):
        raise TypeError("policy must be an M7ExecutionPolicy")
    client = DeepSeekClient(
        model_name=policy.model_name,
        model_version=policy.model_version,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        max_response_bytes=max_response_bytes,
        max_tokens=policy.max_tokens,
        thinking_enabled=policy.thinking_enabled,
        temperature=policy.temperature,
        transport=transport,
        sleep=sleep,
        monotonic=monotonic,
        clock=clock,
        runtime_dir=runtime_dir,
    )
    return DeepSeekM7Adapter(
        client,
        policy=policy,
        privacy_policy=privacy_policy,
        privacy_reviewer=privacy_reviewer,
    )


def build_scoped_deepseek_m7_adapter(
    *,
    settings_resolver: ScopedDeepSeekM7SettingsResolver,
    privacy_reviewer: PrivacyReviewer,
    privacy_policy: M7OutboundPrivacyPolicy = DEFAULT_M7_OUTBOUND_PRIVACY_POLICY,
    transport: DeepSeekTransport | None = None,
    timeout_seconds: float = 30.0,
    max_attempts: int = 3,
    retry_base_seconds: float = 0.25,
    max_response_bytes: int = 1024 * 1024,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    clock: Callable[[], datetime] | None = None,
    runtime_dir: Path | None = None,
) -> ScopedDeepSeekM7Adapter:
    """Build a governed scorer that resolves keys from each task's scope."""

    if not callable(settings_resolver):
        raise TypeError("settings_resolver must be callable")
    return ScopedDeepSeekM7Adapter(
        settings_resolver=settings_resolver,
        privacy_reviewer=privacy_reviewer,
        privacy_policy=privacy_policy,
        transport=transport,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        max_response_bytes=max_response_bytes,
        sleep=sleep,
        monotonic=monotonic,
        clock=clock,
        runtime_dir=runtime_dir,
    )


def build_m7_privacy_reviewer(
    *,
    environment: str,
    runtime_dir: Path,
) -> PrivacyReviewer | None:
    """Build the required local reviewer without depending on API-key location."""

    from course_insight.infrastructure.deepseek_secrets import (
        inspect_m7_privacy_artifacts,
        privacy_model_dir,
    )
    from course_insight.modules.m7_local_model.privacy_reviewer import (
        DenyAllPrivacyReviewer,
        build_presidio_spacy_reviewer,
        build_required_m7_privacy_reviewer,
    )

    ready, _reason = inspect_m7_privacy_artifacts(runtime_dir)
    reviewer: PrivacyReviewer | None = None
    if ready:
        reviewer = build_required_m7_privacy_reviewer(
            runtime_dir=runtime_dir.resolve(),
            model_dir=privacy_model_dir(runtime_dir),
            expected_presidio_version=os.environ.get(
                "COURSE_INSIGHT_M7_PRESIDIO_VERSION",
                "2.2.364",
            ),
            expected_spacy_version=os.environ.get(
                "COURSE_INSIGHT_M7_SPACY_VERSION",
                "3.8.13",
            ),
            expected_spacy_model_version=os.environ.get(
                "COURSE_INSIGHT_M7_SPACY_MODEL_VERSION",
                "3.8.0",
            ),
            expected_semantic_model_id=os.environ.get(
                "COURSE_INSIGHT_M7_PRIVACY_MODEL_ID",
                "m7-semantic-privacy",
            ),
            expected_semantic_model_version=os.environ.get(
                "COURSE_INSIGHT_M7_PRIVACY_MODEL_VERSION",
                "1",
            ),
            expected_semantic_model_sha256=os.environ[
                "COURSE_INSIGHT_M7_PRIVACY_MODEL_SHA256"
            ],
            expected_semantic_manifest_sha256=os.environ[
                "COURSE_INSIGHT_M7_PRIVACY_MANIFEST_SHA256"
            ],
        )
    elif environment == "development":
        development_reviewer = build_presidio_spacy_reviewer(
            expected_model_version=os.environ.get(
                "COURSE_INSIGHT_M7_SPACY_MODEL_VERSION",
                "3.8.0",
            ),
            expected_presidio_version=os.environ.get(
                "COURSE_INSIGHT_M7_PRESIDIO_VERSION",
                "2.2.364",
            ),
            expected_spacy_version=os.environ.get(
                "COURSE_INSIGHT_M7_SPACY_VERSION",
                "3.8.13",
            ),
        )
        if not isinstance(development_reviewer, DenyAllPrivacyReviewer):
            reviewer = development_reviewer
    return reviewer


__all__ = [
    "ScopedDeepSeekM7Adapter",
    "ScopedDeepSeekM7Settings",
    "ScopedDeepSeekM7SettingsResolver",
    "build_deepseek_m7_adapter",
    "build_m7_privacy_reviewer",
    "build_scoped_deepseek_m7_adapter",
    "required_deepseek_api_key_env",
]
