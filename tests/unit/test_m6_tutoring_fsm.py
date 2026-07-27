from __future__ import annotations

import itertools
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from course_insight.contracts.assessment import (
    CriterionScore,
    RemediationPlan,
    RemediationTarget,
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.events import LearningEvent
from course_insight.contracts.state import (
    ClassStateSnapshot,
    ConceptState,
    DiagnosisResult,
    ItemDiagnosis,
    LearnerStateSnapshot,
    MisconceptionStrength,
    StateUpdateResult,
)
from course_insight.contracts.tasking import TaskPlan
from course_insight.contracts.tutoring import SessionStateSnapshot
from course_insight.modules.m6_tutoring_fsm.repository import (
    InMemoryM6Repository,
)
from course_insight.modules.m6_tutoring_fsm.policy_artifacts import (
    LoadedPolicyArtifact,
)
from course_insight.modules.m6_tutoring_fsm.policy_gate import (
    ActivePolicyGate,
    PolicyGateConfig,
)
from course_insight.modules.m6_tutoring_fsm.policy_runtime import (
    PolicyRuntime,
    PolicyRuntimeGateInputs,
)
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    PolicyArtifactManifest,
    PolicyDecision,
    PolicyPrediction,
)
from course_insight.modules.m6_tutoring_fsm.identity import request_fingerprint
from course_insight.modules.m6_tutoring_fsm.service import (
    M6TutoringControlService,
)
from course_insight.modules.m6_tutoring_fsm.state_machine import (
    DEFAULT_STATE_MACHINE,
)
from course_insight.modules.m6_tutoring_fsm.stubs import (
    M6TutoringControlServiceStub,
)


NOW = datetime(2026, 7, 22, 9, 30, tzinfo=timezone.utc)
COURSE_ID = "course-pseudonymous"
CLASS_ID = "class-pseudonymous"
LEARNER_ID = "learner-pseudonymous"
SESSION_ID = "session-pseudonymous"
ATTEMPT_ID = "attempt-pseudonymous"
PAPER_ID = "paper-pseudonymous"
AUDIT_ID = "audit-pseudonymous"
CORE_CONCEPT = "concept-core"
SENSITIVE_ANSWER = "FINAL_ANSWER: 42-sensitive-student-response"

STATES = ("S0", "S1", "S2", "S3", "S4", "S5")
LEGAL_TRANSITIONS = frozenset(
    {
        ("S0", "S1"),
        ("S1", "S2"),
        ("S1", "S3"),
        ("S2", "S3"),
        ("S3", "S4"),
        ("S4", "S2"),
        ("S4", "S3"),
        ("S4", "S5"),
    }
)
STATE_PAIRS = tuple(itertools.product(STATES, repeat=2))


def _concept_state(
    concept_id: str,
    *,
    mastery_probability: float = 0.9,
    mastery_confidence: float = 0.7,
    hint_dependency: float = 0.0,
    recent_correction_rate: float = 0.9,
    misconceptions: tuple[tuple[str, float], ...] = (),
) -> ConceptState:
    return ConceptState(
        concept_id=concept_id,
        mastery_probability=mastery_probability,
        mastery_confidence=mastery_confidence,
        misconceptions=[
            MisconceptionStrength(
                misconception_id=misconception_id,
                strength=strength,
                evidence_count=1,
                last_seen_at=NOW,
            )
            for misconception_id, strength in misconceptions
        ],
        hint_dependency=hint_dependency,
        recent_correction_rate=recent_correction_rate,
        evidence_count=1,
        updated_at=NOW,
    )


def _diagnosis(
    *,
    learner_id: str = LEARNER_ID,
    attempt_id: str = ATTEMPT_ID,
    item_concepts: tuple[str, ...] = (CORE_CONCEPT,),
    priority_concepts: tuple[str, ...] = (CORE_CONCEPT,),
    prerequisite_gaps: tuple[str, ...] = (),
    misconception_ids: tuple[str, ...] = (),
    priority_misconceptions: tuple[str, ...] = (),
    evidence_audit_ids: tuple[str, ...] = (f"{AUDIT_ID}:1",),
) -> DiagnosisResult:
    return DiagnosisResult(
        diagnosis_id=f"diagnosis-{attempt_id}",
        attempt_id=attempt_id,
        learner_id=learner_id,
        item_diagnoses=[
            ItemDiagnosis(
                item_instance_id="item-instance-1",
                concept_ids=list(item_concepts),
                misconception_ids=list(misconception_ids),
                error_type="conceptual_error" if misconception_ids else "needs_practice",
                confidence=0.9,
                evidence_audit_ids=list(evidence_audit_ids),
                prerequisite_gap_ids=list(prerequisite_gaps),
            )
        ],
        priority_concept_ids=list(priority_concepts),
        priority_misconception_ids=list(priority_misconceptions),
        generated_at=NOW,
    )


def _remediation_plan(
    concepts: tuple[tuple[str, int], ...] = (),
    *,
    learner_id: str = LEARNER_ID,
    attempt_id: str = ATTEMPT_ID,
) -> RemediationPlan:
    return RemediationPlan(
        plan_id=f"remediation-{attempt_id}",
        based_on_attempt_id=attempt_id,
        learner_id=learner_id,
        targets=[
            RemediationTarget(
                concept_id=concept_id,
                misconception_id=None,
                priority=priority,
                recommended_item_ids=[],
                reason="deterministic remediation target",
            )
            for concept_id, priority in concepts
        ],
        created_at=NOW,
    )


