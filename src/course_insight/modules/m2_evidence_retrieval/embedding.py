"""Provider ports and safe adapters for M2 embeddings.

The module deliberately contains no vendor SDK dependency.  The production
adapter speaks the OpenAI-compatible ``/v1/embeddings`` protocol over the
standard-library HTTP client, while the deterministic provider is explicitly
limited to the test environment.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from course_insight.infrastructure.config import ConfigurationError


EmbeddingVector = tuple[float, ...]
EmbeddingBatch = tuple[EmbeddingVector, ...]
MAX_VECTOR_DIMENSION = 16_000


@dataclass(frozen=True, slots=True)
class EmbeddingModelIdentity:
    """Stable, secret-free identity of one embedding model deployment."""

    provider: str
    model_name: str
    model_version: str
    dimension: int

    def __post_init__(self) -> None:
        for field_name in ("provider", "model_name", "model_version"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{field_name} must be a non-blank string")
            if len(value) > 256:
                raise ValueError(f"{field_name} is too long")
        if (
            isinstance(self.dimension, bool)
            or not isinstance(self.dimension, int)
            or self.dimension < 1
            or self.dimension > MAX_VECTOR_DIMENSION
        ):
            raise ValueError("dimension must be a positive bounded integer")


class EmbeddingProviderError(RuntimeError):
    """Safe provider failure that never carries secrets or remote payloads."""

    _REASONS = {
        "DETERMINISTIC_PROVIDER_FORBIDDEN": "test-only provider unavailable",
        "EMBEDDING_INPUT_INVALID": "embedding input is invalid",
        "EMBEDDING_VECTOR_INVALID": "embedding vector is invalid",
        "EMBEDDING_DIMENSION_MISMATCH": "embedding dimension mismatch",
        "PROVIDER_RESPONSE_TOO_LARGE": "provider response exceeds the limit",
        "PROVIDER_RESPONSE_INVALID": "provider response is invalid",
        "PROVIDER_MODEL_MISMATCH": "provider model does not match configuration",
        "PROVIDER_REQUEST_REJECTED": "provider request was rejected",
        "PROVIDER_RETRY_EXHAUSTED": "provider retry limit exhausted",
        "PROVIDER_TIMEOUT": "provider request timed out",
        "PROVIDER_UNAVAILABLE": "provider is unavailable",
    }

    def __init__(self, *, code: str) -> None:
        if code not in self._REASONS:
            raise ValueError("unknown embedding provider error code")
        self.code = code
        super().__init__(f"[m2:{code}] {self._REASONS[code]}")


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Replaceable port used by M2 index and query code."""

    @property
    def model_ref(self) -> EmbeddingModelIdentity:
        """Return the model identity bound to all vectors from this provider."""

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        """Embed texts in the same order as the input sequence."""

    def embed_query(self, text: str) -> EmbeddingVector:
        """Embed one query text."""


class DeterministicEmbeddingProvider:
    """Deterministic provider available only to explicitly test-mode callers."""

    def __init__(
        self,
        *,
        model_ref: EmbeddingModelIdentity,
        environment: str = "test",
    ) -> None:
        if environment != "test":
            raise EmbeddingProviderError(
                code="DETERMINISTIC_PROVIDER_FORBIDDEN"
            )
        if not isinstance(model_ref, EmbeddingModelIdentity):
            raise TypeError("model_ref must be an EmbeddingModelIdentity")
        self._model_ref = model_ref

    @classmethod
    def for_tests(
        cls,
        *,
        model_ref: EmbeddingModelIdentity,
    ) -> DeterministicEmbeddingProvider:
        """Construct the provider through an intentionally test-named API."""

        return cls(model_ref=model_ref, environment="test")

    @property
    def model_ref(self) -> EmbeddingModelIdentity:
        return self._model_ref

    @property
    def model_identity(self) -> EmbeddingModelIdentity:
        return self._model_ref

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        normalized_texts = _validate_text_sequence(texts)
        return tuple(self._embed_one(text) for text in normalized_texts)

    def embed_query(self, text: str) -> EmbeddingVector:
        _validate_text(text)
        return self._embed_one(text)

    def _embed_one(self, text: str) -> EmbeddingVector:
        values: list[float] = []
        counter = 0
        seed = text.encode("utf-8")
        while len(values) < self._model_ref.dimension:
            digest = hashlib.sha256(
                b"course-insight:test-embedding:v1\0"
                + self._model_ref.model_name.encode("utf-8")
                + b"\0"
                + seed
                + counter.to_bytes(4, "big")
            ).digest()
            for offset in range(0, len(digest), 4):
                raw = int.from_bytes(digest[offset : offset + 4], "big")
                values.append((raw / 4_294_967_296.0) * 2.0 - 1.0)
                if len(values) == self._model_ref.dimension:
                    break
            counter += 1
        return _validate_vector(tuple(values), self._model_ref.dimension)


