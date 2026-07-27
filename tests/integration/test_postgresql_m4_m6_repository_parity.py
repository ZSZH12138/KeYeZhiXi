from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Iterator
from uuid import uuid4

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceQuery
from course_insight.contracts.state import DiagnosisResult, ItemDiagnosis
from course_insight.contracts.tasking import TaskPlan
from course_insight.contracts.tutoring import (
    FeedbackGenerationTask,
    SessionStateSnapshot,
    TeachingAction,
    TutoringControlResult,
)
from course_insight.infrastructure.postgresql.m4_repository import (
    PostgresM4Repository,
)
from course_insight.infrastructure.postgresql.m6_repository import (
    PostgresM6Repository,
)
from course_insight.infrastructure.postgresql.pool import (
    PostgresPool,
    create_postgres_pool,
)
from course_insight.modules.m4_task_orchestration.identity import (
    canonical_idempotency_key,
)
from course_insight.modules.m6_tutoring_fsm.identity import (
    EvidenceIdentity,
    derive_identifier,
)
from course_insight.modules.m6_tutoring_fsm.repository import (
    TutoringDecisionRecord,
)
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    PolicyExecutionRef,
    PolicyObservation,
)
from tests.integration._postgres_live import require_live_test_database_url


NOW = datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)
@pytest.fixture(scope="module")
def postgres_pool() -> Iterator[PostgresPool]:
    dsn = require_live_test_database_url()
    from course_insight.infrastructure.postgresql.migration_runner import (
        run_migrations,
    )

    pool = create_postgres_pool(
        dsn,
        min_size=1,
        max_size=20,
        connect_timeout_seconds=5.0,
    )
    try:
        run_migrations(pool)
        yield pool
    finally:
        pool.close()


def _m4_plan(token: str, created_at: datetime) -> tuple[TaskPlan, str]:
    identity = {
        "course_id": f"course_{token}",
        "class_id": f"class_{token}",
        "learner_id": f"learner_{token}",
        "session_id": f"session_{token}",
        "task_type": "stage_assessment",
        "knowledge_bundle_id": f"bundle_{token}",
        "course_package_id": f"package_{token}",
        "blueprint_id": f"blueprint_{token}",
    }
    key = canonical_idempotency_key(identity)
    return (
        TaskPlan(
            task_id=f"task_{key}",
            task_type="stage_assessment",
            course_id=identity["course_id"],
            class_id=identity["class_id"],
            learner_id=identity["learner_id"],
            session_id=identity["session_id"],
            blueprint_id=identity["blueprint_id"],
            knowledge_bundle_id=identity["knowledge_bundle_id"],
            course_package_id=identity["course_package_id"],
            workflow=["M8", "M2", "M7", "M5", "M6", "M9"],
            next_module="M8",
            created_at=created_at,
        ),
        key,
    )


def _fingerprint(token: str, kind: str) -> str:
    return hashlib.sha256(f"{kind}:{token}".encode("utf-8")).hexdigest()


