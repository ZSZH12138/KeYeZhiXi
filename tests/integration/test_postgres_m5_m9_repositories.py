from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest

from course_insight.infrastructure.postgresql.base import PostgresOperationError
from course_insight.infrastructure.postgresql.m5_repository import (
    PostgresM5Repository,
)
from course_insight.infrastructure.postgresql.m7_repository import (
    PostgresM7Repository,
)
from course_insight.infrastructure.postgresql.m8_repository import (
    PostgresM8Repository,
)
from course_insight.infrastructure.postgresql.m9_repository import (
    PostgresM9Repository,
)
from course_insight.infrastructure.postgresql.migration_runner import (
    rebuild_schema_for_tests,
)
from course_insight.infrastructure.postgresql.pool import PostgresPool
from tests.integration.test_web_workflow_persistence import (
    NOW,
    _analytics,
    _feedback,
    _paper,
    _review,
    _state_result,
    _vector_scoring_bundle,
)
from tests.integration._postgres_live import require_live_test_database_url


@pytest.fixture
def postgres_pool() -> Iterator[PostgresPool]:
    database_url = require_live_test_database_url()
    pool = PostgresPool(
        database_url,
        min_size=1,
        max_size=8,
    )
    rebuild_schema_for_tests(pool, allow_destructive=True)
    try:
        yield pool
    finally:
        pool.close()


def test_postgres_m5_m9_repositories_round_trip_real_contracts(
    postgres_pool: PostgresPool,
) -> None:
    m5 = PostgresM5Repository(postgres_pool)
    first_state = _state_result(
        attempt_id="attempt_course_1",
        course_id="course_1",
        class_id="shared_class",
        state_version=1,
    )
    second_state = _state_result(
        attempt_id="attempt_course_2",
        course_id="course_2",
        class_id="shared_class",
        state_version=1,
    )
    assert m5.insert_or_get_state_update(first_state) == first_state
    assert m5.insert_or_get_state_update(second_state) == second_state
    assert m5.get_state_update("attempt_course_1") == first_state
    assert m5.get_latest_learner_state(
        "course_2",
        "shared_class",
        "learner_1",
    ) == second_state.learner_state_snapshot
    with pytest.raises(RuntimeError, match="ambiguous across courses"):
        m5.get_learner_state("learner_1", 1)

    m7 = PostgresM7Repository(postgres_pool)
    feedback = _feedback()
    assert m7.insert_or_get_feedback(feedback) == feedback
    assert m7.get_feedback(feedback.feedback_id) == feedback
    assert m7.get_feedback_for_task(
        feedback.task_id,
        feedback.learner_id,
    ) == feedback

    m8 = PostgresM8Repository(postgres_pool)
    paper = _paper()
    assert m8.insert_or_get_paper(
        paper,
        course_id="course_1",
        class_id="class_1",
    ) == paper
    first_scoring = _vector_scoring_bundle(
        first_version=4,
        second_version=3,
        finalized_at=NOW + timedelta(hours=1),
    )
    second_scoring = _vector_scoring_bundle(
        first_version=5,
        second_version=1,
        finalized_at=NOW + timedelta(hours=2),
    )
    assert m8.insert_or_get_scoring_result(first_scoring) == first_scoring
    assert m8.insert_or_get_scoring_result(second_scoring) == second_scoring
    assert m8.get_scoring_result("attempt_vector") == second_scoring
    assert m8.get_scoring_result_by_checksum(
        "attempt_vector",
        first_scoring.content_checksum(),
    ) == first_scoring

    m9 = PostgresM9Repository(postgres_pool)
    older = _analytics(
        report_id="report_older",
        generated_at=datetime(
            2026,
            7,
            25,
            8,
            30,
            tzinfo=timezone(timedelta(hours=8)),
        ),
    )
    newer = _analytics(
        report_id="report_newer",
        generated_at=datetime(2026, 7, 25, 1, 0, tzinfo=timezone.utc),
    )
    assert m9.insert_or_get_analytics(
        older,
        course_id="course_1",
    ) == older
    assert m9.insert_or_get_analytics(
        newer,
        course_id="course_1",
    ) == newer
    assert m9.get_latest_analytics(
        course_id="course_1",
        class_id="class_1",
        learner_id="learner_1",
    ) == newer
    review = _review()
    assert m9.insert_or_get_review_decision(review) == review
    assert m9.get_review_decision(review.decision_id) == review

    with postgres_pool.connection() as connection:
        row = connection.execute(
            """
            SELECT payload_checksum, schema_version
            FROM m9_teacher_analytics
            WHERE report_id = %s
            """,
            (newer.report_id,),
        ).fetchone()
    assert row == {
        "payload_checksum": newer.content_checksum(),
        "schema_version": newer.schema_version,
    }