def _scoring_bundle(
    *,
    learner_id: str = LEARNER_ID,
    attempt_id: str = ATTEMPT_ID,
    paper_id: str = PAPER_ID,
    audit_versions: tuple[int, ...] = (1,),
    needs_teacher_review: bool = False,
    remediation_concepts: tuple[tuple[str, int], ...] = ((CORE_CONCEPT, 1),),
    event_course_id: str = COURSE_ID,
    event_class_id: str = CLASS_ID,
    event_paper_id: str | None = None,
    student_evidence: str = SENSITIVE_ANSWER,
) -> ScoringResultBundle:
    records = [
        ScoreAuditRecord(
            audit_id=AUDIT_ID,
            audit_version=version,
            attempt_id=attempt_id,
            item_instance_id="item-instance-1",
            criterion_scores=[
                CriterionScore(
                    criterion_id="criterion-1",
                    score=0.0,
                    student_evidence=student_evidence,
                    course_evidence_id=None,
                    reason="No credit awarded by the governed scorer.",
                )
            ],
            total_score=0.0,
            max_score=1.0,
            confidence=0.95,
            scoring_method="rule" if version == 1 else "teacher_override",
            review_status="pending" if needs_teacher_review else "completed",
            review_reason=["teacher_review_required"] if needs_teacher_review else [],
            created_at=NOW + timedelta(minutes=version),
        )
        for version in audit_versions
    ]
    event = LearningEvent(
        event_id=f"event-{attempt_id}-{max(audit_versions)}",
        event_type="assessment_scored",
        course_id=event_course_id,
        class_id=event_class_id,
        learner_id=learner_id,
        attempt_id=attempt_id,
        payload={"paper_id": event_paper_id or paper_id},
        occurred_at=NOW,
    )
    return ScoringResultBundle(
        attempt_id=attempt_id,
        paper_id=paper_id,
        learner_id=learner_id,
        score_audit_records=records,
        learning_events=[event],
        remediation_plan=_remediation_plan(
            remediation_concepts,
            learner_id=learner_id,
            attempt_id=attempt_id,
        ),
        total_score=0.0,
        max_score=1.0,
        finalized_at=NOW + timedelta(minutes=max(audit_versions)),
    )


def _state_update(
    *,
    learner_id: str = LEARNER_ID,
    course_id: str = COURSE_ID,
    class_id: str = CLASS_ID,
    attempt_id: str = ATTEMPT_ID,
    state_version: int = 1,
    concept_states: tuple[ConceptState, ...] | None = None,
    item_concepts: tuple[str, ...] = (CORE_CONCEPT,),
    priority_concepts: tuple[str, ...] = (CORE_CONCEPT,),
    prerequisite_gaps: tuple[str, ...] = (),
    misconception_ids: tuple[str, ...] = (),
    priority_misconceptions: tuple[str, ...] = (),
    evidence_audit_ids: tuple[str, ...] = (f"{AUDIT_ID}:1",),
    processed_audit_ids: tuple[str, ...] | None = None,
) -> StateUpdateResult:
    resolved_states = concept_states or (_concept_state(CORE_CONCEPT),)
    diagnosis = _diagnosis(
        learner_id=learner_id,
        attempt_id=attempt_id,
        item_concepts=item_concepts,
        priority_concepts=priority_concepts,
        prerequisite_gaps=prerequisite_gaps,
        misconception_ids=misconception_ids,
        priority_misconceptions=priority_misconceptions,
        evidence_audit_ids=evidence_audit_ids,
    )
    learner = LearnerStateSnapshot(
        snapshot_id=f"learner-snapshot-{state_version}",
        course_id=course_id,
        class_id=class_id,
        learner_id=learner_id,
        state_version=state_version,
        concept_states=list(resolved_states),
        overall_mastery=(
            sum(state.mastery_probability for state in resolved_states)
            / len(resolved_states)
        ),
        evidence_count=sum(state.evidence_count for state in resolved_states),
        updated_at=NOW + timedelta(minutes=state_version),
    )
    class_state = ClassStateSnapshot(
        snapshot_id=f"class-snapshot-{state_version}",
        course_id=course_id,
        class_id=class_id,
        aggregation_policy_version="m5-test-v1",
        scope={"course_id": course_id, "class_id": class_id},
        class_size=1,
        assessed_count=1,
        coverage_rate=1.0,
        concept_status=[],
        misconception_summary=[],
        evidence_status="sufficient",
        updated_at=NOW + timedelta(minutes=state_version),
    )
    return StateUpdateResult(
        diagnosis_result=diagnosis,
        learner_state_snapshot=learner,
        class_state_snapshot=class_state,
        processed_audit_ids=list(processed_audit_ids or evidence_audit_ids),
        updated_at=NOW + timedelta(minutes=state_version),
    )


def _task_plan(
    *,
    task_id: str = "task-pseudonymous",
    learner_id: str = LEARNER_ID,
    course_id: str = COURSE_ID,
    class_id: str = CLASS_ID,
    session_id: str = SESSION_ID,
) -> TaskPlan:
    return TaskPlan(
        task_id=task_id,
        task_type="practice",
        course_id=course_id,
        class_id=class_id,
        learner_id=learner_id,
        session_id=session_id,
        blueprint_id="blueprint-pseudonymous",
        knowledge_bundle_id="knowledge-bundle-pseudonymous",
        course_package_id="course-package-pseudonymous",
        workflow=["M8", "M2", "M7", "M5", "M6", "M9"],
        next_module="M8",
        created_at=NOW,
    )


def _session(
    current_state: str,
    *,
    session_id: str = SESSION_ID,
    turn_count: int | None = None,
) -> SessionStateSnapshot:
    resolved_turn = (0 if current_state == "S0" else 1) if turn_count is None else turn_count
    return SessionStateSnapshot(
        session_id=session_id,
        current_state=current_state,
        turn_count=resolved_turn,
        completed_action_ids=[
            f"historical-action-{index + 1}" for index in range(resolved_turn)
        ],
        updated_at=NOW,
    )