def _m6_record(
    token: str,
    previous: SessionStateSnapshot,
    *,
    variant: str = "first",
) -> TutoringDecisionRecord:
    request_key = _fingerprint(token, f"request:{variant}")
    input_key = _fingerprint(token, f"input:{variant}")
    action_id = derive_identifier("action", input_key)
    query_id = derive_identifier("query", input_key)
    action = TeachingAction(
        action_id=action_id,
        state_before=previous.current_state,
        action_type="guided_question",
        target_concept_ids=["concept_1"],
        prompt_template_id="m6.integration.v1",
        must_not_reveal_answer=True,
        next_state="S3",
        reason="deterministic integration decision",
    )
    diagnosis = DiagnosisResult(
        diagnosis_id=f"diagnosis_{token}",
        attempt_id=f"attempt_{token}",
        learner_id=f"learner_{token}",
        item_diagnoses=[
            ItemDiagnosis(
                item_instance_id=f"item_{token}",
                concept_ids=["concept_1"],
                misconception_ids=[],
                error_type="none",
                confidence=1.0,
                evidence_audit_ids=[f"audit_{token}:1"],
                prerequisite_gap_ids=[],
            )
        ],
        priority_concept_ids=["concept_1"],
        priority_misconception_ids=[],
        generated_at=NOW,
    )
    query = EvidenceQuery(
        query_id=query_id,
        course_package_id=f"package_{token}",
        query_text="Retrieve governed evidence for safe feedback.",
        concept_ids=["concept_1"],
        item_id=None,
        use_case="feedback",
        top_k=3,
        min_relevance=0.2,
    )
    feedback = FeedbackGenerationTask(
        feedback_task_id=derive_identifier("feedback_task", input_key),
        task_id=f"task_{token}",
        learner_id=f"learner_{token}",
        teaching_action=action,
        diagnosis_result=diagnosis,
        score_summary={"total_score": 1.0, "max_score": 1.0},
        learner_state_snapshot_id=f"learner_snapshot_{token}",
        evidence_query_id=query_id,
        created_at=NOW + timedelta(minutes=1),
    )
    result = TutoringControlResult(
        teaching_action=action,
        feedback_generation_task=feedback,
        evidence_query=query,
        session_state_snapshot=SessionStateSnapshot(
            session_id=previous.session_id,
            current_state="S3",
            turn_count=1,
            completed_action_ids=[action_id],
            updated_at=NOW + timedelta(minutes=1),
        ),
        created_at=NOW + timedelta(minutes=1),
    )
    return TutoringDecisionRecord(
        decision_id=derive_identifier("decision", input_key),
        session_id=previous.session_id,
        turn_count=1,
        previous_turn_count=0,
        request_fingerprint=request_key,
        input_fingerprint=input_key,
        evidence_identity=EvidenceIdentity(
            scoring_result_checksum=_fingerprint(token, "scoring"),
            latest_audit_version_keys=(f"audit_{token}:1",),
            processed_audit_ids=(f"audit_{token}:1",),
            learner_state_version=1,
            learner_state_checksum=_fingerprint(token, "learner-state"),
        ),
        result=result,
    )


