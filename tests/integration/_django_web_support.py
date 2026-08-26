from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from django.contrib.auth.models import Group, Permission

from course_insight.contracts.analytics import (
    ClassReport,
    IndividualReport,
    TeacherAnalyticsBundle,
)
from course_insight.contracts.assessment import (
    AssessmentPaper,
    CriterionScore,
    ItemInstance,
    PaperSection,
    RemediationPlan,
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceIndexRef
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.platform import (
    AssessmentSubmission,
    TeacherReviewSubmission,
)
from course_insight.contracts.tutoring import (
    EvidenceCitation,
    StudentFeedbackPackage,
)
from course_insight.modules.m0_platform.django_app.models import (
    ActorGrant,
    User,
)

from tests.integration.test_web_workflow_persistence import NOW


APP_LABEL = "m0_platform_web"


def make_user(
    *,
    actor_id: str,
    role: str,
    permissions: tuple[str, ...],
    course_id: str = "course_1",
    class_id: str = "class_1",
    password: str = "correct-horse-battery-staple",
) -> User:
    user = User.objects.create_user(
        username=actor_id,
        actor_id=actor_id,
        password=password,
    )
    group, _ = Group.objects.get_or_create(name=role)
    group.permissions.add(
        *Permission.objects.filter(
            content_type__app_label=APP_LABEL,
            codename__in=permissions,
        )
    )
    user.groups.add(group)
    ActorGrant.objects.create(
        user=user,
        role=role,
        course_id=course_id,
        class_id=class_id,
        source_checksum="a" * 64,
    )
    return user


def paper_for(actor_id: str) -> AssessmentPaper:
    item = ItemInstance(
        item_instance_id="instance_1",
        item_id="item_1",
        item_version="1.0.0",
        stem="Choose the governed answer.",
        parameters={"answer_type": "str"},
        concept_ids=["concept_1"],
        rubric_id=None,
        max_score=1.0,
        source_evidence_ids=["evidence_1"],
    )
    paper = AssessmentPaper(
        paper_id="paper_1",
        task_id="task_1",
        blueprint_id="blueprint_1",
        blueprint_version="1.0.0",
        learner_id=actor_id,
        sections=[
            PaperSection(
                section_id="section_1",
                name="Objective",
                items=[item],
                score=1.0,
            )
        ],
        generated_at=NOW,
        immutable_checksum="pending",
    )
    return paper.model_copy(
        update={"immutable_checksum": paper.freeze()},
        deep=True,
    )


def knowledge_bundle() -> KnowledgeBundle:
    return KnowledgeBundle(
        knowledge_bundle_id="bundle_1",
        course_package_id="package_1",
        course_id="course_1",
        bundle_version="1.0.0",
        concepts=[],
        prerequisite_relations=[],
        misconception_tags=[],
        items=[],
        rubrics=[],
        blueprints=[],
        q_matrix=[],
        status="published",
        published_at=NOW,
    )


def evidence_index() -> EvidenceIndexRef:
    return EvidenceIndexRef(
        index_id="index_1",
        course_package_id="package_1",
        index_version="1.0.0",
        storage_ref="lexical:index_1",
        backend="lexical",
        embedding_model_id=None,
        source_count=1,
        chunk_count=1,
        built_at=NOW,
        checksum="checksum_1",
        status="ready",
    )


def scoring_for(
    actor_id: str,
    *,
    attempt_id: str = "attempt_1",
    audit_version: int = 1,
) -> ScoringResultBundle:
    audits = [
        ScoreAuditRecord(
            audit_id="audit_1",
            audit_version=version,
            attempt_id=attempt_id,
            item_instance_id="instance_1",
            criterion_scores=[
                CriterionScore(
                    criterion_id="objective_item_1",
                    score=1.0,
                    student_evidence="governed response",
                    course_evidence_id="evidence_1",
                    reason="Matches the governed answer.",
                )
            ],
            total_score=1.0,
            max_score=1.0,
            confidence=0.5,
            scoring_method=(
                "rule" if version == 1 else "teacher_override"
            ),
            review_status=(
                "pending" if version == audit_version else "approved"
            ),
            review_reason=["teacher_review"],
            created_at=NOW,
        )
        for version in range(1, audit_version + 1)
    ]
    return ScoringResultBundle(
        attempt_id=attempt_id,
        paper_id="paper_1",
        learner_id=actor_id,
        score_audit_records=audits,
        learning_events=[],
        remediation_plan=RemediationPlan(
            plan_id=f"remediation_{attempt_id}",
            based_on_attempt_id=attempt_id,
            learner_id=actor_id,
            targets=[],
            created_at=NOW,
        ),
        total_score=1.0,
        max_score=1.0,
        finalized_at=NOW,
    )


def feedback_for(actor_id: str) -> StudentFeedbackPackage:
    return StudentFeedbackPackage(
        feedback_id="feedback_1",
        task_id="task_1",
        learner_id=actor_id,
        message="Review the cited course evidence.",
        rubric_feedback=[],
        missing_concept_ids=["concept_1"],
        evidence_citations=[
            EvidenceCitation(
                evidence_id="evidence_1",
                source_id="source_1",
                locator="p.1",
                quote="Governed evidence.",
            )
        ],
        next_practice_item_ids=["item_2"],
        confidence=1.0,
        generated_at=NOW,
    )


def analytics_for(actor_id: str) -> TeacherAnalyticsBundle:
    return TeacherAnalyticsBundle(
        report_id="report_1",
        class_report=ClassReport(
            class_id="class_1",
            coverage_rate=1.0,
            concept_summaries=[],
            misconception_summaries=[],
            score_statistics={"mean": 1.0},
            evidence_status="sufficient",
        ),
        individual_reports=[
            IndividualReport(
                learner_id=actor_id,
                overall_mastery=1.0,
                weak_concept_ids=[],
                active_misconception_ids=[],
                recent_score=1.0,
                review_required_count=1,
            )
        ],
        review_queue=[],
        teaching_suggestions=[],
        generated_at=NOW,
    )


class FakeCoordinator:
    def __init__(self, actor_id: str) -> None:
        self.paper = paper_for(actor_id)
        self.scoring = scoring_for(actor_id)
        self.feedback = feedback_for(actor_id)
        self.analytics = analytics_for(actor_id)
        self.submission: AssessmentSubmission | None = None
        self.review_submission: TeacherReviewSubmission | None = None
        self.rescore_submission: AssessmentSubmission | None = None
        self.rescore_audit_id: str | None = None
        self.raise_context: DomainError | None = None
        self.start_request: dict[str, object] | None = None

    def start_assessment(self, **kwargs):
        assert kwargs["learner_id"] == self.paper.learner_id
        self.start_request = dict(kwargs)
        return {"assessment_paper": self.paper.model_copy(deep=True)}

    def get_pending_assessment(self, **kwargs):
        if self.submission is not None:
            raise DomainError(
                code="ASSESSMENT_ALREADY_SUBMITTED",
                module="application",
                message="already complete",
                recoverable=True,
            )
        if kwargs["learner_id"] != self.paper.learner_id:
            raise DomainError(
                code="ASSESSMENT_NOT_FOUND",
                module="application",
                message="not found",
            )
        return {"assessment_paper": self.paper.model_copy(deep=True)}

    def submit_assessment(self, **kwargs):
        submission = kwargs["assessment_submission"]
        assert type(submission) is AssessmentSubmission
        self.submission = submission.model_copy(deep=True)
        self.scoring = scoring_for(
            self.paper.learner_id,
            attempt_id=submission.attempt_id,
        )
        return {}

    def get_student_assessment(self, **kwargs):
        if kwargs["learner_id"] != self.paper.learner_id:
            raise DomainError(
                code="ASSESSMENT_NOT_FOUND",
                module="application",
                message="not found",
            )
        return {
            "assessment_paper": self.paper.model_copy(deep=True),
            "scoring_result": self.scoring.model_copy(deep=True),
            "feedback": self.feedback.model_copy(deep=True),
        }

    def get_teacher_review_context(self, **kwargs):
        if self.raise_context is not None:
            raise self.raise_context
        payload = {
            "assessment_paper": self.paper.model_copy(deep=True),
            "scoring_result": self.scoring.model_copy(deep=True),
            "analytics": self.analytics.model_copy(deep=True),
        }
        if self.scoring.has_rejected_score():
            payload["waiting_status"] = "awaiting_rescore"
        elif self.scoring.requires_teacher_review():
            payload["waiting_status"] = "awaiting_review"
        return payload

    def frozen_assessment_submission(self, attempt_id: str):
        if self.submission is None or self.submission.attempt_id != attempt_id:
            raise DomainError(
                code="RESCORE_INPUT_UNAVAILABLE",
                module="application",
                message="frozen original answers are unavailable",
                recoverable=True,
            )
        return self.submission.model_copy(deep=True)

    def rescore_assessment(self, **kwargs):
        submission = kwargs["assessment_submission"]
        assert type(submission) is AssessmentSubmission
        self.rescore_submission = submission.model_copy(deep=True)
        self.rescore_audit_id = str(kwargs["audit_id"])
        return {"waiting_status": "awaiting_review"}

    def review_assessment(self, **kwargs):
        submission = kwargs["review_submission"]
        assert type(submission) is TeacherReviewSubmission
        self.review_submission = submission.model_copy(deep=True)
        return {}

    def apply_suggestion_decision(self, **kwargs):
        suggestion_id = str(kwargs["suggestion_id"])
        decision = str(kwargs["decision"])
        content = kwargs.get("content")
        updated = []
        for item in self.analytics.teaching_suggestions:
            if item.suggestion_id != suggestion_id:
                updated.append(item)
                continue
            payload = {"status": decision}
            if content:
                payload["content"] = content
            updated.append(item.model_copy(update=payload))
        self.analytics = self.analytics.model_copy(
            update={"teaching_suggestions": updated}
        )
        return self.analytics.model_copy(deep=True)


class FakeWebRuntime:
    def __init__(
        self,
        *,
        coordinator: FakeCoordinator,
        runtime_dir: Path,
    ) -> None:
        course = SimpleNamespace(
            course_id="course_1",
            course_context=SimpleNamespace(
                knowledge_bundle=knowledge_bundle(),
                evidence_index_ref=evidence_index(),
            ),
            state_policy_path=runtime_dir / "state.json",
            teacher_threshold_policy_path=runtime_dir / "teacher.json",
        )
        self.courses = {"course_1": course}
        self.container = SimpleNamespace(
            coordinator=coordinator,
            settings=SimpleNamespace(
                runtime_dir=runtime_dir,
                web=SimpleNamespace(session_timeout_seconds=3600),
                outbox=SimpleNamespace(
                    heartbeat_interval_seconds=10.0,
                    poll_interval_seconds=1.0,
                    lease_seconds=30.0,
                ),
            ),
            m0_service=SimpleNamespace(
                health_check=lambda: {
                    "config": "ok",
                    "database": "ok",
                    "runtime": "ok",
                }
            ),
        )

    def require_course(self, course_id: str):
        try:
            return self.courses[course_id]
        except KeyError:
            raise DomainError(
                code="RUNTIME_CONTEXT_UNAVAILABLE",
                module="m0",
                message="not ready",
            ) from None

    @staticmethod
    def logging_is_ready() -> bool:
        return True