def _valid_inputs(
    *,
    concept_id: str = CORE_CONCEPT,
    needs_teacher_review: bool = False,
) -> tuple[TaskPlan, ScoringResultBundle, StateUpdateResult]:
    return (
        _task_plan(),
        _scoring_bundle(
            needs_teacher_review=needs_teacher_review,
            remediation_concepts=((concept_id, 1),),
        ),
        _state_update(
            concept_states=(_concept_state(concept_id),),
            item_concepts=(concept_id,),
            priority_concepts=(concept_id,),
        ),
    )


def test_service_rejects_legacy_task_that_omits_m6_before_repository_access(
) -> None:
    class TrackingRepository(InMemoryM6Repository):
        def __init__(self) -> None:
            super().__init__()
            self.request_lookup_count = 0

        def get_decision_by_request(self, request_fingerprint: str) -> Any:
            self.request_lookup_count += 1
            return super().get_decision_by_request(request_fingerprint)

    task, scoring, state = _valid_inputs()
    legacy_task = task.model_copy(
        update={"workflow": ["M8", "M2", "M7", "M5", "M9"]},
        deep=True,
    )
    repository = TrackingRepository()
    service = M6TutoringControlService(DEFAULT_STATE_MACHINE, repository)

    with pytest.raises(DomainError) as raised:
        service.decide_next_action(legacy_task, scoring, state, None)

    assert raised.value.code == "MODULE_NOT_ALLOWED"
    assert raised.value.module == "m4"
    assert raised.value.details == {"module_name": "M6"}
    assert repository.request_lookup_count == 0
    assert repository.get_latest_session_state(legacy_task.session_id) is None


def _decision_policy_types() -> tuple[type[Any], type[Any]]:
    from course_insight.modules.m6_tutoring_fsm.decision_policy import (
        DecisionSignals,
        M6DecisionPolicy,
    )

    return DecisionSignals, M6DecisionPolicy


def _signals(**overrides: Any) -> Any:
    decision_signals, _ = _decision_policy_types()
    values = {
        "needs_teacher_review": False,
        "has_diagnosed_misconception": False,
        "has_active_misconception": False,
        "has_prerequisite_gap": False,
        "has_new_evidence": True,
        "minimum_recent_correction_rate": 0.8,
        "minimum_mastery_confidence": 0.6,
        "maximum_hint_dependency": 0.0,
    }
    return decision_signals(**{**values, **overrides})


@pytest.mark.parametrize(
    ("current_state", "next_state"),
    STATE_PAIRS,
    ids=[f"{source}-to-{target}" for source, target in STATE_PAIRS],
)
def test_state_machine_enforces_every_directed_state_pair(
    current_state: str,
    next_state: str,
) -> None:
    if (current_state, next_state) in LEGAL_TRANSITIONS:
        DEFAULT_STATE_MACHINE.validate_transition(current_state, next_state)
        return

    with pytest.raises(DomainError) as raised:
        DEFAULT_STATE_MACHINE.validate_transition(current_state, next_state)

    assert raised.value.code == "INVALID_STATE_TRANSITION"


def test_state_machine_rejects_unknown_state() -> None:
    with pytest.raises(DomainError) as raised:
        DEFAULT_STATE_MACHINE.validate_transition("S6", "S1")

    assert raised.value.code == "INVALID_STATE_TRANSITION"


@pytest.mark.parametrize("next_state", STATES)
def test_s5_is_terminal(next_state: str) -> None:
    with pytest.raises(DomainError) as raised:
        DEFAULT_STATE_MACHINE.validate_transition("S5", next_state)

    assert raised.value.code == "INVALID_STATE_TRANSITION"


def test_session_transition_increments_turn_once_without_mutating_history_entries() -> None:
    session = _session("S1")

    session.transition("S3", "new-action")

    assert session.turn_count == 2
    assert session.completed_action_ids == ["historical-action-1", "new-action"]


def test_session_transition_rejects_reused_action_id_atomically() -> None:
    session = _session("S1")
    before = session.model_dump(mode="json")

    with pytest.raises(DomainError) as raised:
        session.transition("S3", "historical-action-1")

    assert raised.value.code == "TUTORING_REFERENCE_CONFLICT"
    assert session.model_dump(mode="json") == before


def test_in_memory_repository_rejects_first_snapshot_after_turn_zero() -> None:
    repository = InMemoryM6Repository()

    with pytest.raises(DomainError) as raised:
        repository.save_session_state(_session("S4", turn_count=2))

    assert raised.value.code == "TUTORING_REFERENCE_MISMATCH"
    assert repository.get_latest_session_state(SESSION_ID) is None


def test_in_memory_repository_rejects_terminal_initial_snapshot() -> None:
    repository = InMemoryM6Repository()

    with pytest.raises(DomainError) as raised:
        repository.save_session_state(_session("S5", turn_count=0))

    assert raised.value.code == "TUTORING_REFERENCE_MISMATCH"
    assert repository.get_latest_session_state(SESSION_ID) is None


def test_in_memory_repository_rejects_illegal_snapshot_transition() -> None:
    repository = InMemoryM6Repository()
    seed = _session("S1", turn_count=0)
    repository.save_session_state(seed)

    with pytest.raises(DomainError) as raised:
        repository.save_session_state(_session("S4", turn_count=1))

    assert raised.value.code == "TUTORING_REFERENCE_MISMATCH"
    assert repository.get_latest_session_state(SESSION_ID) == seed