def test_postgres_m5_model_runtime_round_trip(
    postgres_pool: PostgresPool,
) -> None:
    """Exercise DINA/BKT evidence, models, and trace on real PostgreSQL."""

    from course_insight.modules.m5_learner_class_state.bkt import BktEngine
    from course_insight.modules.m5_learner_class_state.dina import DinaEngine
    from tests.unit.test_m5_bkt import _sequence
    from tests.unit.test_m5_dina import _q_matrix, _training_cohort

    repository = PostgresM5Repository(postgres_pool)
    cohort = _training_cohort()
    for batch in cohort:
        assert repository.insert_or_get_learning_observation_batch(batch) == batch
    dina_model = DinaEngine(
        min_students=4,
        min_responses_per_item=4,
        max_iterations=30,
    ).fit(cohort, _q_matrix())

    assert repository.insert_or_get_dina_model(dina_model) == dina_model
    assert repository.get_latest_dina_model(
        course_id=dina_model.course_id,
        class_id=dina_model.class_id,
    ) == dina_model
    original = cohort[0].observations[0]
    replay = original.model_copy(update={"observation_id": "postgres_audit_replay"})
    replay_batch = cohort[0].model_copy(
        update={
            "batch_id": "postgres_audit_replay_batch",
            "observations": [replay],
            "watermark": "postgres_audit_replay_watermark",
        }
    )
    assert repository.insert_or_get_learning_observation_batch(
        replay_batch
    ) == replay_batch
    conflicting = replay.model_copy(
        update={"score": 0.0, "response_outcome": "incorrect"}
    )
    with pytest.raises(PostgresOperationError, match="identity conflict"):
        repository.insert_or_get_learning_observation_batch(
            replay_batch.model_copy(update={"observations": [conflicting]})
        )
    recovered_observations = repository.list_learning_observations(
        course_id="course_1",
        class_id="class_1",
    )
    expected_observations = sorted(
        (item for batch in cohort for item in batch.observations),
        key=lambda item: (item.occurred_at, item.attempt_id, item.observation_id),
    )
    assert recovered_observations == expected_observations

    sequences = [
        _sequence("learner_1", [True, True, False, True, True]),
        _sequence("learner_2", [False, True, True, True, False]),
        _sequence("learner_3", [False, False, True, True, True]),
        _sequence("learner_4", [True, False, False, True, False]),
    ]
    bkt_engine = BktEngine(
        min_students=4,
        min_observations_per_student=5,
        max_iterations=50,
    )
    bkt_model = bkt_engine.fit(sequences)
    trace = bkt_engine.update(bkt_model, sequences[0])

    assert repository.insert_or_get_bkt_model(bkt_model) == bkt_model
    assert repository.get_latest_bkt_model(
        course_id=bkt_model.course_id,
        class_id=bkt_model.class_id,
    ) == bkt_model
    assert repository.insert_or_get_knowledge_trace(trace) == trace
    assert repository.get_knowledge_trace(trace_id=trace.trace_id) == trace