def test_real_postgres_m4_concurrency_jsonb_checksum_and_recovery(
    postgres_pool: PostgresPool,
) -> None:
    token = uuid4().hex
    repository = PostgresM4Repository(postgres_pool)
    first, key = _m4_plan(token, NOW)
    try:
        def insert(index: int) -> TaskPlan:
            candidate, _ = _m4_plan(
                token,
                NOW + timedelta(seconds=index),
            )
            return repository.insert_or_get_task_plan(candidate, key)

        with ThreadPoolExecutor(max_workers=20) as executor:
            plans = list(executor.map(insert, range(20)))

        assert len({plan.content_checksum() for plan in plans}) == 1
        assert len({plan.created_at for plan in plans}) == 1
        restored = PostgresM4Repository(postgres_pool).get_task_plan(
            first.task_id
        )
        assert restored == plans[0]
        with postgres_pool.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS row_count,
                    MIN(pg_typeof(payload)::text) AS payload_type,
                    MIN(payload_checksum) AS payload_checksum,
                    MIN(schema_version) AS schema_version
                FROM m4_task_plans
                WHERE task_id = %s
                """,
                (first.task_id,),
            ).fetchone()
        assert row is not None
        assert int(row["row_count"]) == 1
        assert row["payload_type"] == "jsonb"
        assert row["payload_checksum"] == restored.content_checksum()
        assert row["schema_version"] == restored.schema_version
    finally:
        with postgres_pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    "DELETE FROM m4_task_plans WHERE task_id = %s",
                    (first.task_id,),
                )


def test_real_postgres_m6_atomic_concurrency_conflict_and_recovery(
    postgres_pool: PostgresPool,
) -> None:
    token = uuid4().hex
    session_id = f"pg_m6_{token}"
    seed = SessionStateSnapshot(
        session_id=session_id,
        current_state="S1",
        turn_count=0,
        completed_action_ids=[],
        updated_at=NOW,
    )
    candidate = _m6_record(token, seed)
    execution = PolicyExecutionRef(
        request_fingerprint=candidate.request_fingerprint,
        mode="rules",
        policy_id="policy_rules_v1",
        adapter_id="rules",
        adapter_version="1",
        artifact_sha256=None,
        feature_schema_version="m6-features-v1",
        action_space_version="m6-actions-v1",
        gate_policy_version="m6-gate-v1",
    )
    observation = PolicyObservation(
        policy_execution_fingerprint=execution.policy_execution_fingerprint,
        request_fingerprint=execution.request_fingerprint,
        feature_schema_version=execution.feature_schema_version,
        candidate_ids=("candidate_rules",),
        selected_candidate_id="candidate_rules",
        propensity=1.0,
    )
    candidate = replace(
        candidate,
        policy_execution_ref=execution,
        policy_observation=observation,
    )
    repository = PostgresM6Repository(postgres_pool)
    try:
        assert repository.commit_policy_execution(execution) == execution

        def commit(_: int) -> TutoringDecisionRecord:
            return repository.commit_decision(
                candidate.isolated_copy(),
                seed.model_copy(deep=True),
            )

        with ThreadPoolExecutor(max_workers=20) as executor:
            decisions = list(executor.map(commit, range(20)))

        assert len({item.result.content_checksum() for item in decisions}) == 1
        competitor = _m6_record(token, seed, variant="competitor")
        with pytest.raises(DomainError) as raised:
            repository.commit_decision(competitor, seed)
        assert raised.value.code == "TUTORING_REFERENCE_MISMATCH"

        restarted = PostgresM6Repository(postgres_pool)
        assert restarted.get_session_state(session_id, 0) == seed
        assert restarted.get_latest_session_state(session_id) == (
            candidate.result.session_state_snapshot
        )
        assert restarted.get_decision_by_request(
            candidate.request_fingerprint
        ) == candidate
        with postgres_pool.connection() as connection:
            snapshot_row = connection.execute(
                """
                SELECT
                    COUNT(*) AS row_count,
                    MIN(pg_typeof(payload)::text) AS payload_type
                FROM m6_session_states
                WHERE session_id = %s
                """,
                (session_id,),
            ).fetchone()
            decision_row = connection.execute(
                """
                SELECT
                    COUNT(*) AS row_count,
                    MIN(pg_typeof(result_payload)::text) AS result_type,
                    MIN(payload_checksum) AS payload_checksum,
                    MIN(schema_version) AS schema_version
                FROM m6_tutoring_decisions
                WHERE session_id = %s
                """,
                (session_id,),
            ).fetchone()
            policy_row = connection.execute(
                """
                SELECT
                    COUNT(*) AS row_count,
                    MIN(pg_typeof(payload)::text) AS payload_type
                FROM m6_policy_observations
                WHERE decision_id = %s
                """,
                (candidate.decision_id,),
            ).fetchone()
        assert snapshot_row is not None
        assert int(snapshot_row["row_count"]) == 2
        assert snapshot_row["payload_type"] == "jsonb"
        assert decision_row is not None
        assert int(decision_row["row_count"]) == 1
        assert decision_row["result_type"] == "jsonb"
        assert decision_row["payload_checksum"] == (
            candidate.result.content_checksum()
        )
        assert decision_row["schema_version"] == candidate.result.schema_version
        assert policy_row == {"row_count": 1, "payload_type": "jsonb"}
    finally:
        with postgres_pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    "DELETE FROM m6_policy_observations WHERE decision_id = %s",
                    (candidate.decision_id,),
                )
                connection.execute(
                    "DELETE FROM m6_tutoring_decisions WHERE session_id = %s",
                    (session_id,),
                )
                connection.execute(
                    """
                    DELETE FROM m6_policy_executions
                    WHERE request_fingerprint = %s
                    """,
                    (candidate.request_fingerprint,),
                )
                connection.execute(
                    "DELETE FROM m6_session_states WHERE session_id = %s",
                    (session_id,),
                )
