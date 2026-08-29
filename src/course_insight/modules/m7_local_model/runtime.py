"""Safe M7 runtime assembly with a fixed environment-only API-key boundary."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from course_insight.infrastructure.deepseek import (
    DEEPSEEK_API_KEY_ENV,
    DeepSeekClient,
    DeepSeekTransport,
)
from course_insight.modules.m7_local_model.adapter import DeepSeekM7Adapter
from course_insight.modules.m7_local_model.policy import (
    DEFAULT_M7_EXECUTION_POLICY,
    M7ExecutionPolicy,
)
from course_insight.modules.m7_local_model.privacy import (
    DEFAULT_M7_OUTBOUND_PRIVACY_POLICY,
    M7OutboundPrivacyPolicy,
)
from course_insight.modules.m7_local_model.privacy_reviewer import PrivacyReviewer


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


__all__ = [
    "build_deepseek_m7_adapter",
    "required_deepseek_api_key_env",
]
