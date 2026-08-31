from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from course_insight.contracts.intelligence import (
    LLMGenerationRequest,
    LLMModelRef,
)
from course_insight.infrastructure.deepseek import (
    DEEPSEEK_CHAT_COMPLETIONS_URL,
    DeepSeekClient,
    DeepSeekHTTPResponse,
)


NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)


class _FakeTransport:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> DeepSeekHTTPResponse:
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": json.loads(payload),
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _request() -> LLMGenerationRequest:
    return LLMGenerationRequest(
        request_id="request_1",
        use_case="rubric_scoring",
        model_ref=LLMModelRef(
            model_name="deepseek-v4-flash",
            model_version="runtime-api",
            status="configured",
        ),
        prompt_template_id="prompt",
        prompt_template_version="1",
        evidence_ids=["evidence_1"],
        input_checksum="a" * 64,
        created_at=NOW,
    )


def _response(
    structured: dict[str, Any] | None = None,
    *,
    finish_reason: str = "stop",
) -> DeepSeekHTTPResponse:
    content = json.dumps(
        structured or {"citation_ids": ["evidence_1"]},
        ensure_ascii=False,
    )
    return DeepSeekHTTPResponse(
        status_code=200,
        body=json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {"content": content},
                    }
                ],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                },
            }
        ).encode(),
        headers={},
    )


def _client(transport: _FakeTransport, **kwargs: Any) -> DeepSeekClient:
    return DeepSeekClient(
        transport=transport,
        sleep=kwargs.pop("sleep", lambda _: None),
        monotonic=lambda: 1.0,
        clock=lambda: NOW,
        **kwargs,
    )


def test_client_uses_current_deepseek_json_endpoint(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    transport = _FakeTransport(_response())

    invocation = _client(transport).invoke_json(
        request=_request(),
        messages=(
            {"role": "system", "content": "Return json."},
            {"role": "user", "content": "{}"},
        ),
    )

    assert invocation.result.status == "succeeded"


def test_explicit_api_key_ignores_global_environment_key(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "wrong-global-key")
    transport = _FakeTransport(_response())

    invocation = _client(
        transport,
        api_key="teacher-scoped-key",
    ).invoke_json(
        request=_request(),
        messages=(
            {"role": "system", "content": "Return json."},
            {"role": "user", "content": "{}"},
        ),
    )


def _web_request() -> LLMGenerationRequest:
    return LLMGenerationRequest(
        request_id="student_qa_web_1",
        use_case="student_rag_qa",
        model_ref=LLMModelRef(
            model_name="deepseek-v4-flash",
            model_version="runtime-api",
            status="configured",
        ),
        prompt_template_id="student-qa-web-search",
        prompt_template_version="1",
        evidence_ids=[],
        input_checksum="b" * 64,
        created_at=NOW,
    )

    assert invocation.result.status == "succeeded"
    assert transport.calls[0]["headers"]["Authorization"] == (
        "Bearer teacher-scoped-key"
    )
    assert invocation.result.citation_ids == ["evidence_1"]
    assert invocation.audit.status == "succeeded"
    assert invocation.audit.input_tokens == 11
    assert invocation.audit.output_tokens == 7
    assert invocation.audit.error_code is None
    assert transport.calls[0]["url"] == DEEPSEEK_CHAT_COMPLETIONS_URL
    assert transport.calls[0]["payload"]["model"] == "deepseek-v4-flash"
    assert transport.calls[0]["payload"]["response_format"] == {
        "type": "json_object"
    }
    assert transport.calls[0]["payload"]["thinking"] == {"type": "disabled"}
    assert transport.calls[0]["payload"]["temperature"] == 0.0
    assert transport.calls[0]["payload"]["max_tokens"] == 4096
    assert transport.calls[0]["payload"]["stream"] is False


def test_web_search_uses_responses_api_and_returns_only_https_sources() -> None:
    response = DeepSeekHTTPResponse(
        status_code=200,
        body=json.dumps(
            {
                "id": "response-1",
                "object": "response",
                "status": "completed",
                "model": "deepseek-v4-flash",
                "output": [
                    {
                        "type": "web_search_call",
                        "id": "search-1",
                        "status": "completed",
                        "action": {"type": "search", "query": "TCP 慢启动"},
                    },
                    {
                        "type": "message",
                        "id": "message-1",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "慢启动用于逐步增加拥塞窗口。",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "title": "RFC 5681",
                                        "url": "https://www.rfc-editor.org/rfc/rfc5681",
                                        "start_index": 0,
                                        "end_index": 3,
                                    },
                                    {
                                        "type": "url_citation",
                                        "title": "Unsafe",
                                        "url": "javascript:alert(1)",
                                        "start_index": 0,
                                        "end_index": 3,
                                    },
                                    {
                                        "type": "url_citation",
                                        "title": "Whitespace URL",
                                        "url": "https://safe.example/path\njavascript:alert(1)",
                                        "start_index": 0,
                                        "end_index": 3,
                                    },
                                ],
                            }
                        ],
                    },
                ],
                "usage": {"input_tokens": 15, "output_tokens": 9},
                "error": None,
                "incomplete_details": None,
            },
            ensure_ascii=False,
        ).encode(),
        headers={},
    )
    transport = _FakeTransport(response)

    invocation = _client(
        transport,
        api_key="teacher-scoped-key",
    ).invoke_web_search(
        request=_web_request(),
        question="慢启动有什么作用？",
    )

    assert invocation.result.status == "succeeded"
    assert invocation.result.content == "慢启动用于逐步增加拥塞窗口。"
    assert [(source.source_id, source.title, source.url) for source in invocation.sources] == [
        (
            "web-source-1",
            "RFC 5681",
            "https://www.rfc-editor.org/rfc/rfc5681",
        )
    ]
    assert invocation.audit.input_tokens == 15
    assert invocation.audit.output_tokens == 9
    assert transport.calls[0]["url"] == "https://api.deepseek.com/responses"
    assert transport.calls[0]["headers"]["Authorization"] == (
        "Bearer teacher-scoped-key"
    )
    assert transport.calls[0]["payload"]["model"] == "deepseek-v4-flash"
    assert transport.calls[0]["payload"]["tools"] == [{"type": "web_search"}]
    assert transport.calls[0]["payload"]["tool_choice"] == {"type": "web_search"}


