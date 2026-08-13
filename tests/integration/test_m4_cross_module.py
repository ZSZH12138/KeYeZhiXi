from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from course_insight.application.coordinator import AppCoordinator
from course_insight.contracts.assessment import RemediationPlan, ScoringResultBundle
from course_insight.contracts.evidence import EvidenceIndexRef
from course_insight.contracts.knowledge import (
    AssessmentBlueprint,
    BlueprintSection,
    ItemCard,
    KnowledgeBundle,
    KnowledgeConcept,
    QMatrixEntry,
)
from course_insight.contracts.state import (
    ClassStateSnapshot,
    ConceptState,
    DiagnosisResult,
    ItemDiagnosis,
    LearnerStateSnapshot,
    StateUpdateResult,
)
from course_insight.contracts.tasking import TaskPlan
from course_insight.infrastructure.sqlite.m4_repository import SQLiteM4Repository
from course_insight.modules.m4_task_orchestration.identity import (
    canonical_idempotency_key,
)
from course_insight.modules.m4_task_orchestration.service import (
    M4TaskOrchestrationService,
)
from course_insight.modules.m6_tutoring_fsm.stubs import (
    M6TutoringControlServiceStub,
)
from tests.factories.m5_m8 import make_m8_test_service


NOW = datetime(2026, 7, 21, tzinfo=timezone.utc)


def _blueprint(blueprint_id: str, section_id: str) -> AssessmentBlueprint:
    return AssessmentBlueprint(
        blueprint_id=blueprint_id,
        version="1.0.0",
        course_id="course_1",
        sections=[
            BlueprintSection(
                section_id=section_id,
                name="Governed objective section",
                item_count=1,
                score=1.0,
                item_types=["true_false"],
                concept_weights={"concept_1": 1.0},
                difficulty_range=(1, 1),
                anchor_item_ids=["item_1"],
            )
        ],
        total_score=1.0,
        duration_minutes=30,
        status="teacher_approved",
    )


def _knowledge_bundle() -> KnowledgeBundle:
    return KnowledgeBundle(
        knowledge_bundle_id="bundle_1",
        course_package_id="package_authoritative",
        course_id="course_1",
        bundle_version="1.0.0",
        concepts=[
            KnowledgeConcept(
                concept_id="concept_1",
                name="Governed concept",
                chapter_id="chapter_1",
                description="A deterministic integration-test concept.",
                aliases=[],
                status="published",
            )
        ],
        prerequisite_relations=[],
        misconception_tags=[],
        items=[
            ItemCard(
                item_id="item_1",
                version="1.0.0",
                stem="The governed statement is true.",
                item_type="true_false",
                concept_ids=["concept_1"],
                misconception_ids=[],
                difficulty_level=1,
                cognitive_level="remember",
                parameter_rules=[],
                answer_key={"answer": True, "max_score": 1.0},
                rubric_id=None,
                source_evidence_ids=["evidence_1"],
                status="teacher_approved",
            )
        ],
        rubrics=[],
        blueprints=[
            _blueprint("blueprint_other", "section_other"),
            _blueprint("blueprint_target", "section_target"),
        ],
        q_matrix=[
            QMatrixEntry(
                item_id="item_1",
                item_version="1.0.0",
                concept_id="concept_1",
                weight=1.0,
            )
        ],
        status="published",
        published_at=NOW,
    )


def _m4_service(database_path: Path) -> M4TaskOrchestrationService:
    repository = SQLiteM4Repository(database_path)
    repository.initialize()
    return M4TaskOrchestrationService(
        repository,
        canonical_idempotency_key,
        blueprint_by_task_type={"stage_assessment": "blueprint_target"},
    )


def _create_stage_plan(service: M4TaskOrchestrationService) -> TaskPlan:
    return service.create_task_plan(
        student_text="请开始阶段测评",
        task_type_hint="stage_assessment",
        course_id="course_1",
        class_id="class_1",
        learner_id="pseudonym_learner",
        session_id="session_1",
        knowledge_bundle=_knowledge_bundle(),
        learner_state_snapshot=None,
    )


def _scoring_result() -> ScoringResultBundle:
    return ScoringResultBundle(
        attempt_id="attempt_1",
        paper_id="paper_1",
        learner_id="pseudonym_learner",
        score_audit_records=[],
        learning_events=[],
        remediation_plan=RemediationPlan(
            plan_id="remediation_1",
            based_on_attempt_id="attempt_1",
            learner_id="pseudonym_learner",
            targets=[],
            created_at=NOW,
        ),
        total_score=0.0,
        max_score=0.0,
        finalized_at=NOW,
    )


