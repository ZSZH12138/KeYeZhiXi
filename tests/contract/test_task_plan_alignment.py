from __future__ import annotations

from datetime import datetime, timezone

import pytest

from course_insight.contracts.assessment import (
    RemediationPlan,
    RemediationTarget,
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.events import LearningEvent
from course_insight.contracts.knowledge import AssessmentBlueprint, KnowledgeBundle
from course_insight.contracts.state import (
    ClassStateSnapshot,
    ConceptState,
    DiagnosisResult,
    ItemDiagnosis,
    LearnerStateSnapshot,
    StateUpdateResult,
)
from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m4_task_orchestration.stubs import (
    M4TaskOrchestrationServiceStub,
)
from course_insight.modules.m6_tutoring_fsm.stubs import M6TutoringControlServiceStub


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def _task_plan(
    *,
    task_type: str = "qa",
    blueprint_id: str | None = None,
    workflow: list[str] | None = None,
    next_module: str = "M2",
) -> TaskPlan:
    return TaskPlan(
        task_id="task_1",
        task_type=task_type,
        course_id="course_1",
        class_id="class_1",
        learner_id="pseudonym_learner",
        session_id="session_1",
        blueprint_id=blueprint_id,
        knowledge_bundle_id="bundle_1",
        course_package_id="package_1",
        workflow=["M2", "M7", "M6"] if workflow is None else workflow,
        next_module=next_module,
        created_at=NOW,
    )


def test_task_plan_rejects_noncanonical_assessment_workflow() -> None:
    with pytest.raises(DomainError) as captured:
        _task_plan(
            task_type="stage_assessment",
            blueprint_id="blueprint_1",
            workflow=["M8", "M5"],
            next_module="M8",
        )

    assert captured.value.code == "WORKFLOW_MISMATCH"


def test_task_plan_rejects_next_module_other_than_workflow_head() -> None:
    with pytest.raises(DomainError) as captured:
        _task_plan(next_module="M7")

    assert captured.value.code == "NEXT_MODULE_NOT_ALLOWED"


def test_task_plan_rejects_blueprint_for_qa() -> None:
    with pytest.raises(DomainError) as captured:
        _task_plan(blueprint_id="blueprint_1")

    assert captured.value.code == "BLUEPRINT_NOT_ALLOWED"


def _knowledge_bundle() -> KnowledgeBundle:
    return KnowledgeBundle.model_construct(
        knowledge_bundle_id="independent_bundle_identifier",
        course_package_id="authoritative_package_identifier",
        course_id="course_1",
        status="published",
        blueprints=[
            AssessmentBlueprint.model_construct(
                blueprint_id="blueprint_1",
                course_id="course_1",
                status="teacher_approved",
            )
        ],
    )


def _assessment_plan() -> TaskPlan:
    return M4TaskOrchestrationServiceStub().create_task_plan(
        student_text="start assessment",
        task_type_hint="stage_assessment",
        course_id="course_1",
        class_id="class_1",
        learner_id="pseudonym_learner",
        session_id="session_1",
        knowledge_bundle=_knowledge_bundle(),
        learner_state_snapshot=None,
    )


def test_m4_task_plan_carries_authoritative_course_package_id() -> None:
    plan = _assessment_plan()

    assert plan.course_package_id == "authoritative_package_identifier"


def test_assessment_workflow_lists_distinct_modules_by_first_participation() -> None:
    plan = _assessment_plan()

    assert plan.workflow == ["M8", "M2", "M7", "M5", "M6", "M9"]


def test_m6_uses_task_plan_course_package_id_without_string_derivation() -> None:
    plan = _assessment_plan()
    scoring = ScoringResultBundle(
        attempt_id="attempt_1",
        paper_id="paper_1",
        learner_id=plan.learner_id,
        score_audit_records=[
            ScoreAuditRecord(
                audit_id="audit_1",
                audit_version=1,
                attempt_id="attempt_1",
                item_instance_id="instance_1",
                criterion_scores=[],
                total_score=0.0,
                max_score=1.0,
                confidence=1.0,
                scoring_method="rule",
                review_status="completed",
                review_reason=[],
                created_at=NOW,
            )
        ],
        learning_events=[
            LearningEvent(
                event_id="event_1",
                event_type="assessment_scored",
                course_id=plan.course_id,
                class_id=plan.class_id,
                learner_id=plan.learner_id,
                attempt_id="attempt_1",
                payload={"paper_id": "paper_1"},
                occurred_at=NOW,
            )
        ],
        remediation_plan=RemediationPlan(
            plan_id="remediation_1",
            based_on_attempt_id="attempt_1",
            learner_id=plan.learner_id,
            targets=[
                RemediationTarget(
                    concept_id="concept_1",
                    misconception_id=None,
                    priority=1,
                    recommended_item_ids=[],
                    reason="The governed diagnosis selects this concept.",
                )
            ],
            created_at=NOW,
        ),
        total_score=0.0,
        max_score=1.0,
        finalized_at=NOW,
    )
    diagnosis = DiagnosisResult(
        diagnosis_id="diagnosis_1",
        attempt_id="attempt_1",
        learner_id=plan.learner_id,
        item_diagnoses=[
            ItemDiagnosis(
                item_instance_id="instance_1",
                concept_ids=["concept_1"],
                misconception_ids=[],
                error_type="needs_practice",
                confidence=1.0,
                evidence_audit_ids=["audit_1:1"],
                prerequisite_gap_ids=[],
            )
        ],
        priority_concept_ids=["concept_1"],
        priority_misconception_ids=[],
        generated_at=NOW,
    )
    learner = LearnerStateSnapshot(
        snapshot_id="snapshot_1",
        learner_id=plan.learner_id,
        course_id=plan.course_id,
        class_id=plan.class_id,
        state_version=1,
        concept_states=[
            ConceptState(
                concept_id="concept_1",
                mastery_probability=0.5,
                mastery_confidence=1.0,
                misconceptions=[],
                hint_dependency=0.0,
                recent_correction_rate=1.0,
                evidence_count=1,
                updated_at=NOW,
            )
        ],
        overall_mastery=0.5,
        evidence_count=1,
        updated_at=NOW,
    )
    class_state = ClassStateSnapshot(
        snapshot_id="class_snapshot_1",
        course_id=plan.course_id,
        class_id=plan.class_id,
        aggregation_policy_version="1.0.0",
        scope={"course_id": plan.course_id, "class_id": plan.class_id},
        class_size=1,
        assessed_count=1,
        coverage_rate=1.0,
        concept_status=[],
        misconception_summary=[],
        evidence_status="sufficient",
        updated_at=NOW,
    )
    state = StateUpdateResult(
        diagnosis_result=diagnosis,
        learner_state_snapshot=learner,
        class_state_snapshot=class_state,
        processed_audit_ids=["audit_1:1"],
        updated_at=NOW,
    )

    result = M6TutoringControlServiceStub().decide_next_action(
        task_plan=plan,
        scoring_result_bundle=scoring,
        state_update_result=state,
        previous_session_state_snapshot=None,
    )

    assert result.evidence_query.course_package_id == plan.course_package_id
    assert result.evidence_query.course_package_id == "authoritative_package_identifier"
