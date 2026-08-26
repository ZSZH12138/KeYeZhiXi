from __future__ import annotations

from course_insight.infrastructure.scoped_credentials import (
    protect_secret,
    unprotect_secret,
)


def test_protected_secret_roundtrips_without_plaintext_storage() -> None:
    raw = "sk-deepseek-private-test-key"
    protected = protect_secret(raw, context="course_1\0class_1")

    assert raw.encode() not in protected.payload
    assert protected.scheme in {"windows-dpapi-v1", "fernet-v1"}
    assert (
        unprotect_secret(protected, context="course_1\0class_1")
        == raw
    )


def test_protected_secret_cannot_be_opened_in_another_scope() -> None:
    protected = protect_secret(
        "sk-deepseek-private-test-key",
        context="course_1\0class_1",
    )

    try:
        unprotect_secret(protected, context="course_1\0class_2")
    except ValueError as error:
        assert str(error) == "protected secret is unavailable"
    else:
        raise AssertionError("scope-bound protected secret was reused")
