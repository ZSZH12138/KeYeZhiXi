"""Formal deterministic M6 tutoring-control service boundary."""

from __future__ import annotations

from typing import Any

from course_insight.contracts.assessment import ScoringResultBundle
from course_insight.contracts.errors import DomainError
from course_insight.contracts.state import StateUpdateResult
from course_insight.contracts.tasking import TaskPlan
from course_insight.contracts.tutoring import (
    SessionStateSnapshot,
    TutoringControlResult,
)
from course_insight.modules.m6_tutoring_fsm.action_factory import (
    build_tutoring_result,
)
from course_insight.modules.m6_tutoring_fsm.decision_policy import (
    DecisionSignals,
    M6DecisionPolicy,
)
from course_insight.modules.m6_tutoring_fsm.identity import (
    EvidenceIdentity,
    build_evidence_identity,
    derive_identifier,
    has_new_evidence,
    input_fingerprint,
    request_fingerprint,
)
from course_insight.modules.m6_tutoring_fsm.repository import (
    M6Repository,
    TutoringDecisionRecord,
    isolated_session_snapshot,
)
from course_insight.modules.m6_tutoring_fsm.policy_runtime import (
    PolicyRuntime,
    rules_policy_execution,
)
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    CandidateAction,
    PolicyExecutionRef,
    TutoringPolicyContext,
)
from course_insight.modules.m6_tutoring_fsm.safety_envelope import SafetyEnvelope
from course_insight.modules.m6_tutoring_fsm.state_machine import (
    DEFAULT_STATE_MACHINE,
    DefaultTutoringStateMachine,
)
from course_insight.modules.m6_tutoring_fsm.target_selector import (
    select_target_concept_ids,
)


