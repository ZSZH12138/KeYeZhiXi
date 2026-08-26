"""Signed opaque handoff from one assessment item to independent QA."""

from __future__ import annotations

from django.core import signing

from course_insight.contracts.errors import DomainError


_SALT = "course-insight.assessment-qa-handoff.v1"


def issue_qa_handoff(
    *,
    course_id: str,
    class_id: str,
    learner_id: str,
    paper_id: str,
    item_instance_id: str,
) -> str:
    return signing.dumps(
        {
            "course_id": course_id,
            "class_id": class_id,
            "learner_id": learner_id,
            "paper_id": paper_id,
            "item_instance_id": item_instance_id,
        },
        salt=_SALT,
        compress=True,
    )


def verify_qa_handoff(
    token: str,
    *,
    course_id: str,
    class_id: str,
    learner_id: str,
    max_age_seconds: int,
) -> tuple[str, str]:
    try:
        payload = signing.loads(
            token,
            salt=_SALT,
            max_age=max_age_seconds,
        )
    except signing.BadSignature as error:
        raise _invalid_handoff() from error
    if not isinstance(payload, dict) or (
        payload.get("course_id") != course_id
        or payload.get("class_id") != class_id
        or payload.get("learner_id") != learner_id
    ):
        raise _invalid_handoff()
    paper_id = payload.get("paper_id")
    item_instance_id = payload.get("item_instance_id")
    if not (
        isinstance(paper_id, str)
        and paper_id
        and isinstance(item_instance_id, str)
        and item_instance_id
    ):
        raise _invalid_handoff()
    return paper_id, item_instance_id


def _invalid_handoff() -> DomainError:
    return DomainError(
        code="QA_HANDOFF_INVALID",
        module="m0",
        message="题目答疑入口无效或已过期。",
        recoverable=True,
    )


__all__ = ["issue_qa_handoff", "verify_qa_handoff"]
