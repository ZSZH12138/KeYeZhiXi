from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from course_insight.application.coordinator import AppCoordinator
from course_insight.contracts.assessment import (
    CriterionScore,
    RemediationPlan,
    RemediationTarget,
    RubricScoringResult,
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.course import (
    ContentChunk,
    CoursePackage,
    SourceDocument,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.events import LearningEvent
from course_insight.contracts.evidence import (
    EvidenceBundle,
    EvidenceChunk,
    EvidenceIndexRef,
    EvidenceQuery,
)
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.state import (
    ClassStateSnapshot,
    ConceptState,
    DiagnosisResult,
    ItemDiagnosis,
    LearnerStateSnapshot,
    StateUpdateResult,
)
from course_insight.contracts.tasking import TaskPlan
from course_insight.contracts.tutoring import (
    FeedbackGenerationTask,
    TutoringControlResult,
)
from course_insight.modules.m2_evidence_retrieval.stubs import (
    M2EvidenceRetrievalServiceStub,
)
from course_insight.modules.m6_tutoring_fsm.service import (
    M6TutoringControlService,
)
from course_insight.modules.m6_tutoring_fsm.state_machine import (
    DEFAULT_STATE_MACHINE,
)
from course_insight.modules.m6_tutoring_fsm.stubs import (
    M6TutoringControlServiceStub,
)
from course_insight.modules.m7_local_model.stubs import M7LocalModelServiceStub


NOW = datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc)
COURSE_ID = "course_1"
CLASS_ID = "class_1"
LEARNER_ID = "pseudonym_learner"
SESSION_ID = "session_1"
CONCEPT_ID = "concept_1"
COURSE_PACKAGE_ID = "package_authoritative_not_derived_from_bundle"


@dataclass(frozen=True)
class _M6Inputs:
    task_plan: TaskPlan
    scoring_result: ScoringResultBundle
    state_update: StateUpdateResult


def _knowledge_bundle() -> KnowledgeBundle:
    return KnowledgeBundle(
        knowledge_bundle_id="knowledge_bundle_with_unrelated_identity",
        course_package_id=COURSE_PACKAGE_ID,
        course_id=COURSE_ID,
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


def _task_plan() -> TaskPlan:
    return TaskPlan(
        task_id="task_1",
        task_type="stage_assessment",
        course_id=COURSE_ID,
        class_id=CLASS_ID,
        learner_id=LEARNER_ID,
        session_id=SESSION_ID,
        blueprint_id="blueprint_1",
        knowledge_bundle_id="knowledge_bundle_with_unrelated_identity",
        course_package_id=COURSE_PACKAGE_ID,
        workflow=["M8", "M2", "M7", "M5", "M6", "M9"],
        next_module="M8",
        created_at=NOW,
    )


def _course_package() -> CoursePackage:
    return CoursePackage(
        course_package_id=COURSE_PACKAGE_ID,
        course_id=COURSE_ID,
        package_version="1.0.0",
        source_documents=[
            SourceDocument(
                source_id="source_1",
                file_name="governed-course.txt",
                media_type="text/plain",
                sha256="source_checksum",
                page_count=None,
                title="Governed course source",
                version="1.0.0",
            )
        ],
        content_chunks=[
            ContentChunk(
                chunk_id="chunk_1",
                source_id="source_1",
                text="A governed rule, distinction, and example for this concept.",
                locator="section-1",
                concept_hints=[CONCEPT_ID],
                sha256="chunk_checksum",
            )
        ],
        source_authorizations=[],
        imported_at=NOW,
        status="ready",
        checksum="package_checksum",
    )


def _learning_event(
    *,
    audit_id: str,
    occurred_at: datetime,
) -> LearningEvent:
    return LearningEvent(
        event_id=f"event_{audit_id}",
        event_type="assessment_scored",
        course_id=COURSE_ID,
        class_id=CLASS_ID,
        learner_id=LEARNER_ID,
        attempt_id="attempt_1",
        payload={
            "paper_id": "paper_1",
            "total_score": 0.0,
            "max_score": 1.0,
        },
        occurred_at=occurred_at,
    )


def _score_audit(audit_id: str, version: int) -> ScoreAuditRecord:
    return ScoreAuditRecord(
        audit_id=audit_id,
        audit_version=version,
        attempt_id="attempt_1",
        item_instance_id="item_instance_1",
        criterion_scores=[],
        total_score=0.0,
        max_score=1.0,
        confidence=1.0,
        scoring_method="rule" if version == 1 else "teacher_override",
        review_status="approved",
        review_reason=[],
        created_at=NOW + timedelta(minutes=version - 1),
    )


def _scoring_result(
    *,
    audit_id: str = "audit_1",
    audit_version: int = 1,
    finalized_at: datetime = NOW,
) -> ScoringResultBundle:
    return ScoringResultBundle(
        attempt_id="attempt_1",
        paper_id="paper_1",
        learner_id=LEARNER_ID,
        score_audit_records=[
            _score_audit(audit_id, version)
            for version in range(1, audit_version + 1)
        ],
        learning_events=[
            _learning_event(audit_id=audit_id, occurred_at=finalized_at)
        ],
        remediation_plan=RemediationPlan(
            plan_id=f"remediation_{audit_id}_{audit_version}",
            based_on_attempt_id="attempt_1",
            learner_id=LEARNER_ID,
            targets=[
                RemediationTarget(
                    concept_id=CONCEPT_ID,
                    misconception_id=None,
                    priority=1,
                    recommended_item_ids=["item_1"],
                    reason="The governed score identifies this concept.",
                )
            ],
            created_at=finalized_at,
        ),
        total_score=0.0,
        max_score=1.0,
        finalized_at=finalized_at,
    )


def _concept_state(
    concept_id: str,
    *,
    mastery_probability: float = 0.5,
    updated_at: datetime = NOW,
) -> ConceptState:
    return ConceptState(
        concept_id=concept_id,
        mastery_probability=mastery_probability,
        mastery_confidence=1.0,
        misconceptions=[],
        hint_dependency=0.0,
        recent_correction_rate=1.0,
        evidence_count=1,
        updated_at=updated_at,
    )


def _state_update(
    *,
    audit_id: str = "audit_1",
    audit_version: int = 1,
    state_version: int = 1,
    processed_audit_ids: list[str] | None = None,
    diagnosis: DiagnosisResult | None = None,
    concept_states: list[ConceptState] | None = None,
    updated_at: datetime = NOW,
) -> StateUpdateResult:
    audit_identity = f"{audit_id}:{audit_version}"
    resolved_diagnosis = diagnosis or DiagnosisResult(
        diagnosis_id=f"diagnosis_{audit_id}_{audit_version}",
        attempt_id="attempt_1",
        learner_id=LEARNER_ID,
        item_diagnoses=[
            ItemDiagnosis(
                item_instance_id="item_instance_1",
                concept_ids=[CONCEPT_ID],
                misconception_ids=[],
                error_type="needs_support",
                confidence=1.0,
                evidence_audit_ids=[audit_identity],
                prerequisite_gap_ids=[],
            )
        ],
        priority_concept_ids=[CONCEPT_ID],
        priority_misconception_ids=[],
        generated_at=updated_at,
    )
    learner = LearnerStateSnapshot(
        snapshot_id=f"learner_snapshot_{state_version}",
        course_id=COURSE_ID,
        class_id=CLASS_ID,
        learner_id=LEARNER_ID,
        state_version=state_version,
        concept_states=concept_states or [_concept_state(CONCEPT_ID, updated_at=updated_at)],
        overall_mastery=0.5,
        evidence_count=state_version,
        updated_at=updated_at,
    )
    class_state = ClassStateSnapshot(
        snapshot_id=f"class_snapshot_{state_version}",
        course_id=COURSE_ID,
        class_id=CLASS_ID,
        aggregation_policy_version="1.0.0",
        scope={"course_id": COURSE_ID, "class_id": CLASS_ID},
        class_size=1,
        assessed_count=1,
        coverage_rate=1.0,
        concept_status=[],
        misconception_summary=[],
        evidence_status="sufficient",
        updated_at=updated_at,
    )
    return StateUpdateResult(
        diagnosis_result=resolved_diagnosis,
        learner_state_snapshot=learner,
        class_state_snapshot=class_state,
        processed_audit_ids=(
            list(processed_audit_ids)
            if processed_audit_ids is not None
            else [
                f"{audit_id}:{version}"
                for version in range(1, audit_version + 1)
            ]
        ),
        updated_at=updated_at,
    )


def _complete_inputs() -> _M6Inputs:
    return _M6Inputs(
        task_plan=_task_plan(),
        scoring_result=_scoring_result(),
        state_update=_state_update(),
    )


def _decide(
    inputs: _M6Inputs,
    *,
    service: M6TutoringControlService | None = None,
) -> TutoringControlResult:
    controller = service or M6TutoringControlServiceStub()
    return controller.decide_next_action(
        task_plan=inputs.task_plan,
        scoring_result_bundle=inputs.scoring_result,
        state_update_result=inputs.state_update,
        previous_session_state_snapshot=None,
    )


def test_complete_m4_m8_m5_fixture_flows_through_m6_m2_and_m7(
    tmp_path: Path,
) -> None:
    inputs = _complete_inputs()

    tutoring = _decide(inputs)
    m2 = M2EvidenceRetrievalServiceStub(tmp_path / "m2-index")
    index_ref = m2.build_index(_course_package())
    evidence = m2.retrieve(tutoring.evidence_query, index_ref)
    feedback = M7LocalModelServiceStub().generate_student_feedback(
        tutoring.feedback_generation_task,
        evidence,
    )

    assert isinstance(tutoring, TutoringControlResult)
    assert isinstance(tutoring.evidence_query, EvidenceQuery)
    assert isinstance(
        tutoring.feedback_generation_task,
        FeedbackGenerationTask,
    )
    tutoring.assert_query_alignment()
    assert tutoring.evidence_query.course_package_id == (
        inputs.task_plan.course_package_id
    )
    assert tutoring.evidence_query.course_package_id == COURSE_PACKAGE_ID
    assert tutoring.evidence_query.concept_ids == (
        tutoring.teaching_action.target_concept_ids
    )
    assert tutoring.feedback_generation_task.evidence_query_id == (
        tutoring.evidence_query.query_id
    )
    assert tutoring.feedback_generation_task.task_id == inputs.task_plan.task_id
    assert tutoring.feedback_generation_task.learner_state_snapshot_id == (
        inputs.state_update.learner_state_snapshot.snapshot_id
    )
    assert tutoring.feedback_generation_task.score_summary == {
        "score": inputs.scoring_result.total_score,
        "max_score": inputs.scoring_result.max_score,
    }
    diagnosed = {
        concept_id
        for item in inputs.state_update.diagnosis_result.item_diagnoses
        for concept_id in item.concept_ids
    }
    assert set(tutoring.teaching_action.target_concept_ids) <= diagnosed
    assert evidence.query_id == tutoring.evidence_query.query_id
    assert not evidence.is_empty()
    assert feedback.task_id == inputs.task_plan.task_id
    assert feedback.learner_id == inputs.task_plan.learner_id
    assert feedback.citation_ids() == evidence.citation_ids()
    assert feedback.safe_for_student()


def test_m6_rejects_when_all_candidate_targets_lack_diagnosis_support() -> None:
    inputs = _complete_inputs()
    unsupported_concept = "concept_not_supported_by_current_diagnosis"
    diagnosis = DiagnosisResult(
        diagnosis_id="diagnosis_without_candidate_support",
        attempt_id="attempt_1",
        learner_id=LEARNER_ID,
        item_diagnoses=[
            ItemDiagnosis(
                item_instance_id="item_instance_1",
                concept_ids=[CONCEPT_ID],
                misconception_ids=[],
                error_type="none",
                confidence=1.0,
                evidence_audit_ids=["audit_1:1"],
                prerequisite_gap_ids=[],
            )
        ],
        priority_concept_ids=[],
        priority_misconception_ids=[],
        generated_at=NOW,
    )
    scoring = inputs.scoring_result.model_copy(
        update={
            "remediation_plan": RemediationPlan(
                plan_id="remediation_without_diagnosis_support",
                based_on_attempt_id="attempt_1",
                learner_id=LEARNER_ID,
                targets=[
                    RemediationTarget(
                        concept_id=unsupported_concept,
                        misconception_id=None,
                        priority=1,
                        recommended_item_ids=[],
                        reason="This deliberately lacks current diagnosis support.",
                    )
                ],
                created_at=NOW,
            )
        },
        deep=True,
    )
    state = _state_update(
        diagnosis=diagnosis,
        concept_states=[
            _concept_state(unsupported_concept, mastery_probability=0.1),
            _concept_state(CONCEPT_ID, mastery_probability=1.0),
        ],
    )

    with pytest.raises(DomainError) as captured:
        _decide(
            _M6Inputs(
                task_plan=inputs.task_plan,
                scoring_result=scoring,
                state_update=state,
            )
        )

    assert captured.value.code == "TUTORING_REFERENCE_MISMATCH"


def test_m6_rejects_latest_scoring_audit_version_missing_from_m5_watermark() -> None:
    inputs = _complete_inputs()
    scoring = _scoring_result(audit_version=2, finalized_at=NOW + timedelta(minutes=1))
    stale_state = _state_update(
        audit_version=1,
        state_version=2,
        processed_audit_ids=["audit_1:1"],
        updated_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(DomainError) as captured:
        _decide(
            _M6Inputs(
                task_plan=inputs.task_plan,
                scoring_result=scoring,
                state_update=stale_state,
            )
        )

    assert captured.value.code == "TUTORING_REFERENCE_MISMATCH"


@pytest.mark.parametrize(
    ("identity", "conflicting_value"),
    [
        ("course_id", "course_other"),
        ("class_id", "class_other"),
        ("learner_id", "learner_other"),
        ("paper_id", "paper_other"),
    ],
)
def test_m6_rejects_scoring_learning_event_identity_conflicts(
    identity: str,
    conflicting_value: str,
) -> None:
    inputs = _complete_inputs()
    event = inputs.scoring_result.learning_events[0]
    event_update: dict[str, Any]
    if identity == "paper_id":
        event_update = {
            "payload": {**event.payload, "paper_id": conflicting_value}
        }
    else:
        event_update = {identity: conflicting_value}
    conflicting_event = event.model_copy(update=event_update, deep=True)
    # M8 already rejects learner conflicts during normal construction. This
    # deliberate, fully populated boundary poison verifies M6 still validates
    # every event identity instead of trusting an upstream object blindly.
    conflicting_scoring = inputs.scoring_result.model_copy(
        update={"learning_events": [conflicting_event]},
        deep=True,
    )

    with pytest.raises(DomainError) as captured:
        _decide(
            _M6Inputs(
                task_plan=inputs.task_plan,
                scoring_result=conflicting_scoring,
                state_update=inputs.state_update,
            )
        )

    assert captured.value.code == "TUTORING_REFERENCE_MISMATCH"


def _sqlite_m6_repository(database_path: Path) -> Any:
    try:
        repository_module = importlib.import_module(
            "course_insight.infrastructure.sqlite.m6_repository"
        )
    except ImportError as error:
        pytest.fail(f"SQLiteM6Repository is unavailable: {error}")
    repository_type = getattr(repository_module, "SQLiteM6Repository", None)
    if repository_type is None:
        pytest.fail("SQLiteM6Repository is unavailable from the M6 SQLite module")
    repository = repository_type(database_path)
    repository.initialize()
    return repository


class _StopAfterM6(RuntimeError):
    pass


class _FixedM4:
    def __init__(self, task_plan: TaskPlan) -> None:
        self._task_plan = task_plan

    def create_task_plan(self, **_: Any) -> TaskPlan:
        return self._task_plan.model_copy(deep=True)


class _CoordinatorPreparation:
    def __init__(self) -> None:
        self.rubric_scoring_tasks = [_CoordinatorScoringTask()]

    @staticmethod
    def query_for_task(scoring_task_id: str) -> EvidenceQuery:
        assert scoring_task_id == "coordinator_scoring_task"
        return EvidenceQuery(
            query_id="coordinator_grading_query",
            course_package_id=COURSE_PACKAGE_ID,
            query_text="Retrieve governed evidence for scoring.",
            concept_ids=[CONCEPT_ID],
            item_id="item_1",
            use_case="grading",
            top_k=1,
            min_relevance=0.2,
        )


class _CoordinatorScoringTask:
    scoring_task_id = "coordinator_scoring_task"


class _FixedM8:
    def __init__(self, scoring_result: ScoringResultBundle) -> None:
        self._scoring_result = scoring_result

    @staticmethod
    def generate_paper(**_: Any) -> str:
        return "complete-paper-fixture-owned-by-the-test-double"

    @staticmethod
    def prepare_scoring(**_: Any) -> _CoordinatorPreparation:
        return _CoordinatorPreparation()

    def finalize_scoring(self, **_: Any) -> ScoringResultBundle:
        return self._scoring_result.model_copy(deep=True)


class _CoordinatorM2:
    @staticmethod
    def retrieve(
        evidence_query: EvidenceQuery,
        evidence_index_ref: EvidenceIndexRef,
    ) -> EvidenceBundle:
        if evidence_query.use_case == "feedback":
            raise _StopAfterM6
        return EvidenceBundle(
            query_id=evidence_query.query_id,
            index_id=evidence_index_ref.index_id,
            course_id=COURSE_ID,
            evidence_chunks=[
                EvidenceChunk(
                    evidence_id="evidence_chunk_1",
                    source_id="source_1",
                    chunk_id="chunk_1",
                    text="Governed scoring evidence.",
                    locator="section-1",
                    concept_ids=[CONCEPT_ID],
                    relevance=1.0,
                    checksum="chunk_checksum",
                )
            ],
            retrieved_at=NOW,
        )


class _CoordinatorM7:
    @staticmethod
    def score_subjective_answer(**_: Any) -> RubricScoringResult:
        return RubricScoringResult(
            scoring_task_id="coordinator_scoring_task",
            criterion_scores=[
                CriterionScore(
                    criterion_id="criterion_1",
                    score=0.0,
                    student_evidence="",
                    course_evidence_id=None,
                    reason="No unsupported credit is awarded.",
                )
            ],
            total_score=0.0,
            confidence=1.0,
            missing_concept_ids=[CONCEPT_ID],
            review_flags=[],
            model_name="deterministic-test-double",
            model_version="1.0.0",
            scored_at=NOW,
        )


class _FixedM5:
    def __init__(self, state_update: StateUpdateResult) -> None:
        self._state_update = state_update

    def update_state(self, **_: Any) -> StateUpdateResult:
        return self._state_update.model_copy(deep=True)


class _RecordingM6:
    def __init__(self, delegate: M6TutoringControlService) -> None:
        self._delegate = delegate
        self.previous_arguments: tuple[Any, ...] = ()
        self.results: tuple[TutoringControlResult, ...] = ()

    def decide_next_action(self, **kwargs: Any) -> TutoringControlResult:
        self.previous_arguments = (
            *self.previous_arguments,
            kwargs["previous_session_state_snapshot"],
        )
        result = self._delegate.decide_next_action(**kwargs)
        self.results = (*self.results, result)
        return result


class _NoOpM0:
    @staticmethod
    def append_learning_events(**_: Any) -> None:
        return None


class _UnusedService:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unexpected coordinator dependency call: {name}")


def _ready_index_ref() -> EvidenceIndexRef:
    return EvidenceIndexRef(
        index_id="coordinator_index",
        course_package_id=COURSE_PACKAGE_ID,
        index_version="1.0.0",
        storage_ref="lexical:coordinator_index",
        backend="lexical",
        embedding_model_id=None,
        source_count=1,
        chunk_count=1,
        built_at=NOW,
        checksum="index_checksum",
        status="ready",
    )


def _coordinator(
    *,
    task_plan: TaskPlan,
    scoring_result: ScoringResultBundle,
    state_update: StateUpdateResult,
    m6: _RecordingM6,
) -> AppCoordinator:
    unused = _UnusedService()
    return AppCoordinator(
        m0_service=_NoOpM0(),
        m1_service=unused,
        m2_service=_CoordinatorM2(),
        m3_service=unused,
        m4_service=_FixedM4(task_plan),
        m5_service=_FixedM5(state_update),
        m6_service=m6,
        m7_service=_CoordinatorM7(),
        m8_service=_FixedM8(scoring_result),
        m9_service=unused,
    )


def _run_coordinator_until_after_m6(
    coordinator: AppCoordinator,
    *,
    tmp_path: Path,
) -> None:
    with pytest.raises(_StopAfterM6):
        coordinator.run_assessment_cycle(
            index_ref=_ready_index_ref(),
            knowledge_bundle=_knowledge_bundle(),
            student_text="请开始阶段测评",
            task_type_hint="stage_assessment",
            course_id=COURSE_ID,
            class_id=CLASS_ID,
            learner_id=LEARNER_ID,
            session_id=SESSION_ID,
            raw_answer_path=tmp_path / "unused-answer.json",
            state_policy_path=tmp_path / "unused-state-policy.json",
            teacher_threshold_policy_path=tmp_path / "unused-threshold.json",
        )


def test_coordinator_none_allows_restarted_m6_to_restore_repository_session(
    tmp_path: Path,
) -> None:
    task_plan = _task_plan()
    database_path = tmp_path / "m6.sqlite3"
    first_repository = _sqlite_m6_repository(database_path)
    first_m6 = _RecordingM6(
        M6TutoringControlService(DEFAULT_STATE_MACHINE, first_repository)
    )
    _run_coordinator_until_after_m6(
        _coordinator(
            task_plan=task_plan,
            scoring_result=_scoring_result(audit_id="audit_1"),
            state_update=_state_update(audit_id="audit_1", state_version=1),
            m6=first_m6,
        ),
        tmp_path=tmp_path,
    )

    restarted_repository = _sqlite_m6_repository(database_path)
    restarted_m6 = _RecordingM6(
        M6TutoringControlService(DEFAULT_STATE_MACHINE, restarted_repository)
    )
    second_time = NOW + timedelta(minutes=2)
    _run_coordinator_until_after_m6(
        _coordinator(
            task_plan=task_plan,
            scoring_result=_scoring_result(
                audit_id="audit_2",
                finalized_at=second_time,
            ),
            state_update=_state_update(
                audit_id="audit_2",
                state_version=2,
                updated_at=second_time,
            ),
            m6=restarted_m6,
        ),
        tmp_path=tmp_path,
    )

    assert first_m6.previous_arguments == (None,)
    assert restarted_m6.previous_arguments == (None,)
    assert len(first_m6.results) == 1
    assert len(restarted_m6.results) == 1
    first_result = first_m6.results[0]
    restarted_result = restarted_m6.results[0]
    assert first_result.session_state_snapshot.turn_count == 1
    assert restarted_result.session_state_snapshot.turn_count == 2
    assert restarted_result.teaching_action.state_before == (
        first_result.session_state_snapshot.current_state
    )
    assert restarted_result.session_state_snapshot.completed_action_ids[:-1] == (
        first_result.session_state_snapshot.completed_action_ids
    )