class M6TutoringControlService:
    """Select, persist, and replay one safe evidence-bound tutoring action."""

    def __init__(
        self,
        state_machine_definition: Any,
        repository: M6Repository,
        *,
        policy_runtime: PolicyRuntime | None = None,
    ) -> None:
        self._state_machine_definition = (
            state_machine_definition
            if isinstance(state_machine_definition, DefaultTutoringStateMachine)
            else DEFAULT_STATE_MACHINE
        )
        self._repository = repository
        self._policy = M6DecisionPolicy()
        self._policy_runtime = (
            policy_runtime if policy_runtime is not None else PolicyRuntime()
        )
        self._safety_envelope = SafetyEnvelope()

    def prepare_policy_execution(
        self,
        task_plan: TaskPlan,
        scoring_result_bundle: ScoringResultBundle,
        state_update_result: StateUpdateResult,
        previous_session_state_snapshot: SessionStateSnapshot | None,
    ) -> PolicyExecutionRef:
        """First-write the private policy identity for the public M6 request."""

        task_plan.assert_module_allowed("M6")
        _validate_cross_contract_references(
            task_plan,
            scoring_result_bundle,
            state_update_result,
            previous_session_state_snapshot,
        )
        request_key = request_fingerprint(
            task_plan=task_plan,
            scoring_result_bundle=scoring_result_bundle,
            state_update_result=state_update_result,
            caller_previous_session_state_snapshot=(
                previous_session_state_snapshot
            ),
        )
        stored = self._load_policy_execution(request_key)
        if stored is not None:
            replay = self._repository.get_decision_by_request(request_key)
            if replay is not None:
                self._validate_replay_policy_execution(
                    replay,
                    stored,
                    request_key,
                )
            return stored
        replay = self._repository.get_decision_by_request(request_key)
        if replay is not None:
            return self._prepare_replay_policy_execution(replay, request_key)

        try:
            previous = self._resolve_previous_snapshot(
                task_plan,
                scoring_result_bundle,
                previous_session_state_snapshot,
            )
        except DomainError as error:
            if (
                error.module != "m6"
                or error.code != "TUTORING_REFERENCE_MISMATCH"
                or error.details.get("reason")
                != "caller_session_history_is_stale"
            ):
                raise
            replay = self._repository.get_decision_by_request(request_key)
            if replay is None:
                raise
            return self._prepare_replay_policy_execution(replay, request_key)
        _, _, _, context, candidates = self._build_policy_inputs(
            task_plan,
            scoring_result_bundle,
            state_update_result,
            previous,
            request_key,
        )
        desired = self._policy_runtime.prepare_execution(context, candidates)
        return self._commit_policy_execution(desired)

    def decide_next_action(
        self,
        task_plan: TaskPlan,
        scoring_result_bundle: ScoringResultBundle,
        state_update_result: StateUpdateResult,
        previous_session_state_snapshot: SessionStateSnapshot | None,
    ) -> TutoringControlResult:
        """Return the unique authoritative action for the supplied M6 inputs."""

        task_plan.assert_module_allowed("M6")
        _validate_cross_contract_references(
            task_plan,
            scoring_result_bundle,
            state_update_result,
            previous_session_state_snapshot,
        )
        request_key = request_fingerprint(
            task_plan=task_plan,
            scoring_result_bundle=scoring_result_bundle,
            state_update_result=state_update_result,
            caller_previous_session_state_snapshot=(
                previous_session_state_snapshot
            ),
        )
        replay = self._repository.get_decision_by_request(request_key)
        if replay is not None:
            self._prepare_replay_policy_execution(replay, request_key)
            return _validated_replay(replay, task_plan, request_key)

        try:
            previous = self._resolve_previous_snapshot(
                task_plan,
                scoring_result_bundle,
                previous_session_state_snapshot,
            )
        except DomainError as error:
            if (
                error.module != "m6"
                or error.code != "TUTORING_REFERENCE_MISMATCH"
                or error.details.get("reason")
                != "caller_session_history_is_stale"
            ):
                raise
            replay = self._repository.get_decision_by_request(request_key)
            if replay is None:
                raise
            self._prepare_replay_policy_execution(replay, request_key)
            return _validated_replay(replay, task_plan, request_key)
        (
            targets,
            current_evidence,
            signals,
            context,
            candidates,
        ) = self._build_policy_inputs(
            task_plan,
            scoring_result_bundle,
            state_update_result,
            previous,
            request_key,
        )
        execution = self._load_policy_execution(request_key)
        if execution is None:
            desired = self._policy_runtime.prepare_execution(context, candidates)
            execution = self._commit_policy_execution(desired)
        runtime_selection = self._policy_runtime.select(
            execution,
            context,
            candidates,
        )
        next_state = runtime_selection.public_candidate.next_state
        self._state_machine_definition.validate_transition(
            previous.current_state,
            next_state,
        )
        input_key = input_fingerprint(
            task_plan=task_plan,
            scoring_result_bundle=scoring_result_bundle,
            state_update_result=state_update_result,
            authoritative_previous_session_state_snapshot=previous,
        )
        candidate_result = build_tutoring_result(
            task_plan=task_plan,
            scoring_result_bundle=scoring_result_bundle,
            state_update_result=state_update_result,
            previous_session_state_snapshot=previous,
            next_state=next_state,
            target_concept_ids=targets,
            authoritative_input_fingerprint=input_key,
            policy_version=self._policy.policy_version,
            signals=signals,
        )
        candidate = TutoringDecisionRecord(
            decision_id=derive_identifier("decision", input_key),
            session_id=task_plan.session_id,
            turn_count=candidate_result.session_state_snapshot.turn_count,
            previous_turn_count=previous.turn_count,
            request_fingerprint=request_key,
            input_fingerprint=input_key,
            evidence_identity=current_evidence,
            result=candidate_result,
            policy_execution_ref=execution,
            policy_observation=runtime_selection.observation,
        )
        authoritative = self._repository.commit_decision(candidate, previous)
        if (
            authoritative.request_fingerprint == request_key
            and authoritative.policy_execution_ref is not None
            and authoritative.policy_execution_ref != execution
        ):
            _raise_policy_integrity_error("decision_policy_execution_mismatch")
        return _validated_replay(authoritative, task_plan, authoritative.request_fingerprint)

    def _build_policy_inputs(
        self,
        task_plan: TaskPlan,
        scoring_result_bundle: ScoringResultBundle,
        state_update_result: StateUpdateResult,
        previous: SessionStateSnapshot,
        request_key: str,
    ) -> tuple[
        list[str],
        EvidenceIdentity,
        DecisionSignals,
        TutoringPolicyContext,
        tuple[CandidateAction, ...],
    ]:
        targets = select_target_concept_ids(
            diagnosis_result=state_update_result.diagnosis_result,
            remediation_plan=scoring_result_bundle.remediation_plan,
            learner_state_snapshot=state_update_result.learner_state_snapshot,
            weak_mastery_threshold=self._policy.weak_mastery_threshold,
        )
        current_evidence = build_evidence_identity(
            scoring_result_bundle,
            state_update_result,
        )
        prior_decision = self._repository.get_latest_decision(task_plan.session_id)
        evidence_progressed = has_new_evidence(
            current_evidence,
            None if prior_decision is None else prior_decision.evidence_identity,
        )
        signals = _decision_signals(
            scoring_result_bundle,
            state_update_result,
            targets,
            self._policy,
            evidence_progressed,
        )
        context = TutoringPolicyContext(
            request_fingerprint=request_key,
            current_state=previous.current_state,
            task_type=task_plan.task_type,
            turn_count=previous.turn_count,
            score_ratio=(
                scoring_result_bundle.total_score
                / scoring_result_bundle.max_score
            ),
            target_concept_count=len(targets),
            signals=signals,
            learner_evidence_count=(
                state_update_result.learner_state_snapshot.evidence_count
            ),
            course_id=task_plan.course_id,
            class_id=task_plan.class_id,
        )
        candidates = self._safety_envelope.candidates_for(context)
        return targets, current_evidence, signals, context, candidates

    def _load_policy_execution(
        self,
        request_key: str,
    ) -> PolicyExecutionRef | None:
        getter = getattr(
            self._repository,
            "get_policy_execution_by_request",
            None,
        )
        if not callable(getter):
            return None
        try:
            execution = getter(request_key)
        except DomainError as error:
            if error.code == "TUTORING_POLICY_INTEGRITY_ERROR":
                raise
            return None
        except Exception:
            return None
        if execution is None:
            return None
        if (
            not isinstance(execution, PolicyExecutionRef)
            or execution.request_fingerprint != request_key
        ):
            _raise_policy_integrity_error("policy_execution_request_mismatch")
        return execution

    def _commit_policy_execution(
        self,
        desired: PolicyExecutionRef,
    ) -> PolicyExecutionRef:
        committer = getattr(self._repository, "commit_policy_execution", None)
        if not callable(committer):
            return (
                desired
                if desired.mode == "rules"
                else rules_policy_execution(desired.request_fingerprint)
            )
        try:
            execution = committer(desired)
        except DomainError as error:
            if error.code == "TUTORING_POLICY_INTEGRITY_ERROR":
                raise
            return rules_policy_execution(desired.request_fingerprint)
        except Exception:
            return rules_policy_execution(desired.request_fingerprint)
        if (
            not isinstance(execution, PolicyExecutionRef)
            or execution.request_fingerprint != desired.request_fingerprint
        ):
            _raise_policy_integrity_error("policy_execution_request_mismatch")
        return execution

    def _prepare_replay_policy_execution(
        self,
        replay: TutoringDecisionRecord,
        request_key: str,
    ) -> PolicyExecutionRef:
        expected = (
            replay.policy_execution_ref
            if replay.policy_execution_ref is not None
            else rules_policy_execution(request_key)
        )
        stored = self._load_policy_execution(request_key)
        authoritative = (
            stored
            if stored is not None
            else self._commit_policy_execution(expected)
        )
        self._validate_replay_policy_execution(
            replay,
            authoritative,
            request_key,
        )
        return authoritative

    @staticmethod
    def _validate_replay_policy_execution(
        replay: TutoringDecisionRecord,
        execution: PolicyExecutionRef,
        request_key: str,
    ) -> None:
        expected = (
            replay.policy_execution_ref
            if replay.policy_execution_ref is not None
            else rules_policy_execution(request_key)
        )
        if (
            replay.request_fingerprint != request_key
            or execution != expected
        ):
            _raise_policy_integrity_error("replay_policy_execution_mismatch")

    def _resolve_previous_snapshot(
        self,
        task_plan: TaskPlan,
        scoring_result_bundle: ScoringResultBundle,
        caller_snapshot: SessionStateSnapshot | None,
    ) -> SessionStateSnapshot:
        repository_snapshot = self._repository.get_latest_session_state(
            task_plan.session_id
        )
        caller = (
            None
            if caller_snapshot is None
            else isolated_session_snapshot(caller_snapshot)
        )
        if repository_snapshot is not None and caller is not None:
            if repository_snapshot.content_checksum() != caller.content_checksum():
                _raise_reference_mismatch("caller_session_history_is_stale")
            return repository_snapshot
        if repository_snapshot is not None:
            return repository_snapshot
        if caller is not None:
            return caller
        return SessionStateSnapshot(
            session_id=task_plan.session_id,
            current_state="S1",
            turn_count=0,
            completed_action_ids=[],
            updated_at=scoring_result_bundle.finalized_at,
        )