def test_decision_policy_defaults_are_frozen_and_versioned() -> None:
    decision_signals, decision_policy = _decision_policy_types()
    policy = decision_policy()
    signals = _signals()

    assert policy.policy_version == "m6-deterministic-v1"
    assert policy.weak_mastery_threshold == 0.8
    assert policy.stable_correction_threshold == 0.8
    assert policy.mastery_confidence_threshold == 0.6
    assert policy.maximum_hint_dependency == 0.0
    assert policy.active_misconception_threshold == 0.5
    with pytest.raises(FrozenInstanceError):
        policy.policy_version = "tampered"
    with pytest.raises(FrozenInstanceError):
        signals.has_new_evidence = False
    assert isinstance(signals, decision_signals)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("minimum_recent_correction_rate", float("nan")),
        ("minimum_mastery_confidence", float("inf")),
        ("maximum_hint_dependency", -0.000001),
    ],
)
def test_decision_signals_reject_nonfinite_or_out_of_range_measurements(
    field_name: str,
    invalid_value: float,
) -> None:
    with pytest.raises(ValueError):
        _signals(**{field_name: invalid_value})


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("policy_version", ""),
        ("weak_mastery_threshold", 1.000001),
        ("stable_correction_threshold", float("nan")),
        ("mastery_confidence_threshold", -0.000001),
        ("maximum_hint_dependency", float("inf")),
        ("active_misconception_threshold", -0.000001),
    ],
)
def test_decision_policy_rejects_invalid_version_or_thresholds(
    field_name: str,
    invalid_value: str | float,
) -> None:
    _, decision_policy = _decision_policy_types()

    with pytest.raises(ValueError):
        decision_policy(**{field_name: invalid_value})


POLICY_CASES = (
    ("S0", {}, "S1"),
    ("S1", {}, "S3"),
    ("S1", {"needs_teacher_review": True}, "S2"),
    ("S1", {"has_diagnosed_misconception": True}, "S3"),
    ("S1", {"has_active_misconception": True}, "S2"),
    ("S1", {"has_prerequisite_gap": True}, "S2"),
    ("S2", {"needs_teacher_review": True}, "S3"),
    ("S3", {"has_prerequisite_gap": True}, "S4"),
    ("S4", {"needs_teacher_review": True}, "S2"),
    ("S4", {"has_diagnosed_misconception": True}, "S3"),
    ("S4", {"has_active_misconception": True}, "S2"),
    ("S4", {"has_prerequisite_gap": True}, "S2"),
    ("S4", {"has_new_evidence": False}, "S3"),
    ("S4", {"minimum_recent_correction_rate": 0.799999}, "S3"),
    ("S4", {"minimum_mastery_confidence": 0.599999}, "S3"),
    ("S4", {"maximum_hint_dependency": 0.000001}, "S3"),
    ("S4", {}, "S5"),
)


@pytest.mark.parametrize(
    ("current_state", "overrides", "expected_state"),
    POLICY_CASES,
    ids=[
        f"{current}-{expected}-{index}"
        for index, (current, _overrides, expected) in enumerate(POLICY_CASES)
    ],
)
def test_decision_policy_covers_the_complete_signal_matrix(
    current_state: str,
    overrides: dict[str, Any],
    expected_state: str,
) -> None:
    _, decision_policy = _decision_policy_types()

    selected = decision_policy().decide_next_state(
        current_state,
        _signals(**overrides),
    )

    assert selected == expected_state


@pytest.mark.parametrize("current_state", ["S5", "unknown"])
def test_decision_policy_rejects_terminal_or_unknown_current_state(
    current_state: str,
) -> None:
    _, decision_policy = _decision_policy_types()

    with pytest.raises(DomainError) as raised:
        decision_policy().decide_next_state(current_state, _signals())

    assert raised.value.code == "INVALID_STATE_TRANSITION"


def _select_targets(
    diagnosis: DiagnosisResult,
    remediation: RemediationPlan,
    learner: LearnerStateSnapshot,
) -> list[str]:
    from course_insight.modules.m6_tutoring_fsm.target_selector import (
        select_target_concept_ids,
    )

    return select_target_concept_ids(
        diagnosis_result=diagnosis,
        remediation_plan=remediation,
        learner_state_snapshot=learner,
        weak_mastery_threshold=0.8,
    )


def _learner_snapshot(concept_states: tuple[ConceptState, ...]) -> LearnerStateSnapshot:
    return LearnerStateSnapshot(
        snapshot_id="target-selector-snapshot",
        course_id=COURSE_ID,
        class_id=CLASS_ID,
        learner_id=LEARNER_ID,
        state_version=1,
        concept_states=list(concept_states),
        overall_mastery=(
            sum(state.mastery_probability for state in concept_states)
            / len(concept_states)
        ),
        evidence_count=sum(state.evidence_count for state in concept_states),
        updated_at=NOW,
    )


def test_target_selector_merges_four_sources_in_stable_priority_order() -> None:
    diagnosis = _diagnosis(
        item_concepts=(
            "priority",
            "shared",
            "gap",
            "remediation",
            "weak",
            "low-priority",
        ),
        priority_concepts=("priority", "shared"),
        prerequisite_gaps=("gap", "shared"),
    )
    remediation = _remediation_plan(
        (("remediation", 1), ("shared", 1), ("low-priority", 2))
    )
    learner = _learner_snapshot(
        (
            _concept_state("weak", mastery_probability=0.2),
            _concept_state("shared", mastery_probability=0.2),
            _concept_state("priority"),
            _concept_state("gap"),
            _concept_state("remediation"),
            _concept_state("low-priority"),
        )
    )
    before = (
        diagnosis.model_dump(mode="json"),
        remediation.model_dump(mode="json"),
        learner.model_dump(mode="json"),
    )

    selected = _select_targets(diagnosis, remediation, learner)

    assert selected == ["priority", "shared", "gap", "remediation", "weak"]
    assert (
        diagnosis.model_dump(mode="json"),
        remediation.model_dump(mode="json"),
        learner.model_dump(mode="json"),
    ) == before


