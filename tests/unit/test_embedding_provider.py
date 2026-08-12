"""S1 embedding provider port and adapter contract tests."""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any

import pytest

from course_insight.infrastructure.config import ConfigurationError
from course_insight.modules.m2_evidence_retrieval.embedding import (
    DeterministicEmbeddingProvider,
    EmbeddingModelIdentity,
    EmbeddingProvider,
    EmbeddingProviderError,
    OpenAICompatibleEmbeddingProvider,
)


def _identity(*, dimension: int = 4) -> EmbeddingModelIdentity:
    return EmbeddingModelIdentity(
        provider="openai-compatible",
        model_name="text-embedding-test",
        model_version="2026-01",
        dimension=dimension,
    )


@dataclass
class _FakeResponse:
    payload: bytes
    status: int = 200
    content_length: int | None = None

    def __post_init__(self) -> None:
        self.headers = {}
        if self.content_length is not None:
            self.headers["Content-Length"] = str(self.content_length)
        self.closed = False

    def getcode(self) -> int:
        return self.status

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self.payload
        return self.payload[:size]

    def close(self) -> None:
        self.closed = True


class _FakeOpener:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: Any, **kwargs: Any) -> object:
        self.calls.append(
            {
                "url": request.full_url,
                "headers": dict(request.headers),
                "body": json.loads(request.data.decode("utf-8")),
                "kwargs": kwargs,
            }
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _response(
    vectors: list[list[float]],
    *,
    model: str = "text-embedding-test",
    status: int = 200,
) -> _FakeResponse:
    return _FakeResponse(
        json.dumps(
            {
                "object": "list",
                "model": model,
                "data": [
                    {"object": "embedding", "index": i, "embedding": vector}
                    for i, vector in enumerate(vectors)
                ],
            }
        ).encode("utf-8"),
        status=status,
    )


def _provider(
    opener: _FakeOpener,
    *,
    dimension: int = 4,
    **kwargs: Any,
) -> OpenAICompatibleEmbeddingProvider:
    return OpenAICompatibleEmbeddingProvider(
        endpoint="http://embedding.test/api",
        api_key="secret-provider-key",
        model_ref=_identity(dimension=dimension),
        environment="test",
        urlopen=opener,
        **kwargs,
    )


def test_identity_and_provider_protocol_are_explicit_and_immutable() -> None:
    identity = _identity()
    provider = DeterministicEmbeddingProvider.for_tests(model_ref=identity)

    assert identity.dimension == 4
    assert provider.model_ref == identity
    assert provider.model_identity == identity
    assert isinstance(provider, EmbeddingProvider)
    with pytest.raises((AttributeError, TypeError)):
        identity.dimension = 8  # type: ignore[misc]


def test_deterministic_provider_is_stable_ordered_finite_and_exactly_sized() -> None:
    provider = DeterministicEmbeddingProvider.for_tests(model_ref=_identity())

    first = provider.embed_documents(["alpha", "beta", "alpha"])
    second = provider.embed_documents(["alpha", "beta", "alpha"])

    assert first == second
    assert first[0] == first[2]
    assert first[0] != first[1]
    assert len(first) == 3
    assert all(len(vector) == 4 for vector in first)
    assert all(value == pytest.approx(value) for vector in first for value in vector)
    assert provider.embed_documents([]) == ()
    assert provider.embed_query("alpha") == first[0]


def test_document_embedding_accepts_one_shot_iterables_without_losing_order() -> None:
    provider = DeterministicEmbeddingProvider.for_tests(model_ref=_identity())

    vectors = provider.embed_documents(iter(("first", "second")))

    assert vectors == provider.embed_documents(("first", "second"))


def test_deterministic_provider_cannot_be_created_outside_test_mode() -> None:
    with pytest.raises(EmbeddingProviderError) as captured:
        DeterministicEmbeddingProvider(model_ref=_identity(), environment="production")

    assert captured.value.code == "DETERMINISTIC_PROVIDER_FORBIDDEN"
    assert "production" not in str(captured.value)


def test_unencodable_text_is_rejected_without_echoing_input() -> None:
    provider = DeterministicEmbeddingProvider.for_tests(model_ref=_identity())

    with pytest.raises(EmbeddingProviderError) as captured:
        provider.embed_query("bad\ud800")

    assert captured.value.code == "EMBEDDING_INPUT_INVALID"
    assert "bad" not in str(captured.value)


def test_openai_compatible_adapter_posts_to_v1_embeddings_and_preserves_batch_order() -> None:
    opener = _FakeOpener(
        [
            _FakeResponse(
                json.dumps(
                    {
                        "model": "text-embedding-test",
                        "data": [
                            {"index": 1, "embedding": [0.2, 0.3, 0.4, 0.5]},
                            {"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]},
                        ],
                    }
                ).encode("utf-8")
            )
        ]
    )
    provider = _provider(opener)

    vectors = provider.embed_documents(["first", "second"])

    assert vectors == ((0.1, 0.2, 0.3, 0.4), (0.2, 0.3, 0.4, 0.5))
    call = opener.calls[0]
    assert call["url"] == "http://embedding.test/api/v1/embeddings"
    assert call["body"] == {
        "input": ["first", "second"],
        "model": "text-embedding-test",
    }
    assert call["headers"]["Authorization"] == "Bearer secret-provider-key"
    assert call["headers"]["Content-type"] == "application/json"


def test_openai_compatible_adapter_retries_429_and_5xx_with_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _FakeOpener(
        [
            _FakeResponse(b"rate limited", status=429),
            _FakeResponse(b"server failure", status=503),
            _response([[0.1, 0.2, 0.3, 0.4]]),
        ]
    )
    delays: list[float] = []
    provider = _provider(
        opener,
        max_retries=2,
        backoff_seconds=0.25,
        max_backoff_seconds=0.5,
        sleep=delays.append,
    )

    assert provider.embed_query("retry me") == (0.1, 0.2, 0.3, 0.4)
    assert len(opener.calls) == 3
    assert delays == [0.25, 0.5]


def test_retry_exhaustion_is_safe_and_does_not_expose_key_body_or_endpoint() -> None:
    secret = "secret-provider-key"
    endpoint = "http://embedding.test/private/path"
    opener = _FakeOpener(
        [
            _FakeResponse(
                f"response contains {secret} and {endpoint}".encode(),
                status=500,
            ),
            _FakeResponse(b"same sensitive response", status=500),
        ]
    )
    provider = OpenAICompatibleEmbeddingProvider(
        endpoint=endpoint,
        api_key=secret,
        model_ref=_identity(),
        environment="test",
        max_retries=1,
        urlopen=opener,
        sleep=lambda _: None,
    )

    with pytest.raises(EmbeddingProviderError) as captured:
        provider.embed_query("query")

    assert captured.value.code == "PROVIDER_RETRY_EXHAUSTED"
    message = str(captured.value)
    assert secret not in message
    assert endpoint not in message
    assert "response contains" not in message


def test_timeout_is_retried_and_mapped_without_underlying_exception_text() -> None:
    opener = _FakeOpener([TimeoutError("private timeout detail")] * 3)
    provider = _provider(
        opener,
        max_retries=2,
        sleep=lambda _: None,
    )

    with pytest.raises(EmbeddingProviderError) as captured:
        provider.embed_query("query")

    assert captured.value.code == "PROVIDER_TIMEOUT"
    assert "private timeout detail" not in str(captured.value)
    assert len(opener.calls) == 3


def test_socket_timeout_is_classified_as_timeout() -> None:
    opener = _FakeOpener([socket.timeout("sensitive socket detail")])
    provider = _provider(opener, max_retries=0)

    with pytest.raises(EmbeddingProviderError) as captured:
        provider.embed_query("query")

    assert captured.value.code == "PROVIDER_TIMEOUT"
    assert "sensitive socket detail" not in str(captured.value)


def test_transport_value_error_is_redacted_and_classified_as_unavailable() -> None:
    opener = _FakeOpener([ValueError("secret and private host path")])
    provider = _provider(opener, max_retries=0)

    with pytest.raises(EmbeddingProviderError) as captured:
        provider.embed_query("query")

    assert captured.value.code == "PROVIDER_UNAVAILABLE"
    assert "secret" not in str(captured.value)
    assert "private host path" not in str(captured.value)


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (b"not json", "PROVIDER_RESPONSE_INVALID"),
        (json.dumps({"data": []}).encode(), "PROVIDER_RESPONSE_INVALID"),
        (
            json.dumps(
                {"model": "other-model", "data": [{"index": 0, "embedding": [0.0] * 4}]}
            ).encode(),
            "PROVIDER_MODEL_MISMATCH",
        ),
        (
            json.dumps(
                {"data": [{"index": 0, "embedding": [0.0] * 3}]}
            ).encode(),
            "EMBEDDING_DIMENSION_MISMATCH",
        ),
        (
            b'{"data":[{"index":0,"embedding":[0.0, NaN, 0.0, 0.0]}]}',
            "EMBEDDING_VECTOR_INVALID",
        ),
        (
            json.dumps(
                {"data": [{"index": 0, "embedding": [10**1000] + [0.0] * 3}]}
            ).encode(),
            "EMBEDDING_VECTOR_INVALID",
        ),
    ],
)
def test_response_and_vector_validation_is_fail_closed(
    payload: bytes,
    expected_code: str,
) -> None:
    provider = _provider(_FakeOpener([_FakeResponse(payload)]))

    with pytest.raises(EmbeddingProviderError) as captured:
        provider.embed_query("query")

    assert captured.value.code == expected_code
    assert "NaN" not in str(captured.value)


