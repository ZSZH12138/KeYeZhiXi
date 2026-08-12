"""Small, explicit M5/M8 contract factories used by focused tests."""

from __future__ import annotations

from datetime import datetime, timezone

from course_insight.contracts.assessment import (
    AssessmentPaper,
    CriterionScore,
    ItemInstance,
    PaperSection,
    RemediationPlan,
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.events import LearningEvent
from course_insight.contracts.knowledge import (
    AssessmentBlueprint,
    BlueprintSection,
    ItemCard,
    KnowledgeBundle,
    KnowledgeConcept,
    QMatrixEntry,
    ReviewPolicy,
    Rubric,
    RubricCriterion,
)
from course_insight.contracts.platform import AssessmentSubmission


UTC_TIME = datetime(2026, 8, 12, 4, 0, tzinfo=timezone.utc)


def make_rubric(*, version: str = "1.0.0", rubric_id: str = "rubric_1") -> Rubric:
    return Rubric(
        rubric_id=rubric_id,
        version=version,
        total_score=1.0,
        criteria=[
            RubricCriterion(
                criterion_id="criterion_1",
                description="Shows the required reasoning.",
                max_score=1.0,
                expected_student_evidence="working",
                course_evidence_ids=["evidence_1"],
            )
        ],
        review_policy=ReviewPolicy(
            low_confidence_threshold=0.5,
            double_score_disagreement_threshold=0.25,
            require_evidence_for_positive_score=True,
        ),
        status="teacher_approved",
    )


def make_paper(
    *,
    instance_id: str = "random-instance-88",
    item_id: str = "item_2",
    concept_ids: list[str] | None = None,
    subjective: bool = False,
) -> AssessmentPaper:
    instance = ItemInstance(
        item_instance_id=instance_id,
        item_id=item_id,
        item_version="1.0.0",
        stem="Question",
        parameters={},
        concept_ids=["concept_2"] if concept_ids is None else concept_ids,
        rubric_id="rubric_1" if subjective else None,
        max_score=1.0,
        source_evidence_ids=["evidence_1"],
    )
    payload = {
        "paper_id": "paper_1",
        "task_id": "task_1",
        "blueprint_id": "blueprint_1",
        "blueprint_version": "1.0.0",
        "learner_id": "learner_1",
        "sections": [
            PaperSection(
                section_id="section_1",
                name="Section",
                items=[instance],
                score=1.0,
            )
        ],
        "generated_at": UTC_TIME,
        "immutable_checksum": "pending",
    }
    candidate = AssessmentPaper(**payload)
    return AssessmentPaper(**{**payload, "immutable_checksum": candidate.freeze()})


def make_scoring_bundle(
    paper: AssessmentPaper,
    *,
    score: float = 1.0,
    audit_version: int = 1,
) -> ScoringResultBundle:
    instance = paper.all_items()[0]
    audit = ScoreAuditRecord(
        audit_id=f"audit_attempt_1_{instance.item_instance_id}",
        audit_version=audit_version,
        attempt_id="attempt_1",
        item_instance_id=instance.item_instance_id,
        criterion_scores=[
            CriterionScore(
                criterion_id=f"objective_{instance.item_id}",
                score=score,
                student_evidence="answer" if score > 0.0 else "",
                course_evidence_id="evidence_1",
                reason="Test score.",
            )
        ],
        total_score=score,
        max_score=instance.max_score,
        confidence=1.0,
        scoring_method="rule",
        review_status="not_required",
        review_reason=[],
        created_at=UTC_TIME,
    )
    event = LearningEvent(
        event_id="event_attempt_1_scored",
        event_type="assessment_scored",
        course_id="course_1",
        class_id="class_1",
        learner_id=paper.learner_id,
        attempt_id="attempt_1",
        payload={"paper_id": paper.paper_id},
        occurred_at=UTC_TIME,
    )
    return ScoringResultBundle(
        attempt_id="attempt_1",
        paper_id=paper.paper_id,
        learner_id=paper.learner_id,
        score_audit_records=[audit],
        learning_events=[event],
        remediation_plan=RemediationPlan(
            plan_id="remediation_attempt_1",
            based_on_attempt_id="attempt_1",
            learner_id=paper.learner_id,
            targets=[],
            created_at=UTC_TIME,
        ),
        total_score=score,
        max_score=instance.max_score,
        finalized_at=UTC_TIME,
    )


def make_knowledge_bundle(
    *,
    rubric_version: str = "1.0.0",
    subjective: bool = True,
) -> KnowledgeBundle:
    rubric = make_rubric(version=rubric_version)
    item = ItemCard(
        item_id="item_2",
        version="1.0.0",
        stem="Question",
        item_type="short_answer" if subjective else "multiple_choice",
        concept_ids=["concept_2"],
        misconception_ids=[],
        difficulty_level=2,
        cognitive_level="apply",
        parameter_rules=[],
        answer_key={} if subjective else {"answer": "yes", "max_score": 1.0},
        rubric_id="rubric_1" if subjective else None,
        source_evidence_ids=["evidence_1"],
        status="teacher_approved",
    )
    section = BlueprintSection(
        section_id="section_1",
        name="Section",
        item_count=1,
        score=1.0,
        item_types=[],
        concept_weights={"concept_2": 1.0},
        difficulty_range=(0, 5),
        anchor_item_ids=[],
    )
    return KnowledgeBundle(
        knowledge_bundle_id="kb_1",
        course_package_id="cp_1",
        course_id="course_1",
        bundle_version=f"bundle-{rubric_version}",
        concepts=[
            KnowledgeConcept(
                concept_id="concept_2",
                name="Concept 2",
                chapter_id="chapter_1",
                description="Description",
                aliases=[],
                status="published",
            )
        ],
        prerequisite_relations=[],
        misconception_tags=[],
        items=[item],
        rubrics=[rubric],
        blueprints=[
            AssessmentBlueprint(
                blueprint_id="blueprint_1",
                version="1.0.0",
                course_id="course_1",
                sections=[section],
                total_score=1.0,
                duration_minutes=30,
                status="teacher_approved",
            )
        ],
        q_matrix=[
            QMatrixEntry(
                item_id="item_2",
                item_version="1.0.0",
                concept_id="concept_2",
                weight=1.0,
            )
        ],
        status="published",
        published_at=UTC_TIME,
    )


def make_submission(paper: AssessmentPaper) -> AssessmentSubmission:
    return AssessmentSubmission(
        submission_id="submission_1",
        attempt_id="attempt_1",
        paper_id=paper.paper_id,
        learner_id=paper.learner_id,
        answers={paper.all_items()[0].item_instance_id: "working"},
        submitted_at=UTC_TIME,
    )
