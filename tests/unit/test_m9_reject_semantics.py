from __future__ import annotations

import json
from datetime import timedelta

import pytest

from course_insight.contracts.analytics import (
    CriterionOverride,
    TeacherAnalyticsBundle,
    TeacherReviewDecision,
)
from course_insight.contracts.assessment import ScoringResultBundle
from course_insight.contracts.errors import DomainError
from course_insight.contracts.events import LearningEvent
from course_insight.contracts.knowledge import KnowledgeBundle, KnowledgeConcept
from course_insight.contracts.platform import TeacherReviewSubmission
from course_insight.modules.m0_platform.django_app.forms.review import (
    TeacherReviewForm,
)
from course_insight.modules.m0_platform.django_app.views.viewmodels import (
    student_result_view,
)
from course_insight.modules.m5_learner_class_state.service import M5StateService
from course_insight.modules.m8_assessment_scoring.service import (
    M8AssessmentService,
)
from course_insight.modules.m9_teacher_analytics.stubs import (
    M9TeacherAnalyticsServiceStub,
)
from course_insight.modules.m9_teacher_analytics.service import (
    M9TeacherAnalyticsService,
)
from tests.integration.test_web_workflow_persistence import (
    NOW,
    _feedback,
    _paper,
    _scoring_bundle,
    _state_result,
    _vector_scoring_bundle,
)
from tests.unit import test_application_web_workflow as workflow_support


class _MemoryM8Repository:
    def get_paper_execution_context(self, paper_id: str):
        return ("course_1", "class_1")

    def insert_or_get_scoring_result(self, bundle):
        return bundle

    def insert_or_get_reviewed_scoring_result(self, bundle, **_kwargs):
        return bundle


def _m8_service() -> M8AssessmentService:
    return M8AssessmentService(_MemoryM8Repository(), object(), object())


def _knowledge_bundle() -> KnowledgeBundle:
    return KnowledgeBundle(
        knowledge_bundle_id="bundle_1",
        course_package_id="package_1",
        course_id="course_1",
        bundle_version="1.0.0",
        concepts=[
            KnowledgeConcept(
                concept_id="concept_1",
                name="Governed concept",
                chapter_id="chapter_1",
                description="A governed concept used by the score state.",
                aliases=[],
                status="published",
            )
        ],
        prerequisite_relations=[],
        misconception_tags=[],
        items=[],
        rubrics=[],
        blueprints=[],
        q_matrix=[],
        status="published",
        published_at=NOW,
    )


def _scoring_with_event() -> ScoringResultBundle:
    scoring = _scoring_bundle()
    return ScoringResultBundle(
        **{
            **scoring.model_dump(mode="python"),
            "learning_events": [
                LearningEvent(
                    event_id="event_assessment_scored",
                    event_type="assessment_scored",
                    course_id="course_1",
                    class_id="class_1",
                    learner_id="learner_1",
                    attempt_id="attempt_1",
                    payload={
                        "paper_id": "paper_1",
                        "total_score": 1.0,
                        "max_score": 1.0,
                    },
                    occurred_at=NOW,
                )
            ],
        }
    )


def _decision(
    decision: str,
    *,
    expected_version: int = 1,
    final_total_score: float = 1.0,
    criterion_overrides: list[CriterionOverride] | None = None,
    expected_audit_checksum: str | None = None,
) -> TeacherReviewDecision:
    return TeacherReviewDecision(
        decision_id=f"decision_{decision}_{expected_version}",
        audit_id="audit_attempt_1",
        expected_audit_version=expected_version,
        expected_audit_checksum=expected_audit_checksum or ("0" * 64),
        decision=decision,
        final_total_score=final_total_score,
        criterion_overrides=criterion_overrides or [],
        teacher_comment="Reviewed against the governed evidence.",
        reviewer_id="teacher_1",
        reviewed_at=NOW + timedelta(minutes=expected_version),
    )


@pytest.mark.parametrize("decision", ["confirm", "reject"])
def test_non_override_decisions_cannot_change_the_audited_total(
    decision: str,
) -> None:
    audit = _scoring_with_event().get_audit_record("audit_attempt_1")

    with pytest.raises(DomainError) as captured:
        _decision(decision, final_total_score=0.5).assert_matches(audit)

    assert captured.value.code == "REVIEW_TOTAL_MISMATCH"