def test_response_size_is_rejected_before_body_is_consumed() -> None:
    response = _FakeResponse(b"{}", content_length=100)
    provider = _provider(
        _FakeOpener([response]),
        max_response_bytes=16,
    )

    with pytest.raises(EmbeddingProviderError) as captured:
        provider.embed_query("query")

    assert captured.value.code == "PROVIDER_RESPONSE_TOO_LARGE"
    assert response.closed is True


def test_response_size_header_is_case_insensitive() -> None:
    response = _FakeResponse(b"{}")
    response.headers = {"content-length": "100"}
    provider = _provider(
        _FakeOpener([response]),
        max_response_bytes=16,
    )

    with pytest.raises(EmbeddingProviderError) as captured:
        provider.embed_query("query")

    assert captured.value.code == "PROVIDER_RESPONSE_TOO_LARGE"
    assert response.closed is True


def test_production_rejects_tls_disabled_http_endpoint_and_missing_key() -> None:
    with pytest.raises(ConfigurationError) as tls_error:
        OpenAICompatibleEmbeddingProvider(
            endpoint="https://embedding.test/api",
            api_key="secret",
            model_ref=_identity(),
            environment="production",
            verify_tls=False,
        )
    assert tls_error.value.code == "EMBEDDING_TLS_REQUIRED"
    assert "secret" not in str(tls_error.value)

    with pytest.raises(ConfigurationError) as http_error:
        OpenAICompatibleEmbeddingProvider(
            endpoint="http://embedding.test/api",
            api_key="secret",
            model_ref=_identity(),
            environment="production",
        )
    assert http_error.value.code == "EMBEDDING_TLS_REQUIRED"

    with pytest.raises(ConfigurationError) as key_error:
        OpenAICompatibleEmbeddingProvider(
            endpoint="https://embedding.test/api",
            api_key="",
            model_ref=_identity(),
            environment="production",
        )
    assert key_error.value.code == "EMBEDDING_API_KEY_MISSING"
    assert "secret" not in str(key_error.value)