def _validate_cross_contract_references(
    task_plan: TaskPlan,
    scoring: ScoringResultBundle,
    state: StateUpdateResult,
    caller_snapshot: SessionStateSnapshot | None,
) -> None:
    try:
        diagnosis = state.diagnosis_result
        learner = state.learner_state_snapshot
        class_state = state.class_state_snapshot
        remediation = scoring.remediation_plan
        if (
            task_plan.learner_id != scoring.learner_id
            or task_plan.learner_id != diagnosis.learner_id
            or task_plan.learner_id != learner.learner_id
            or task_plan.course_id != learner.course_id
            or task_plan.class_id != learner.class_id
            or task_plan.course_id != class_state.course_id
            or task_plan.class_id != class_state.class_id
            or scoring.attempt_id != diagnosis.attempt_id
            or remediation.based_on_attempt_id != scoring.attempt_id
            or remediation.learner_id != scoring.learner_id
        ):
            _raise_reference_mismatch("cross_contract_identity_mismatch")
        if caller_snapshot is not None and (
            caller_snapshot.session_id != task_plan.session_id
        ):
            _raise_reference_mismatch("caller_session_identity_mismatch")

        latest_audit_versions: dict[str, int] = {}
        for audit in scoring.score_audit_records:
            current_version = latest_audit_versions.get(audit.audit_id)
            if current_version is None or audit.audit_version > current_version:
                latest_audit_versions = {
                    **latest_audit_versions,
                    audit.audit_id: audit.audit_version,
                }
        expected_audit_keys = {
            f"{audit_id}:{version}"
            for audit_id, version in latest_audit_versions.items()
        }
        if not expected_audit_keys <= set(state.processed_audit_ids):
            _raise_reference_mismatch("latest_scoring_audit_not_processed")

        for event in scoring.learning_events:
            if (
                event.course_id != task_plan.course_id
                or event.class_id != task_plan.class_id
                or event.learner_id != task_plan.learner_id
            ):
                _raise_reference_mismatch("learning_event_identity_mismatch")
            if "paper_id" in event.payload and (
                type(event.payload["paper_id"]) is not str
                or event.payload["paper_id"] != scoring.paper_id
            ):
                _raise_reference_mismatch("learning_event_paper_mismatch")
        state.assert_consistent()
    except DomainError as error:
        if error.module == "m6":
            raise
        raise DomainError(
            code="TUTORING_REFERENCE_MISMATCH",
            module="m6",
            message="state update references are inconsistent for tutoring",
            details={"reason": "upstream_state_consistency_mismatch"},
        ) from error
    except (AttributeError, TypeError, ValueError) as error:
        raise DomainError(
            code="TUTORING_REFERENCE_MISMATCH",
            module="m6",
            message="tutoring inputs do not satisfy the required contract shape",
            details={"reason": "invalid_contract_shape"},
        ) from error


