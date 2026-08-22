"""Teachers confirm, rewrite, or ignore suggestions without deleting evidence."""

from __future__ import annotations

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.modules.m9_teacher_analytics.service import (
    M9TeacherAnalyticsService,
)
from tests.unit.test_m9_deepseek import _analytics


def test_teacher_can_confirm_modify_or_ignore_a_candidate_suggestion() -> None:
    bundle = _analytics()
    service = M9TeacherAnalyticsServiceStub()
    service._repository.save_analytics(bundle)

    confirmed = service.apply_suggestion_decision(
        report_id=bundle.report_id,
        suggestion_id="suggestion_private_scope",
        decision="confirmed",
    )
    assert confirmed.teaching_suggestions[0].status == "confirmed"
    assert confirmed.teaching_suggestions[0].evidence_ids == [
        "audit_private_scope"
    ]

    rewritten = service.apply_suggestion_decision(
        report_id=bundle.report_id,
        suggestion_id="suggestion_private_scope",
        decision="modified",
        content="下节课先复习该知识点，而不是另排新课。",
    )
    assert rewritten.teaching_suggestions[0].status == "modified"
    assert "另排新课" in rewritten.teaching_suggestions[0].content

    ignored = service.apply_suggestion_decision(
        report_id=bundle.report_id,
        suggestion_id="suggestion_private_scope",
        decision="ignored",
    )
    assert ignored.teaching_suggestions[0].status == "ignored"
    assert ignored.teaching_suggestions[0].evidence_ids == [
        "audit_private_scope"
    ]


def test_unknown_suggestion_decision_fails_closed() -> None:
    bundle = _analytics()
    service = M9TeacherAnalyticsServiceStub()
    service._repository.save_analytics(bundle)

    with pytest.raises(DomainError) as error:
        service.apply_suggestion_decision(
            report_id=bundle.report_id,
            suggestion_id="suggestion_private_scope",
            decision="auto_schedule",
        )

    assert error.value.code == "SUGGESTION_DECISION_INVALID"


class M9TeacherAnalyticsServiceStub(M9TeacherAnalyticsService):
    def __init__(self) -> None:
        from course_insight.modules.m9_teacher_analytics.stubs import (
            _MemoryM9Repository,
        )

        super().__init__(_MemoryM9Repository(), object(), object())