def test_api_key_header_injection_is_rejected_without_echoing_the_key() -> None:
    secret = "secret-provider-key\nprivate-header"

    with pytest.raises(ConfigurationError) as captured:
        OpenAICompatibleEmbeddingProvider(
            endpoint="https://embedding.test/api",
            api_key=secret,
            model_ref=_identity(),
            environment="production",
        )

    assert captured.value.code == "EMBEDDING_API_KEY_INVALID"
    assert secret not in str(captured.value)


def test_provider_can_read_api_key_from_named_environment_without_exposing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_EMBEDDING_KEY", "secret-from-environment")
    opener = _FakeOpener([_response([[0.1, 0.2, 0.3, 0.4]])])
    provider = OpenAICompatibleEmbeddingProvider.from_environment(
        endpoint="http://embedding.test/api",
        api_key_env="TEST_EMBEDDING_KEY",
        model_ref=_identity(),
        environment="test",
        urlopen=opener,
    )

    provider.embed_query("query")

    assert opener.calls[0]["headers"]["Authorization"] == (
        "Bearer secret-from-environment"
    )


def test_adapter_constructor_supports_named_environment_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_EMBEDDING_KEY", "secret-from-environment")
    opener = _FakeOpener([_response([[0.1, 0.2, 0.3, 0.4]])])
    provider = OpenAICompatibleEmbeddingProvider(
        endpoint="http://embedding.test/api",
        api_key_env="TEST_EMBEDDING_KEY",
        model_ref=_identity(),
        environment="test",
        urlopen=opener,
    )

    provider.embed_query("query")

    assert opener.calls[0]["headers"]["Authorization"] == (
        "Bearer secret-from-environment"
    )