def _decision_signals(
    scoring: ScoringResultBundle,
    state: StateUpdateResult,
    targets: list[str],
    policy: M6DecisionPolicy,
    evidence_progressed: bool,
) -> DecisionSignals:
    concept_states = [
        state.learner_state_snapshot.get_concept_state(concept_id)
        for concept_id in targets
    ]
    active_misconception = any(
        concept_state.active_misconceptions(
            policy.active_misconception_threshold
        )
        for concept_state in concept_states
    )
    return DecisionSignals(
        needs_teacher_review=scoring.requires_teacher_review(),
        has_diagnosed_misconception=bool(
            state.diagnosis_result.priority_misconception_ids
        )
        or any(
            item_diagnosis.misconception_ids
            for item_diagnosis in state.diagnosis_result.item_diagnoses
        ),
        has_active_misconception=active_misconception,
        has_prerequisite_gap=state.diagnosis_result.has_prerequisite_gap(),
        has_new_evidence=evidence_progressed,
        minimum_recent_correction_rate=min(
            concept_state.recent_correction_rate
            for concept_state in concept_states
        ),
        minimum_mastery_confidence=min(
            concept_state.mastery_confidence for concept_state in concept_states
        ),
        maximum_hint_dependency=max(
            concept_state.hint_dependency for concept_state in concept_states
        ),
    )


def _validated_replay(
    record: TutoringDecisionRecord,
    task_plan: TaskPlan,
    expected_request_fingerprint: str,
) -> TutoringControlResult:
    result = record.isolated_copy().result
    if (
        record.request_fingerprint != expected_request_fingerprint
        or record.session_id != task_plan.session_id
        or result.session_state_snapshot.session_id != task_plan.session_id
        or result.feedback_generation_task.task_id != task_plan.task_id
        or result.feedback_generation_task.learner_id != task_plan.learner_id
        or result.evidence_query.course_package_id != task_plan.course_package_id
    ):
        _raise_reference_mismatch("persisted_decision_identity_mismatch")
    result.assert_query_alignment()
    return result


def _raise_reference_mismatch(reason: str) -> None:
    raise DomainError(
        code="TUTORING_REFERENCE_MISMATCH",
        module="m6",
        message="task, scoring, state, and session identities must align",
        details={"reason": reason},
    )


def _raise_policy_integrity_error(reason: str) -> None:
    raise DomainError(
        code="TUTORING_POLICY_INTEGRITY_ERROR",
        module="m6",
        message="stored tutoring policy execution is inconsistent",
        details={"reason": reason},
    )