def test_target_selector_filters_every_candidate_not_supported_by_diagnosis() -> None:
    diagnosis = _diagnosis(
        item_concepts=("priority", "supported-gap"),
        priority_concepts=("priority",),
        prerequisite_gaps=("supported-gap", "unsupported-gap"),
    )
    remediation = _remediation_plan((("unsupported-remediation", 1),))
    learner = _learner_snapshot(
        (
            _concept_state("priority"),
            _concept_state("supported-gap"),
            _concept_state("unsupported-gap"),
            _concept_state("unsupported-remediation"),
            _concept_state("unsupported-weak", mastery_probability=0.2),
        )
    )

    assert _select_targets(diagnosis, remediation, learner) == [
        "priority",
        "supported-gap",
    ]


def test_target_selector_does_not_treat_threshold_boundary_as_weak() -> None:
    diagnosis = _diagnosis(
        item_concepts=("below-threshold", "at-threshold"),
        priority_concepts=(),
    )
    learner = _learner_snapshot(
        (
            _concept_state("below-threshold", mastery_probability=0.799999),
            _concept_state("at-threshold", mastery_probability=0.8),
        )
    )

    assert _select_targets(diagnosis, _remediation_plan(), learner) == [
        "below-threshold"
    ]


def test_target_selector_rejects_when_no_candidate_has_diagnosis_support() -> None:
    diagnosis = _diagnosis(
        item_concepts=("diagnosed-but-not-candidate",),
        priority_concepts=(),
        prerequisite_gaps=("unsupported-gap",),
    )
    remediation = _remediation_plan((("unsupported-remediation", 1),))
    learner = _learner_snapshot(
        (
            _concept_state("diagnosed-but-not-candidate"),
            _concept_state("unsupported-gap"),
            _concept_state("unsupported-remediation"),
        )
    )

    with pytest.raises(DomainError) as raised:
        _select_targets(diagnosis, remediation, learner)

    assert raised.value.code == "TUTORING_REFERENCE_MISMATCH"


def test_target_selector_rejects_supported_target_without_concept_state() -> None:
    diagnosis = _diagnosis(
        item_concepts=("supported-but-state-missing",),
        priority_concepts=("supported-but-state-missing",),
    )
    learner = _learner_snapshot((_concept_state("unrelated-concept"),))

    with pytest.raises(DomainError) as raised:
        _select_targets(diagnosis, _remediation_plan(), learner)

    assert raised.value.code == "TUTORING_REFERENCE_MISMATCH"


def _decide_for_target_state(target_state: str) -> Any:
    service = M6TutoringControlServiceStub()
    task, scoring, state = _valid_inputs(
        needs_teacher_review=target_state == "S2"
    )
    if target_state == "S5":
        first = service.decide_next_action(
            task_plan=task,
            scoring_result_bundle=scoring,
            state_update_result=state,
            previous_session_state_snapshot=_session("S3"),
        )
        scoring = _scoring_bundle(audit_versions=(1, 2))
        state = _state_update(
            state_version=2,
            evidence_audit_ids=(f"{AUDIT_ID}:2",),
            processed_audit_ids=(f"{AUDIT_ID}:1", f"{AUDIT_ID}:2"),
        )
        return service.decide_next_action(
            task_plan=task,
            scoring_result_bundle=scoring,
            state_update_result=state,
            previous_session_state_snapshot=first.session_state_snapshot,
        )

    source_state = {"S1": "S0", "S2": "S1", "S3": "S1", "S4": "S3"}[
        target_state
    ]
    return service.decide_next_action(
        task_plan=task,
        scoring_result_bundle=scoring,
        state_update_result=state,
        previous_session_state_snapshot=_session(source_state),
    )


ACTION_MAPPING = {
    "S1": ("diagnostic_probe", "m6.s1.diagnostic_probe.v1"),
    "S2": ("minimal_hint", "m6.s2.minimal_hint.v1"),
    "S3": ("guided_question", "m6.s3.guided_question.v1"),
    "S4": ("self_explanation_prompt", "m6.s4.self_explanation.v1"),
    "S5": ("summary_and_transfer", "m6.s5.summary.v1"),
}


@pytest.mark.parametrize(
    ("target_state", "expected_action_type", "expected_template"),
    [
        (state, action_type, template)
        for state, (action_type, template) in ACTION_MAPPING.items()
    ],
)
def test_service_maps_each_target_state_to_versioned_action_template(
    target_state: str,
    expected_action_type: str,
    expected_template: str,
) -> None:
    result = _decide_for_target_state(target_state)
    action = result.teaching_action

    assert result.next_state() == target_state
    assert action.action_type == expected_action_type
    assert action.prompt_template_id == expected_template
    assert action.must_not_reveal_answer is True
    assert "m6-deterministic-v1" in action.reason
    assert target_state in action.reason
    result.assert_query_alignment()


def test_s4_without_a_prior_m6_decision_cannot_claim_new_evidence() -> None:
    task, scoring, state = _valid_inputs()

    result = M6TutoringControlServiceStub().decide_next_action(
        task_plan=task,
        scoring_result_bundle=scoring,
        state_update_result=state,
        previous_session_state_snapshot=_session("S4"),
    )

    assert result.next_state() == "S3"


def test_same_state_version_with_changed_content_is_an_identity_conflict() -> None:
    service = M6TutoringControlServiceStub()
    task, scoring, state = _valid_inputs()
    first = service.decide_next_action(
        task_plan=task,
        scoring_result_bundle=scoring,
        state_update_result=state,
        previous_session_state_snapshot=_session("S3"),
    )
    changed_state = _state_update(
        state_version=1,
        concept_states=(_concept_state(CORE_CONCEPT, mastery_probability=0.95),),
    )

    with pytest.raises(DomainError) as raised:
        service.decide_next_action(
            task_plan=task,
            scoring_result_bundle=scoring,
            state_update_result=changed_state,
            previous_session_state_snapshot=first.session_state_snapshot,
        )

    assert raised.value.code == "TUTORING_REFERENCE_MISMATCH"