def _state_update() -> StateUpdateResult:
    diagnosis = DiagnosisResult(
        diagnosis_id="diagnosis_1",
        attempt_id="attempt_1",
        learner_id="pseudonym_learner",
        item_diagnoses=[
            ItemDiagnosis(
                item_instance_id="item_instance_1",
                concept_ids=["concept_1"],
                misconception_ids=[],
                error_type="none",
                confidence=1.0,
                evidence_audit_ids=["audit_1"],
                prerequisite_gap_ids=[],
            )
        ],
        priority_concept_ids=["concept_1"],
        priority_misconception_ids=[],
        generated_at=NOW,
    )
    learner = LearnerStateSnapshot(
        snapshot_id="learner_snapshot_1",
        course_id="course_1",
        class_id="class_1",
        learner_id="pseudonym_learner",
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
        course_id="course_1",
        class_id="class_1",
        aggregation_policy_version="1.0.0",
        scope={"course_id": "course_1", "class_id": "class_1"},
        class_size=1,
        assessed_count=1,
        coverage_rate=1.0,
        concept_status=[],
        misconception_summary=[],
        evidence_status="sufficient",
        updated_at=NOW,
    )
    return StateUpdateResult(
        diagnosis_result=diagnosis,
        learner_state_snapshot=learner,
        class_state_snapshot=class_state,
        processed_audit_ids=["audit_1"],
        updated_at=NOW,
    )


def test_m3_bundle_flows_through_m4_directly_into_m8(tmp_path: Path) -> None:
    bundle = _knowledge_bundle()
    task_plan = _m4_service(tmp_path / "m4.sqlite3").create_task_plan(
        student_text="start stage assessment",
        task_type_hint="stage_assessment",
        course_id="course_1",
        class_id="class_1",
        learner_id="pseudonym_learner",
        session_id="session_1",
        knowledge_bundle=bundle,
        learner_state_snapshot=None,
    )

    paper = make_m8_test_service().generate_paper(
        task_plan=task_plan,
        knowledge_bundle=bundle,
        learner_state_snapshot=None,
        diagnosis_result=None,
    )

    assert isinstance(task_plan, TaskPlan)
    assert task_plan.blueprint_id == "blueprint_target"
    assert task_plan.course_package_id == bundle.course_package_id
    assert paper.task_id == task_plan.task_id
    assert paper.blueprint_id == task_plan.blueprint_id


def test_m4_course_package_identity_flows_through_m6_to_m2_query(
    tmp_path: Path,
) -> None:
    task_plan = _create_stage_plan(_m4_service(tmp_path / "m4.sqlite3"))

    tutoring = M6TutoringControlServiceStub().decide_next_action(
        task_plan=task_plan,
        scoring_result_bundle=_scoring_result(),
        state_update_result=_state_update(),
        previous_session_state_snapshot=None,
    )

    assert (
        task_plan.course_package_id
        == tutoring.evidence_query.course_package_id
        == "package_authoritative"
    )


class _StopAfterM4(RuntimeError):
    pass


class _RecordingM8:
    def __init__(self) -> None:
        self.received_task_plan: TaskPlan | None = None

    def generate_paper(
        self,
        task_plan: TaskPlan,
        knowledge_bundle: KnowledgeBundle,
        learner_state_snapshot: LearnerStateSnapshot | None,
        diagnosis_result: DiagnosisResult | None,
    ) -> None:
        self.received_task_plan = task_plan
        raise _StopAfterM4


def test_app_coordinator_passes_typed_m4_plan_directly_to_m8(
    tmp_path: Path,
) -> None:
    m8 = _RecordingM8()
    unused_dependency = cast(Any, object())
    coordinator = AppCoordinator(
        m0_service=unused_dependency,
        m1_service=unused_dependency,
        m2_service=unused_dependency,
        m3_service=unused_dependency,
        m4_service=_m4_service(tmp_path / "m4.sqlite3"),
        m5_service=unused_dependency,
        m6_service=unused_dependency,
        m7_service=unused_dependency,
        m8_service=cast(Any, m8),
        m9_service=unused_dependency,
    )

    with pytest.raises(_StopAfterM4):
        coordinator.run_assessment_cycle(
            index_ref=cast(EvidenceIndexRef, object()),
            knowledge_bundle=_knowledge_bundle(),
            student_text="start stage assessment",
            task_type_hint="stage_assessment",
            course_id="course_1",
            class_id="class_1",
            learner_id="pseudonym_learner",
            session_id="session_1",
            raw_answer_path=tmp_path / "unused-answer.json",
            state_policy_path=tmp_path / "unused-state-policy.json",
            teacher_threshold_policy_path=tmp_path / "unused-thresholds.json",
        )

    assert isinstance(m8.received_task_plan, TaskPlan)
    assert m8.received_task_plan.blueprint_id == "blueprint_target"