def test_postgres_feedback_insert_or_get_is_concurrency_safe(
    postgres_pool: PostgresPool,
) -> None:
    feedback = _feedback()

    def insert_once(_: int):
        return PostgresM7Repository(
            postgres_pool
        ).insert_or_get_feedback(feedback)

    with ThreadPoolExecutor(max_workers=8) as executor:
        winners = list(executor.map(insert_once, range(16)))

    assert winners == [feedback] * 16
    with postgres_pool.connection() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS row_count
            FROM m7_student_feedback
            WHERE task_id = %s AND learner_id = %s
            """,
            (feedback.task_id, feedback.learner_id),
        ).fetchone()
    assert row == {"row_count": 1}


def test_postgres_m5_identical_retries_keep_one_class_version(
    postgres_pool: PostgresPool,
) -> None:
    result = _state_result(
        attempt_id="attempt_retry",
        course_id="course_1",
        class_id="class_1",
        state_version=1,
    )

    def insert_once(_: int):
        return PostgresM5Repository(postgres_pool).insert_or_get_state_update(
            result,
            expected_previous_class_snapshot_id=None,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        winners = list(executor.map(insert_once, range(16)))

    assert winners == [result] * 16
    with postgres_pool.connection() as connection:
        class_count = connection.execute(
            """
            SELECT COUNT(*) AS row_count
            FROM m5_class_states
            WHERE course_id = %s AND class_id = %s
            """,
            ("course_1", "class_1"),
        ).fetchone()
        update_count = connection.execute(
            """
            SELECT COUNT(*) AS row_count
            FROM m5_state_updates
            WHERE attempt_id = %s
            """,
            ("attempt_retry",),
        ).fetchone()
    assert class_count == {"row_count": 1}
    assert update_count == {"row_count": 1}


def test_postgres_m8_time_only_retries_return_first_paper_and_score(
    postgres_pool: PostgresPool,
) -> None:
    repository = PostgresM8Repository(postgres_pool)
    paper = _paper()
    assert repository.insert_or_get_paper(
        paper,
        course_id="course_1",
        class_id="class_1",
    ) == paper

    def retry_paper(offset_minutes: int):
        changed_payload = {
            **paper.model_dump(mode="python"),
            "generated_at": paper.generated_at + timedelta(
                minutes=offset_minutes
            ),
            "immutable_checksum": "pending",
        }
        candidate = type(paper)(**changed_payload)
        retried = type(paper)(
            **{
                **changed_payload,
                "immutable_checksum": candidate.freeze(),
            }
        )
        return PostgresM8Repository(postgres_pool).insert_or_get_paper(
            retried,
            course_id="course_1",
            class_id="class_1",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        papers = list(executor.map(retry_paper, range(1, 17)))
    assert papers == [paper] * 16

    scoring = _vector_scoring_bundle(
        first_version=1,
        second_version=1,
        finalized_at=NOW,
    )
    assert repository.insert_or_get_scoring_result(scoring) == scoring

    def retry_scoring(offset_minutes: int):
        retried_at = NOW + timedelta(minutes=offset_minutes)
        retried = scoring.model_copy(
            update={
                "score_audit_records": [
                    audit.model_copy(update={"created_at": retried_at})
                    for audit in scoring.score_audit_records
                ],
                "remediation_plan": scoring.remediation_plan.model_copy(
                    update={"created_at": retried_at}
                ),
                "finalized_at": retried_at,
            }
        )
        return PostgresM8Repository(
            postgres_pool
        ).insert_or_get_scoring_result(retried)

    with ThreadPoolExecutor(max_workers=8) as executor:
        scores = list(executor.map(retry_scoring, range(1, 17)))
    assert scores == [scoring] * 16

    with postgres_pool.connection() as connection:
        paper_count = connection.execute(
            """
            SELECT COUNT(*) AS row_count
            FROM m8_assessment_papers
            WHERE paper_id = %s
            """,
            (paper.paper_id,),
        ).fetchone()
        scoring_count = connection.execute(
            """
            SELECT COUNT(*) AS row_count
            FROM m8_scoring_results
            WHERE attempt_id = %s
            """,
            (scoring.attempt_id,),
        ).fetchone()
    assert paper_count == {"row_count": 1}
    assert scoring_count == {"row_count": 1}