def test_same_audit_version_with_changed_scoring_content_is_an_identity_conflict(
) -> None:
    service = M6TutoringControlServiceStub()
    task, scoring, state = _valid_inputs()
    first = service.decide_next_action(
        task_plan=task,
        scoring_result_bundle=scoring,
        state_update_result=state,
        previous_session_state_snapshot=_session("S3"),
    )
    changed_scoring = _scoring_bundle(
        student_evidence="changed evidence under the same audit version",
    )

    with pytest.raises(DomainError) as raised:
        service.decide_next_action(
            task_plan=task,
            scoring_result_bundle=changed_scoring,
            state_update_result=state,
            previous_session_state_snapshot=first.session_state_snapshot,
        )

    assert raised.value.code == "TUTORING_REFERENCE_MISMATCH"
    assert raised.value.details["reason"] == "scoring_evidence_content_conflict"


def test_audit_version_regression_is_an_identity_conflict() -> None:
    service = M6TutoringControlServiceStub()
    task = _task_plan()
    scoring_v2 = _scoring_bundle(audit_versions=(1, 2))
    state_v1 = _state_update(
        evidence_audit_ids=(f"{AUDIT_ID}:2",),
        processed_audit_ids=(f"{AUDIT_ID}:1", f"{AUDIT_ID}:2"),
    )
    first = service.decide_next_action(
        task_plan=task,
        scoring_result_bundle=scoring_v2,
        state_update_result=state_v1,
        previous_session_state_snapshot=_session("S3"),
    )
    scoring_v1 = _scoring_bundle(audit_versions=(1,))
    state_v2 = _state_update(
        state_version=2,
        evidence_audit_ids=(f"{AUDIT_ID}:1",),
        processed_audit_ids=(f"{AUDIT_ID}:1",),
    )

    with pytest.raises(DomainError) as raised:
        service.decide_next_action(
            task_plan=task,
            scoring_result_bundle=scoring_v1,
            state_update_result=state_v2,
            previous_session_state_snapshot=first.session_state_snapshot,
        )

    assert raised.value.code == "TUTORING_REFERENCE_MISMATCH"
    assert raised.value.details["reason"] == "scoring_audit_version_regressed"


def test_nonpriority_diagnosed_misconception_blocks_s4_completion() -> None:
    service = M6TutoringControlServiceStub()
    task = _task_plan()
    scoring_v1 = _scoring_bundle()
    state_v1 = _state_update(misconception_ids=("misconception-1",))
    first = service.decide_next_action(
        task_plan=task,
        scoring_result_bundle=scoring_v1,
        state_update_result=state_v1,
        previous_session_state_snapshot=_session("S3"),
    )
    scoring_v2 = _scoring_bundle(audit_versions=(1, 2))
    state_v2 = _state_update(
        state_version=2,
        misconception_ids=("misconception-1",),
        evidence_audit_ids=(f"{AUDIT_ID}:2",),
        processed_audit_ids=(f"{AUDIT_ID}:1", f"{AUDIT_ID}:2"),
    )

    result = service.decide_next_action(
        task_plan=task,
        scoring_result_bundle=scoring_v2,
        state_update_result=state_v2,
        previous_session_state_snapshot=first.session_state_snapshot,
    )

    assert result.next_state() == "S3"


def test_identical_authoritative_inputs_produce_identical_nonembedded_ids() -> None:
    task, scoring, state = _valid_inputs()
    previous = _session("S1")

    first = M6TutoringControlServiceStub().decide_next_action(
        task,
        scoring,
        state,
        previous,
    )
    second = M6TutoringControlServiceStub().decide_next_action(
        task.model_copy(deep=True),
        scoring.model_copy(deep=True),
        state.model_copy(deep=True),
        previous.model_copy(deep=True),
    )
    first_ids = (
        first.teaching_action.action_id,
        first.evidence_query.query_id,
        first.feedback_generation_task.feedback_task_id,
    )
    second_ids = (
        second.teaching_action.action_id,
        second.evidence_query.query_id,
        second.feedback_generation_task.feedback_task_id,
    )

    assert first_ids == second_ids
    assert len(set(first_ids)) == 3
    assert all(task.task_id not in identifier for identifier in first_ids)
    assert all(task.learner_id not in identifier for identifier in first_ids)


def test_identity_changes_when_authoritative_task_input_changes() -> None:
    task, scoring, state = _valid_inputs()
    changed_task = _task_plan(task_id="a-different-task")

    original = M6TutoringControlServiceStub().decide_next_action(
        task,
        scoring,
        state,
        _session("S1"),
    )
    changed = M6TutoringControlServiceStub().decide_next_action(
        changed_task,
        scoring,
        state,
        _session("S1"),
    )

    assert original.teaching_action.action_id != changed.teaching_action.action_id
    assert original.evidence_query.query_id != changed.evidence_query.query_id
    assert (
        original.feedback_generation_task.feedback_task_id
        != changed.feedback_generation_task.feedback_task_id
    )