def test_client_fails_closed_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    transport = _FakeTransport()

    invocation = _client(transport).invoke_json(
        request=_request(),
        messages=({"role": "user", "content": "Return json."},),
    )

    assert invocation.result.status == "failed"
    assert invocation.audit.error_code == "DEEPSEEK_API_KEY_MISSING"
    assert not transport.calls


def test_client_retries_429_and_5xx_then_succeeds(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    sleeps: list[float] = []
    transport = _FakeTransport(
        DeepSeekHTTPResponse(429, b"private body", {}),
        DeepSeekHTTPResponse(503, b"private body", {}),
        _response(),
    )

    invocation = _client(
        transport,
        sleep=sleeps.append,
    ).invoke_json(
        request=_request(),
        messages=({"role": "user", "content": "Return json."},),
    )

    assert invocation.result.status == "succeeded"
    assert len(transport.calls) == 3
    assert sleeps == [0.25, 0.5]


def test_client_maps_timeouts_and_malformed_json_to_safe_errors(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    timeout = _client(
        _FakeTransport(TimeoutError(), TimeoutError(), TimeoutError())
    ).invoke_json(
        request=_request(),
        messages=({"role": "user", "content": "Return json."},),
    )
    malformed = _client(
        _FakeTransport(DeepSeekHTTPResponse(200, b"not-json", {})),
        max_attempts=1,
    ).invoke_json(
        request=_request(),
        messages=({"role": "user", "content": "Return json."},),
    )

    assert timeout.audit.error_code == "DEEPSEEK_NETWORK_ERROR"
    assert malformed.audit.error_code == "DEEPSEEK_RESPONSE_INVALID"
    assert "private" not in str(timeout.audit.to_dict())
    assert "not-json" not in str(malformed.audit.to_dict())


def test_client_blocks_provider_content_filter(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")

    invocation = _client(
        _FakeTransport(_response(finish_reason="content_filter"))
    ).invoke_json(
        request=_request(),
        messages=({"role": "user", "content": "Return json."},),
    )

    assert invocation.result.status == "blocked"
    assert invocation.audit.status == "blocked"
    assert invocation.audit.error_code == "DEEPSEEK_CONTENT_FILTERED"


def test_client_retries_documented_empty_json_output(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    sleeps: list[float] = []
    empty = DeepSeekHTTPResponse(
        status_code=200,
        body=json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "   "},
                    }
                ]
            }
        ).encode(),
        headers={},
    )
    transport = _FakeTransport(empty, _response())

    invocation = _client(transport, sleep=sleeps.append).invoke_json(
        request=_request(),
        messages=({"role": "user", "content": "Return json."},),
    )

    assert invocation.result.status == "succeeded"
    assert len(transport.calls) == 2
    assert sleeps == [0.25]


def test_client_retries_malformed_200_json_output(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    sleeps: list[float] = []
    transport = _FakeTransport(
        DeepSeekHTTPResponse(200, b"not-json", {}),
        _response(),
    )

    invocation = _client(transport, sleep=sleeps.append).invoke_json(
        request=_request(),
        messages=({"role": "user", "content": "Return json."},),
    )

    assert invocation.result.status == "succeeded"
    assert len(transport.calls) == 2
    assert sleeps == [0.25]


def test_client_rejects_duplicate_json_keys(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-test-key")
    response = DeepSeekHTTPResponse(
        status_code=200,
        body=json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": (
                                '{"citation_ids":["evidence_1"],'
                                '"citation_ids":[]}'
                            )
                        },
                    }
                ]
            }
        ).encode(),
        headers={},
    )

    invocation = _client(_FakeTransport(response), max_attempts=1).invoke_json(
        request=_request(),
        messages=({"role": "user", "content": "Return json."},),
    )

    assert invocation.result.status == "failed"
    assert invocation.audit.error_code == "DEEPSEEK_RESPONSE_INVALID"