def test_reject_invalidates_without_zeroing_and_complete_override_recovers() -> None:
    m9_service = M9TeacherAnalyticsServiceStub()
    service = _m8_service()
    original = _scoring_with_event()
    submission = TeacherReviewSubmission(
        submission_id="decision_reject_1",
        audit_id="audit_attempt_1",
        expected_audit_version=1,
        expected_audit_checksum=(
            original.get_audit_record("audit_attempt_1").content_checksum()
        ),
        reviewer_id="teacher_1",
        decision="reject",
        final_total_score=1.0,
        criterion_overrides=[],
        teacher_comment="Reject this score and wait for a rescore.",
        submitted_at=NOW + timedelta(minutes=1),
    )
    decision = m9_service.record_teacher_review(submission, original)

    rejected = service.apply_teacher_review(
        current_scoring_result_bundle=original,
        teacher_review_decision=decision,
    )
    rejected_audit = rejected.get_audit_record("audit_attempt_1")

    assert original.get_audit_record("audit_attempt_1").audit_version == 1
    assert rejected_audit.audit_version == 2
    assert rejected_audit.review_status == "rejected_pending_rescore"
    assert rejected_audit.total_score == 1.0
    assert rejected.total_score == 1.0
    assert rejected.has_rejected_score() is True
    assert rejected.learning_events[-1].payload["decision"] == "reject"
    assert rejected.learning_events[-1].payload["total_score"] == 1.0

    with pytest.raises(DomainError) as blocked:
        rejected.assert_score_usable(module="m5")
    assert blocked.value.code == "SCORE_REJECTED_PENDING_RESCORE"

    override = _decision(
        "override",
        expected_version=2,
        expected_audit_checksum=rejected_audit.content_checksum(),
        final_total_score=0.5,
        criterion_overrides=[
            CriterionOverride(
                criterion_id="objective_item_1",
                previous_score=1.0,
                new_score=0.5,
                reason="Teacher supplied a complete replacement score.",
            )
        ],
    )
    recovered = service.apply_teacher_review(
        current_scoring_result_bundle=rejected,
        teacher_review_decision=override,
    )

    assert recovered.get_audit_record("audit_attempt_1").audit_version == 3
    assert recovered.get_audit_record("audit_attempt_1").review_status == "approved"
    assert recovered.total_score == 0.5
    assert recovered.has_rejected_score() is False
    recovered.assert_score_usable(module="m5")