def test_action_reason_query_and_feedback_task_do_not_leak_answers_or_identity() -> None:
    concept_id = "concept-do-not-place-in-free-text"
    task, scoring, state = _valid_inputs(concept_id=concept_id)

    result = M6TutoringControlServiceStub().decide_next_action(
        task,
        scoring,
        state,
        None,
    )
    exposed_text = "\n".join(
        [
            result.teaching_action.reason,
            result.evidence_query.query_text,
            json.dumps(
                result.feedback_generation_task.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
            ),
        ]
    )

    assert result.teaching_action.must_not_reveal_answer is True
    assert result.feedback_generation_task.must_hide_answer() is True
    assert SENSITIVE_ANSWER not in exposed_text
    assert "final_answer:" not in exposed_text.casefold()
    assert "标准答案" not in exposed_text
    assert task.learner_id not in result.teaching_action.reason
    assert task.learner_id not in result.evidence_query.query_text
    assert concept_id not in result.evidence_query.query_text


def test_query_text_is_fixed_while_concepts_stay_in_structured_fields() -> None:
    task_a, scoring_a, state_a = _valid_inputs(concept_id="concept-alpha-private")
    task_b, scoring_b, state_b = _valid_inputs(concept_id="concept-beta-private")

    first = M6TutoringControlServiceStub().decide_next_action(
        task_a, scoring_a, state_a, None
    )
    second = M6TutoringControlServiceStub().decide_next_action(
        task_b, scoring_b, state_b, None
    )

    assert first.evidence_query.query_text == second.evidence_query.query_text
    assert first.evidence_query.concept_ids == ["concept-alpha-private"]
    assert second.evidence_query.concept_ids == ["concept-beta-private"]
    assert first.evidence_query.item_id is None
    assert first.evidence_query.use_case == "feedback"
    assert first.evidence_query.top_k == 3
    assert first.evidence_query.min_relevance == 0.2


def test_service_does_not_mutate_any_caller_owned_input() -> None:
    task, scoring, state = _valid_inputs()
    previous = _session("S1")
    before = tuple(
        value.model_dump(mode="json") for value in (task, scoring, state, previous)
    )

    result = M6TutoringControlServiceStub().decide_next_action(
        task,
        scoring,
        state,
        previous,
    )

    after = tuple(
        value.model_dump(mode="json") for value in (task, scoring, state, previous)
    )
    assert after == before
    assert result.session_state_snapshot is not previous
    assert result.session_state_snapshot.turn_count == previous.turn_count + 1
    assert result.session_state_snapshot.completed_action_ids[:-1] == (
        previous.completed_action_ids
    )


IDENTITY_CONFLICT_CASES = (
    "scoring_learner",
    "state_learner",
    "state_course",
    "state_class",
    "diagnosis_attempt",
    "latest_audit_watermark",
    "event_course",
    "event_class",
    "event_paper",
    "state_internal_audit_conflict",
    "session",
    "target_without_concept_state",
)


def _conflicting_inputs(
    case: str,
) -> tuple[TaskPlan, ScoringResultBundle, StateUpdateResult, SessionStateSnapshot | None]:
    task = _task_plan()
    scoring = _scoring_bundle()
    state = _state_update()
    previous: SessionStateSnapshot | None = None

    if case == "scoring_learner":
        scoring = _scoring_bundle(learner_id="other-learner")
    elif case == "state_learner":
        state = _state_update(learner_id="other-learner")
    elif case == "state_course":
        state = _state_update(course_id="other-course")
    elif case == "state_class":
        state = _state_update(class_id="other-class")
    elif case == "diagnosis_attempt":
        state = _state_update(attempt_id="other-attempt")
    elif case == "latest_audit_watermark":
        scoring = _scoring_bundle(audit_versions=(1, 2))
        state = _state_update(
            evidence_audit_ids=(f"{AUDIT_ID}:1",),
            processed_audit_ids=(f"{AUDIT_ID}:1",),
        )
    elif case == "event_course":
        scoring = _scoring_bundle(event_course_id="other-course")
    elif case == "event_class":
        scoring = _scoring_bundle(event_class_id="other-class")
    elif case == "event_paper":
        scoring = _scoring_bundle(event_paper_id="other-paper")
    elif case == "state_internal_audit_conflict":
        state = state.model_copy(
            update={
                "processed_audit_ids": [
                    f"{AUDIT_ID}:1",
                    f"{AUDIT_ID}:1",
                ]
            },
            deep=True,
        )
    elif case == "session":
        previous = _session("S1", session_id="other-session")
    elif case == "target_without_concept_state":
        state = _state_update(
            concept_states=(_concept_state("unrelated-concept"),),
            item_concepts=("supported-target",),
            priority_concepts=("supported-target",),
        )
    else:  # pragma: no cover - protects the test table itself
        raise AssertionError(f"unknown conflict fixture: {case}")

    return task, scoring, state, previous


@pytest.mark.parametrize("case", IDENTITY_CONFLICT_CASES)
def test_service_rejects_critical_cross_contract_identity_conflicts(case: str) -> None:
    task, scoring, state, previous = _conflicting_inputs(case)

    with pytest.raises(DomainError) as raised:
        M6TutoringControlServiceStub().decide_next_action(
            task,
            scoring,
            state,
            previous,
        )

    assert raised.value.code == "TUTORING_REFERENCE_MISMATCH"


def test_reference_errors_do_not_echo_host_paths_or_sensitive_answers() -> None:
    sensitive_path = r"C:\Users\Alice\private\answer-key.txt"
    task = _task_plan(task_id=sensitive_path)
    scoring = _scoring_bundle(learner_id="other-learner")
    state = _state_update()

    with pytest.raises(DomainError) as raised:
        M6TutoringControlServiceStub().decide_next_action(
            task,
            scoring,
            state,
            None,
        )

    serialized_error = json.dumps(
        raised.value.to_dict(), ensure_ascii=False, sort_keys=True
    )
    assert raised.value.code == "TUTORING_REFERENCE_MISMATCH"
    assert sensitive_path not in serialized_error
    assert SENSITIVE_ANSWER not in serialized_error