@dataclass(frozen=True, slots=True)
class _RetryableFailure(Exception):
    code: str


class OpenAICompatibleEmbeddingProvider:
    """OpenAI-compatible embeddings adapter implemented with ``urllib``."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str | None = None,
        api_key_env: str | None = None,
        model_ref: EmbeddingModelIdentity,
        environment: str = "development",
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.25,
        max_backoff_seconds: float = 5.0,
        max_response_bytes: int = 4 * 1024 * 1024,
        verify_tls: bool = True,
        urlopen: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not isinstance(model_ref, EmbeddingModelIdentity):
            raise TypeError("model_ref must be an EmbeddingModelIdentity")
        self._endpoint = _normalize_endpoint(
            endpoint,
            environment=environment,
            verify_tls=verify_tls,
        )
        self._api_key = _resolve_api_key(api_key, api_key_env)
        self._model_ref = model_ref
        self._environment = environment
        self._timeout_seconds = _validate_finite_range(
            timeout_seconds,
            name="timeout_seconds",
            minimum=0.001,
            maximum=300.0,
        )
        self._max_retries = _validate_int_range(
            max_retries,
            name="max_retries",
            minimum=0,
            maximum=8,
        )
        self._backoff_seconds = _validate_finite_range(
            backoff_seconds,
            name="backoff_seconds",
            minimum=0.0,
            maximum=300.0,
        )
        self._max_backoff_seconds = _validate_finite_range(
            max_backoff_seconds,
            name="max_backoff_seconds",
            minimum=0.0,
            maximum=300.0,
        )
        if self._backoff_seconds > self._max_backoff_seconds:
            raise ConfigurationError(
                code="EMBEDDING_RETRY_INVALID",
                fields=("embedding.backoff_seconds", "embedding.max_backoff_seconds"),
                reason="retry_range",
            )
        self._max_response_bytes = _validate_int_range(
            max_response_bytes,
            name="max_response_bytes",
            minimum=1,
            maximum=100 * 1024 * 1024,
        )
        self._verify_tls = verify_tls
        self._urlopen = urllib.request.urlopen if urlopen is None else urlopen
        self._sleep = time.sleep if sleep is None else sleep
        self._ssl_context = (
            ssl.create_default_context()
            if verify_tls
            else ssl._create_unverified_context()
        )

    @classmethod
    def from_environment(
        cls,
        *,
        endpoint: str,
        api_key_env: str,
        model_ref: EmbeddingModelIdentity,
        environment: str = "development",
        **kwargs: Any,
    ) -> OpenAICompatibleEmbeddingProvider:
        """Construct from a named environment variable without storing its name as a secret."""

        if not isinstance(api_key_env, str) or not _is_environment_name(api_key_env):
            raise ConfigurationError(
                code="EMBEDDING_API_KEY_ENV_INVALID",
                fields=("embedding.api_key_env",),
                reason="invalid_name",
            )
        api_key = os.environ.get(api_key_env)
        if api_key is None or not api_key.strip():
            raise ConfigurationError(
                code="EMBEDDING_API_KEY_MISSING",
                fields=("embedding.api_key",),
                reason="required",
            )
        return cls(
            endpoint=endpoint,
            api_key=api_key,
            model_ref=model_ref,
            environment=environment,
            **kwargs,
        )

    @property
    def model_ref(self) -> EmbeddingModelIdentity:
        return self._model_ref

    @property
    def model_identity(self) -> EmbeddingModelIdentity:
        return self._model_ref

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        normalized_texts = _validate_text_sequence(texts)
        if not normalized_texts:
            return ()
        return self._embed(normalized_texts)

    def embed_query(self, text: str) -> EmbeddingVector:
        _validate_text(text)
        return self._embed((text,))[0]

    def _embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        request_payload = json.dumps(
            {"input": list(texts), "model": self._model_ref.model_name},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        response_body = self._request(request_payload)
        return _parse_embedding_response(
            response_body,
            expected_count=len(texts),
            expected_model=self._model_ref.model_name,
            expected_dimension=self._model_ref.dimension,
        )

    def _request(self, request_payload: bytes) -> bytes:
        for attempt in range(self._max_retries + 1):
            try:
                return self._request_once(request_payload)
            except _RetryableFailure as failure:
                if attempt >= self._max_retries:
                    if failure.code == "PROVIDER_TIMEOUT":
                        raise EmbeddingProviderError(code=failure.code) from None
                    if failure.code == "PROVIDER_UNAVAILABLE":
                        raise EmbeddingProviderError(code=failure.code) from None
                    raise EmbeddingProviderError(
                        code="PROVIDER_RETRY_EXHAUSTED"
                    ) from None
                delay = min(
                    self._backoff_seconds * (2**attempt),
                    self._max_backoff_seconds,
                )
                if delay > 0.0:
                    self._sleep(delay)
        raise AssertionError("embedding request loop did not return or raise")

    def _request_once(self, request_payload: bytes) -> bytes:
        request = urllib.request.Request(
            self._endpoint,
            data=request_payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            response = self._urlopen(
                request,
                timeout=self._timeout_seconds,
                context=self._ssl_context,
            )
        except urllib.error.HTTPError as error:
            status = int(getattr(error, "code", 0) or 0)
            _close_quietly(error)
            if status == 429 or 500 <= status <= 599:
                raise _RetryableFailure(code="PROVIDER_HTTP_RETRYABLE") from None
            raise EmbeddingProviderError(
                code="PROVIDER_REQUEST_REJECTED"
            ) from None
        except (TimeoutError, socket.timeout):
            raise _RetryableFailure(code="PROVIDER_TIMEOUT") from None
        except urllib.error.URLError as error:
            if isinstance(getattr(error, "reason", None), (TimeoutError, socket.timeout)):
                raise _RetryableFailure(code="PROVIDER_TIMEOUT") from None
            raise _RetryableFailure(code="PROVIDER_UNAVAILABLE") from None
        except ValueError:
            raise _RetryableFailure(code="PROVIDER_UNAVAILABLE") from None
        except OSError:
            raise _RetryableFailure(code="PROVIDER_UNAVAILABLE") from None

        try:
            status = _response_status(response)
            if status == 429 or 500 <= status <= 599:
                raise _RetryableFailure(code="PROVIDER_HTTP_RETRYABLE")
            if status < 200 or status >= 300:
                raise EmbeddingProviderError(
                    code="PROVIDER_REQUEST_REJECTED"
                )
            content_length = _response_content_length(response)
            if content_length is not None and content_length > self._max_response_bytes:
                raise EmbeddingProviderError(
                    code="PROVIDER_RESPONSE_TOO_LARGE"
                )
            try:
                body = response.read(self._max_response_bytes + 1)
            except (TimeoutError, socket.timeout):
                raise _RetryableFailure(code="PROVIDER_TIMEOUT") from None
            except OSError:
                raise _RetryableFailure(code="PROVIDER_UNAVAILABLE") from None
            if not isinstance(body, bytes) or len(body) > self._max_response_bytes:
                raise EmbeddingProviderError(
                    code="PROVIDER_RESPONSE_TOO_LARGE"
                )
            return body
        finally:
            _close_quietly(response)


def _normalize_endpoint(
    endpoint: str,
    *,
    environment: str,
    verify_tls: bool,
) -> str:
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ConfigurationError(
            code="EMBEDDING_ENDPOINT_INVALID",
            fields=("embedding.endpoint",),
            reason="required",
        )
    raw = endpoint.strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (ValueError, UnicodeError):
        raise ConfigurationError(
            code="EMBEDDING_ENDPOINT_INVALID",
            fields=("embedding.endpoint",),
            reason="invalid_url",
        ) from None
    del port
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            code="EMBEDDING_ENDPOINT_INVALID",
            fields=("embedding.endpoint",),
            reason="invalid_url",
        )
    if environment == "production" and (
        parsed.scheme != "https" or not verify_tls
    ):
        raise ConfigurationError(
            code="EMBEDDING_TLS_REQUIRED",
            fields=("embedding.endpoint",),
            reason="tls_required",
        )
    path = parsed.path.rstrip("/")
    if path.endswith("/v1/embeddings"):
        normalized_path = path
    elif path.endswith("/v1"):
        normalized_path = f"{path}/embeddings"
    else:
        normalized_path = f"{path}/v1/embeddings"
    return urlunsplit(
        (parsed.scheme, parsed.netloc, normalized_path, "", "")
    )


def _validate_api_key(api_key: str) -> str:
    if not isinstance(api_key, str) or not api_key.strip():
        raise ConfigurationError(
            code="EMBEDDING_API_KEY_MISSING",
            fields=("embedding.api_key",),
            reason="required",
        )
    if "\r" in api_key or "\n" in api_key:
        raise ConfigurationError(
            code="EMBEDDING_API_KEY_INVALID",
            fields=("embedding.api_key",),
            reason="invalid_value",
        )
    return api_key


def _resolve_api_key(api_key: str | None, api_key_env: str | None) -> str:
    if api_key is not None and api_key_env is not None:
        raise ConfigurationError(
            code="EMBEDDING_API_KEY_CONFIG_INVALID",
            fields=("embedding.api_key", "embedding.api_key_env"),
            reason="mutually_exclusive",
        )
    if api_key_env is not None:
        if not _is_environment_name(api_key_env):
            raise ConfigurationError(
                code="EMBEDDING_API_KEY_ENV_INVALID",
                fields=("embedding.api_key_env",),
                reason="invalid_name",
            )
        api_key = os.environ.get(api_key_env)
    if api_key is None or not isinstance(api_key, str) or not api_key.strip():
        raise ConfigurationError(
            code="EMBEDDING_API_KEY_MISSING",
            fields=("embedding.api_key",),
            reason="required",
        )
    return _validate_api_key(api_key)


def _validate_text_sequence(texts: Sequence[str]) -> tuple[str, ...]:
    if isinstance(texts, (str, bytes, bytearray)):
        raise EmbeddingProviderError(code="EMBEDDING_INPUT_INVALID")
    try:
        values = tuple(texts)
    except (TypeError, ValueError):
        raise EmbeddingProviderError(code="EMBEDDING_INPUT_INVALID") from None
    if any(not isinstance(text, str) for text in values):
        raise EmbeddingProviderError(code="EMBEDDING_INPUT_INVALID")
    for text in values:
        _validate_text(text)
    return values


def _validate_text(text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise EmbeddingProviderError(code="EMBEDDING_INPUT_INVALID")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise EmbeddingProviderError(code="EMBEDDING_INPUT_INVALID") from None


def _validate_vector(
    vector: Sequence[float],
    expected_dimension: int,
) -> EmbeddingVector:
    if len(vector) != expected_dimension:
        raise EmbeddingProviderError(code="EMBEDDING_DIMENSION_MISMATCH")
    normalized: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EmbeddingProviderError(code="EMBEDDING_VECTOR_INVALID")
        try:
            converted = float(value)
        except (OverflowError, TypeError, ValueError):
            raise EmbeddingProviderError(
                code="EMBEDDING_VECTOR_INVALID"
            ) from None
        if not math.isfinite(converted):
            raise EmbeddingProviderError(code="EMBEDDING_VECTOR_INVALID")
        normalized.append(converted)
    return tuple(normalized)


def _parse_embedding_response(
    response_body: bytes,
    *,
    expected_count: int,
    expected_model: str,
    expected_dimension: int,
) -> EmbeddingBatch:
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        raise EmbeddingProviderError(code="PROVIDER_RESPONSE_INVALID") from None
    if not isinstance(payload, Mapping):
        raise EmbeddingProviderError(code="PROVIDER_RESPONSE_INVALID")
    raw_data = payload.get("data")
    if not isinstance(raw_data, list) or len(raw_data) != expected_count:
        raise EmbeddingProviderError(code="PROVIDER_RESPONSE_INVALID")

    ordered: list[EmbeddingVector | None] = [None] * expected_count
    for item in raw_data:
        if not isinstance(item, Mapping):
            raise EmbeddingProviderError(code="PROVIDER_RESPONSE_INVALID")
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise EmbeddingProviderError(code="PROVIDER_RESPONSE_INVALID")
        if index < 0 or index >= expected_count or ordered[index] is not None:
            raise EmbeddingProviderError(code="PROVIDER_RESPONSE_INVALID")
        raw_vector = item.get("embedding")
        if not isinstance(raw_vector, list):
            raise EmbeddingProviderError(code="PROVIDER_RESPONSE_INVALID")
        try:
            ordered[index] = _validate_vector(raw_vector, expected_dimension)
        except EmbeddingProviderError:
            raise
        except (TypeError, ValueError):
            raise EmbeddingProviderError(code="EMBEDDING_VECTOR_INVALID") from None
    if any(vector is None for vector in ordered):
        raise EmbeddingProviderError(code="PROVIDER_RESPONSE_INVALID")

    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise EmbeddingProviderError(code="PROVIDER_RESPONSE_INVALID")
    if model != expected_model:
        raise EmbeddingProviderError(code="PROVIDER_MODEL_MISMATCH")
    return tuple(vector for vector in ordered if vector is not None)


def _response_status(response: Any) -> int:
    getcode = getattr(response, "getcode", None)
    if not callable(getcode):
        return 200
    try:
        status = getcode()
    except (OSError, ValueError):
        raise EmbeddingProviderError(code="PROVIDER_UNAVAILABLE") from None
    if isinstance(status, bool) or not isinstance(status, int):
        raise EmbeddingProviderError(code="PROVIDER_RESPONSE_INVALID")
    return status


def _response_content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw_value = headers.get("Content-Length")
    except (AttributeError, TypeError, ValueError):
        raise EmbeddingProviderError(code="PROVIDER_RESPONSE_INVALID") from None
    if raw_value is None and isinstance(headers, Mapping):
        for name, value in headers.items():
            if isinstance(name, str) and name.lower() == "content-length":
                raw_value = value
                break
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        raise EmbeddingProviderError(code="PROVIDER_RESPONSE_INVALID") from None
    if value < 0:
        raise EmbeddingProviderError(code="PROVIDER_RESPONSE_INVALID")
    return value


def _close_quietly(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            return


def _validate_finite_range(
    value: float,
    *,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < minimum
        or float(value) > maximum
    ):
        raise ConfigurationError(
            code="EMBEDDING_SETTING_INVALID",
            fields=(f"embedding.{name}",),
            reason="range",
        )
    return float(value)


def _validate_int_range(
    value: int,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise ConfigurationError(
            code="EMBEDDING_SETTING_INVALID",
            fields=(f"embedding.{name}",),
            reason="range",
        )
    return value


def _is_environment_name(value: str) -> bool:
    return bool(value) and "A" <= value[0] <= "Z" and all(
        ("A" <= character <= "Z")
        or ("0" <= character <= "9")
        or character == "_"
        for character in value
    )


__all__ = [
    "DeterministicEmbeddingProvider",
    "EmbeddingBatch",
    "EmbeddingModelIdentity",
    "EmbeddingProvider",
    "EmbeddingProviderError",
    "EmbeddingVector",
    "OpenAICompatibleEmbeddingProvider",
]