def test_m5_blocks_rejected_score_and_m9_excludes_it_from_reports(
    tmp_path,
) -> None:
    rejected = _m8_service().apply_teacher_review(
        current_scoring_result_bundle=_scoring_with_event(),
        teacher_review_decision=_decision("reject"),
    )
    knowledge = _knowledge_bundle()
    state = _state_result(
        attempt_id="attempt_1",
        course_id="course_1",
        class_id="class_1",
        state_version=1,
    )
    state_policy = tmp_path / "state.json"
    state_policy.write_text(
        json.dumps(
            {
                "aggregation_policy_version": "1.0.0",
                "class_id": "class_1",
                "class_size": 1,
                "consolidating_threshold": 0.5,
                "mastered_threshold": 0.8,
                "minimum_assessed_count": 1,
                "minimum_coverage": 1.0,
                "misconception_activation_threshold": 0.5,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DomainError) as blocked:
        M5StateService(object(), object(), object()).update_state(
            scoring_result_bundle=rejected,
            knowledge_bundle=knowledge,
            previous_learner_state_snapshot=state.learner_state_snapshot,
            previous_class_state_snapshot=state.class_state_snapshot,
            state_policy_path=state_policy,
        )
    assert blocked.value.code == "SCORE_REJECTED_PENDING_RESCORE"

    teacher_policy = tmp_path / "teacher.json"
    teacher_policy.write_text(
        json.dumps(
            {
                "minimum_coverage": 1.0,
                "minimum_assessed_count": 1,
                "minimum_confidence": 0.5,
                "weak_mastery_threshold": 0.8,
                "misconception_threshold": 0.5,
                "priority_support_threshold": 0.5,
            }
        ),
        encoding="utf-8",
    )
    m9 = M9TeacherAnalyticsServiceStub()
    analytics = m9.build_rejected_score_analytics(
        knowledge_bundle=knowledge,
        scoring_result_bundle=rejected,
        state_update_result=state,
        teacher_threshold_policy_path=teacher_policy,
    )

    assert analytics.class_report.score_statistics["audit_count"] == 0.0
    assert analytics.class_report.score_statistics["score_total"] == 0.0
    assert analytics.class_report.coverage_rate == 0.0
    assert analytics.class_report.concept_summaries == []
    assert analytics.class_report.misconception_summaries == []
    assert analytics.class_report.evidence_status == "pending_rescore"
    assert analytics.individual_reports == []
    assert analytics.review_queue == []
    assert analytics.teaching_suggestions == []
    assert "_rejected_" in analytics.report_id
    assert analytics.generated_at == rejected.finalized_at
    assert m9.get_latest_analytics(
        course_id="course_1",
        class_id="class_1",
        learner_id="learner_1",
    ) == analytics


def test_any_rejected_audit_tombstones_all_attempt_level_score_statistics(
    tmp_path,
) -> None:
    mixed = _vector_scoring_bundle(
        first_version=1,
        second_version=1,
        finalized_at=NOW,
    )
    rejected_audit = mixed.get_audit_record("audit_a").model_copy(
        update={
            "audit_version": 2,
            "scoring_method": "teacher_override",
            "review_status": "rejected_pending_rescore",
            "review_reason": ["teacher_rejected_score"],
            "created_at": NOW + timedelta(minutes=1),
        },
        deep=True,
    )
    rejected = ScoringResultBundle(
        **{
            **mixed.model_dump(mode="python"),
            "score_audit_records": [
                *mixed.score_audit_records,
                rejected_audit,
            ],
            "finalized_at": NOW + timedelta(minutes=1),
        }
    )
    policy_path = tmp_path / "teacher.json"
    policy_path.write_text(
        json.dumps(
            {
                "minimum_coverage": 1.0,
                "minimum_assessed_count": 1,
                "minimum_confidence": 0.5,
                "weak_mastery_threshold": 0.8,
                "misconception_threshold": 0.5,
                "priority_support_threshold": 0.5,
            }
        ),
        encoding="utf-8",
    )

    analytics = M9TeacherAnalyticsServiceStub().build_rejected_score_analytics(
        knowledge_bundle=_knowledge_bundle(),
        scoring_result_bundle=rejected,
        state_update_result=_state_result(
            attempt_id="attempt_vector",
            course_id="course_1",
            class_id="class_1",
            state_version=1,
        ),
        teacher_threshold_policy_path=policy_path,
    )

    assert analytics.class_report.score_statistics == {
        "audit_count": 0.0,
        "score_total": 0.0,
        "score_mean": 0.0,
        "score_min": 0.0,
        "score_max": 0.0,
    }
    assert analytics.class_report.evidence_status == "pending_rescore"


def test_rejected_analytics_refuses_legacy_save_only_repository(tmp_path) -> None:
    class SaveOnlyRepository:
        def __init__(self) -> None:
            self.saved: list[TeacherAnalyticsBundle] = []

        def save_analytics(self, bundle: TeacherAnalyticsBundle) -> None:
            self.saved.append(bundle)

    rejected = _m8_service().apply_teacher_review(
        current_scoring_result_bundle=_scoring_with_event(),
        teacher_review_decision=_decision("reject"),
    )
    policy_path = tmp_path / "teacher.json"
    policy_path.write_text(
        json.dumps(
            {
                "minimum_coverage": 1.0,
                "minimum_assessed_count": 1,
                "minimum_confidence": 0.5,
                "weak_mastery_threshold": 0.8,
                "misconception_threshold": 0.5,
                "priority_support_threshold": 0.5,
            }
        ),
        encoding="utf-8",
    )
    repository = SaveOnlyRepository()
    service = M9TeacherAnalyticsService(  # type: ignore[arg-type]
        repository,
        object(),
        object(),
    )

    with pytest.raises(
        RuntimeError,
        match="learner-scope tombstone persistence",
    ):
        service.build_rejected_score_analytics(
            knowledge_bundle=_knowledge_bundle(),
            scoring_result_bundle=rejected,
            state_update_result=_state_result(
                attempt_id="attempt_1",
                course_id="course_1",
                class_id="class_1",
                state_version=1,
            ),
            teacher_threshold_policy_path=policy_path,
        )

    assert repository.saved == []


def test_rejected_score_is_hidden_from_student_view_and_requires_override() -> None:
    rejected = _m8_service().apply_teacher_review(
        current_scoring_result_bundle=_scoring_with_event(),
        teacher_review_decision=_decision("reject"),
    )
    view = student_result_view(_paper(), rejected, _feedback())
    audit = rejected.get_audit_record("audit_attempt_1")
    form = TeacherReviewForm(
        audit=audit,
        reviewer_id="teacher_1",
        criterion_caps={"objective_item_1": 1.0},
        data={
            "decision": "confirm",
            "final_total_score": "1.0",
            "teacher_comment": "This rejected score cannot be confirmed.",
        },
    )

    assert view.score_pending_rescore is True
    assert view.total_score is None
    assert view.feedback is None
    assert view.audits == ()
    assert tuple(form.fields["decision"].choices) == (("override", "改分"),)
    assert form.is_valid() is False


def test_review_workflow_skips_an_additional_m5_write_and_hides_score_fields(
    tmp_path,
    monkeypatch,
) -> None:
    original_apply = workflow_support._M8.apply_teacher_review

    def apply_rejection(self, teacher_review_decision, **kwargs):
        reviewed = original_apply(self, teacher_review_decision, **kwargs)
        audits = [item.model_copy(deep=True) for item in reviewed.score_audit_records]
        audits[-1] = audits[-1].model_copy(
            update={
                "review_status": "rejected_pending_rescore",
                "review_reason": ["teacher_rejected_score"],
            },
            deep=True,
        )
        rejected = ScoringResultBundle(
            **{
                **reviewed.model_dump(mode="python"),
                "score_audit_records": audits,
            }
        )
        self.store.reviewed = rejected
        self.store.scoring_history[-1] = rejected
        return rejected.model_copy(deep=True)

    def build_rejection_analytics(
        self,
        *,
        scoring_result_bundle,
        state_update_result,
        **kwargs,
    ):
        del kwargs
        assert scoring_result_bundle.has_rejected_score()
        base = workflow_support._analytics(
            report_id=(
                "report_course_1_"
                f"{state_update_result.class_state_snapshot.snapshot_id}_rejected"
            ),
            generated_at=state_update_result.updated_at,
        )
        safe = TeacherAnalyticsBundle(
            **{
                **base.model_dump(mode="python"),
                "class_report": base.class_report.model_copy(
                    update={
                        "score_statistics": {
                            "audit_count": 0.0,
                            "score_total": 0.0,
                        },
                        "coverage_rate": 0.0,
                        "concept_summaries": [],
                        "misconception_summaries": [],
                        "evidence_status": "pending_rescore",
                    },
                    deep=True,
                ),
                "individual_reports": [],
                "review_queue": [],
                "teaching_suggestions": [],
            }
        )
        self.store.analytics = safe
        self.store.analytics_history[safe.report_id] = safe
        return safe.model_copy(deep=True)

    monkeypatch.setattr(
        workflow_support._M8,
        "apply_teacher_review",
        apply_rejection,
    )
    monkeypatch.setattr(
        workflow_support._M9,
        "build_rejected_score_analytics_with_frozen_policy",
        build_rejection_analytics,
        raising=False,
    )
    store = workflow_support._WorkflowStore()
    coordinator = workflow_support._coordinator(tmp_path, store)
    knowledge, _, submit_arguments = workflow_support._start_and_submission(
        coordinator,
        tmp_path,
    )
    submitted = coordinator.submit_assessment(**submit_arguments)
    current_audit = submitted["scoring_result"].get_audit_record(
        "audit_attempt_1"
    )
    state_history_count = len(store.state_history)
    submission = TeacherReviewSubmission(
        submission_id="decision_reject_1",
        audit_id="audit_attempt_1",
        expected_audit_version=1,
        expected_audit_checksum=current_audit.content_checksum(),
        reviewer_id="teacher_1",
        decision="reject",
        final_total_score=current_audit.total_score,
        criterion_overrides=[],
        teacher_comment="Reject this score and wait for a rescore.",
        submitted_at=workflow_support.NOW + timedelta(minutes=1),
    )

    result = coordinator.review_assessment(
        paper_id="paper_1",
        review_submission=submission,
        request_id="review_request_reject_1",
        knowledge_bundle=knowledge,
        state_policy_path=tmp_path / "state.json",
        teacher_threshold_policy_path=tmp_path / "teacher.json",
        course_id="course_1",
        class_id="class_1",
    )

    # This is a prospective guard only: the submitted state already consumed
    # the pre-review score. A cross-module M5 compensation protocol is tracked
    # in docs/m7_m9_cross_module_change_request.md.
    assert len(store.state_history) == state_history_count
    assert result["recomputed_state_result"] == submitted["state_result"]
    assert result["review_decision"].teacher_comment == submission.teacher_comment
    assert (
        result["reviewed_scoring_result"]
        .get_audit_record("audit_attempt_1")
        .review_status
        == "rejected_pending_rescore"
    )
    assert result["refreshed_analytics"].class_report.score_statistics == {
        "audit_count": 0.0,
        "score_total": 0.0,
    }
    assert result["refreshed_analytics"].class_report.coverage_rate == 0.0
    assert result["refreshed_analytics"].class_report.concept_summaries == []
    assert result["refreshed_analytics"].class_report.misconception_summaries == []
    assert (
        result["refreshed_analytics"].class_report.evidence_status
        == "pending_rescore"
    )
    assert result["refreshed_analytics"].individual_reports == []
    assert result["refreshed_analytics"].review_queue == []
    assert result["refreshed_analytics"].teaching_suggestions == []
    completed = coordinator._m0.get_assessment_run("review:decision_reject_1")
    assert completed.status == "completed"
    assert completed.state_version == submitted[
        "state_result"
    ].learner_state_snapshot.state_version
    assert completed.report_id == result["refreshed_analytics"].report_id