def _learned_runtime(mode: str) -> PolicyRuntime:
    class PreferHintAdapter:
        adapter_id = "test-prefer-hint"
        adapter_version = "v1"
        policy_id = "test-learned-policy-v1"

        def select(self, context: Any, candidates: Any) -> PolicyDecision:
            selected = next(
                candidate for candidate in candidates if candidate.next_state == "S2"
            )
            return PolicyDecision(
                request_fingerprint=context.request_fingerprint,
                mode="active",
                selected_candidate_id=selected.candidate_id,
                candidate_ids=tuple(
                    candidate.candidate_id for candidate in candidates
                ),
                prediction=PolicyPrediction(
                    policy_id=self.policy_id,
                    candidate_id=selected.candidate_id,
                    score=1.0,
                    propensity=1.0,
                ),
            )

    manifest = PolicyArtifactManifest(
        policy_id="test-learned-policy-v1",
        adapter_id="test-prefer-hint",
        adapter_version="v1",
        artifact_sha256="d" * 64,
        feature_schema_version="m6-features-v1",
        action_space_version="m6-action-space-v1",
        gate_policy_version="m6-active-gate-v1",
        status="approved",
        artifact_reference="policy.json",
        allowed_scopes=(COURSE_ID,),
    )

    def load_artifact(candidate_ids: tuple[str, ...]) -> LoadedPolicyArtifact:
        return LoadedPolicyArtifact(
            manifest=manifest,
            payload={
                "actions": {candidate_id: {} for candidate_id in candidate_ids}
            },
        )

    return PolicyRuntime(
        mode=mode,
        learned_adapter=PreferHintAdapter(),
        artifact_loader=load_artifact,
        active_gate=ActivePolicyGate(
            PolicyGateConfig(
                gate_policy_version="m6-active-gate-v1",
                minimum_support=1,
                maximum_uncertainty=0.1,
                rollout_percentage=1.0,
                kill_switch=False,
            )
        ),
        gate_inputs=PolicyRuntimeGateInputs(
            support=10,
            uncertainty=0.0,
            offline_evaluation_approved=True,
            scope=COURSE_ID,
        ),
    )


def test_explicit_rules_runtime_matches_default_public_result_field_for_field() -> None:
    task, scoring, state = _valid_inputs()
    previous = _session("S1")
    default_result = M6TutoringControlService(
        DEFAULT_STATE_MACHINE,
        InMemoryM6Repository(),
    ).decide_next_action(task, scoring, state, previous)
    rules_result = M6TutoringControlService(
        DEFAULT_STATE_MACHINE,
        InMemoryM6Repository(),
        policy_runtime=PolicyRuntime(mode="rules"),
    ).decide_next_action(
        task.model_copy(deep=True),
        scoring.model_copy(deep=True),
        state.model_copy(deep=True),
        previous.model_copy(deep=True),
    )

    assert rules_result.model_dump(mode="json") == default_result.model_dump(
        mode="json"
    )


def test_service_shadow_prediction_cannot_change_the_public_result() -> None:
    task, scoring, state = _valid_inputs()
    repository = InMemoryM6Repository()
    service = M6TutoringControlService(
        DEFAULT_STATE_MACHINE,
        repository,
        policy_runtime=_learned_runtime("shadow"),
    )

    result = service.decide_next_action(task, scoring, state, _session("S1"))
    stored = repository.get_decision_by_request(
        request_fingerprint(
            task_plan=task,
            scoring_result_bundle=scoring,
            state_update_result=state,
            caller_previous_session_state_snapshot=_session("S1"),
        )
    )

    assert result.next_state() == "S3"
    assert stored is not None
    assert stored.policy_execution_ref is not None
    assert stored.policy_execution_ref.mode == "shadow"
    assert stored.policy_observation is not None
    assert stored.policy_observation.selected_candidate_id.endswith("s1_to_s2.v1")


def test_service_active_adopts_gate_approved_safe_candidate_and_replays_it() -> None:
    task, scoring, state = _valid_inputs()
    previous = _session("S1")
    repository = InMemoryM6Repository()
    service = M6TutoringControlService(
        DEFAULT_STATE_MACHINE,
        repository,
        policy_runtime=_learned_runtime("active"),
    )

    prepared = service.prepare_policy_execution(task, scoring, state, previous)
    rules_service = M6TutoringControlService(
        DEFAULT_STATE_MACHINE,
        repository,
        policy_runtime=PolicyRuntime(mode="rules"),
    )
    assert (
        rules_service.prepare_policy_execution(task, scoring, state, previous)
        == prepared
    )
    first = service.decide_next_action(task, scoring, state, previous)
    replay = service.decide_next_action(task, scoring, state, previous)
    stored = repository.get_decision_by_request(
        request_fingerprint(
            task_plan=task,
            scoring_result_bundle=scoring,
            state_update_result=state,
            caller_previous_session_state_snapshot=previous,
        )
    )

    assert first.next_state() == "S2"
    assert replay.model_dump(mode="json") == first.model_dump(mode="json")
    assert stored is not None
    assert stored.policy_execution_ref == prepared
    assert stored.policy_observation is not None
    assert stored.policy_observation.policy_execution_fingerprint == (
        prepared.policy_execution_fingerprint
    )


def test_policy_persistence_failure_falls_back_to_rules_behavior() -> None:
    class FailingPolicyRepository(InMemoryM6Repository):
        def commit_policy_execution(self, execution: Any) -> Any:
            raise RuntimeError("policy persistence unavailable")

    task, scoring, state = _valid_inputs()
    repository = FailingPolicyRepository()
    result = M6TutoringControlService(
        DEFAULT_STATE_MACHINE,
        repository,
        policy_runtime=_learned_runtime("active"),
    ).decide_next_action(task, scoring, state, _session("S1"))

    assert result.next_state() == "S3"
