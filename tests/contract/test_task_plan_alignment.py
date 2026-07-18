from __future__ import annotations

from datetime import datetime, timezone

from course_insight.contracts.assessment import ScoringResultBundle
from course_insight.contracts.knowledge import AssessmentBlueprint, KnowledgeBundle
from course_insight.contracts.state import (
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
    scoring = ScoringResultBundle.model_construct(
        learner_id=plan.learner_id,
        total_score=0.0,
        max_score=1.0,
        score_audit_records=[],
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
    learner = LearnerStateSnapshot.model_construct(
        snapshot_id="snapshot_1",
        learner_id=plan.learner_id,
        course_id=plan.course_id,
        class_id=plan.class_id,
        concept_states=[ConceptState.model_construct(concept_id="concept_1")],
    )
    state = StateUpdateResult.model_construct(
        diagnosis_result=diagnosis,
        learner_state_snapshot=learner,
    )

    result = M6TutoringControlServiceStub().decide_next_action(
        task_plan=plan,
        scoring_result_bundle=scoring,
        state_update_result=state,
        previous_session_state_snapshot=None,
    )

    assert result.evidence_query.course_package_id == plan.course_package_id
    assert result.evidence_query.course_package_id == "authoritative_package_identifier"
