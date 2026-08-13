"""Unit tests for four-parameter Bayesian Knowledge Tracing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from course_insight.contracts.errors import DomainError


NOW = datetime(2026, 8, 12, 11, 0, tzinfo=UTC)


def _engine(**overrides):
    from course_insight.modules.m5_learner_class_state.bkt import BktEngine

    return BktEngine(**overrides)


def _response(
    learner_id: str,
    index: int,
    correct: bool,
    *,
    concept_id: str = "concept_1",
    audit_version: int = 1,
):
    from course_insight.contracts.learning_models import ConceptResponse

    return ConceptResponse(
        observation_id=f"obs_{learner_id}_{concept_id}_{index}",
        learner_id=learner_id,
        course_id="course_1",
        class_id="class_1",
        attempt_id=f"attempt_{learner_id}_{index}",
        concept_id=concept_id,
        is_correct=correct,
        source_audit_id=f"audit_{learner_id}_{concept_id}_{index}",
        source_audit_version=audit_version,
        occurred_at=NOW + timedelta(minutes=index),
    )


def _sequence(
    learner_id: str,
    outcomes: list[bool],
    *,
    concept_id: str = "concept_1",
):
    from course_insight.contracts.learning_models import ConceptResponseSequence

    return ConceptResponseSequence(
        sequence_id=f"sequence_{learner_id}_{concept_id}",
        learner_id=learner_id,
        course_id="course_1",
        class_id="class_1",
        concept_id=concept_id,
        responses=[
            _response(learner_id, index, correct, concept_id=concept_id)
            for index, correct in enumerate(outcomes)
        ],
        watermark=f"watermark_{learner_id}_{concept_id}",
        created_at=NOW + timedelta(hours=1),
    )


def _model():
    from course_insight.contracts.learning_models import (
        BktConceptParameters,
        BktModelArtifact,
    )

    return BktModelArtifact(
        model_id="bkt_model_1",
        course_id="course_1",
        class_id="class_1",
        model_version="bkt-v1",
        concept_parameters=[
            BktConceptParameters(
                concept_id="concept_1",
                prior=0.4,
                learn=0.2,
                guess=0.2,
                slip=0.1,
                learner_count=100,
                observation_count=500,
            )
        ],
        learner_count=100,
        observation_count=500,
        log_likelihood=-200.0,
        iteration_count=20,
        converged=True,
        created_at=NOW,
    )


def test_correct_answer_raises_mastery_probability() -> None:
    """Catch reversing the correct-response Bayesian update."""

    posterior = _engine().observation_update(
        prior=0.5,
        correct=True,
        guess=0.2,
        slip=0.1,
    )

    assert posterior > 0.5


def test_incorrect_answer_reduces_posterior_before_transition() -> None:
    """Catch treating an incorrect response as positive evidence."""

    posterior = _engine().observation_update(
        prior=0.5,
        correct=False,
        guess=0.2,
        slip=0.1,
    )

    assert posterior < 0.5


def test_order_of_answers_changes_the_knowledge_trace() -> None:
    """Catch sorting by correctness instead of governed event time."""

    first = _engine().update(_model(), _sequence("learner_1", [True, False]))
    second = _engine().update(_model(), _sequence("learner_1", [False, True]))

    assert first.concept_probabilities != second.concept_probabilities


def test_trace_rejects_a_non_converged_bkt_model() -> None:
    """Catch publishing a knowledge trace from an unfinished BKT fit."""

    model = _model().model_copy(update={"converged": False})

    with pytest.raises(DomainError) as captured:
        _engine().update(model, _sequence("learner_1", [True, False]))

    assert captured.value.code == "MODEL_NOT_CONVERGED"


def test_equal_timestamp_responses_are_ordered_by_attempt_then_observation() -> None:
    """Catch nondeterministic ordering when two audits have the same timestamp."""

    from course_insight.contracts.learning_models import ConceptResponseSequence

    early = _response("learner_1", 0, True).model_copy(
        update={
            "observation_id": "z_observation",
            "attempt_id": "attempt_a",
            "occurred_at": NOW,
        }
    )
    late = _response("learner_1", 1, False).model_copy(
        update={
            "observation_id": "a_observation",
            "attempt_id": "attempt_b",
            "occurred_at": NOW,
        }
    )
    unordered = ConceptResponseSequence(
        sequence_id="same_time_unordered",
        learner_id="learner_1",
        course_id="course_1",
        class_id="class_1",
        concept_id="concept_1",
        responses=[late, early],
        watermark="same_time",
        created_at=NOW,
    )
    ordered = unordered.model_copy(update={"responses": [early, late]})

    assert _engine().update(_model(), unordered) == _engine().update(
        _model(),
        ordered,
    )


def test_duplicate_audit_version_is_consumed_only_once() -> None:
    """Catch replaying the same authoritative score evidence twice."""

    from course_insight.contracts.learning_models import ConceptResponseSequence

    unique = _sequence("learner_1", [True])
    duplicate_response = unique.responses[0].model_copy(
        update={"observation_id": "duplicate_observation_id"}
    )
    replay = ConceptResponseSequence(
        sequence_id="sequence_replay",
        learner_id=unique.learner_id,
        course_id=unique.course_id,
        class_id=unique.class_id,
        concept_id=unique.concept_id,
        responses=[unique.responses[0], duplicate_response],
        watermark="watermark_replay",
        created_at=unique.created_at,
    )

    assert _engine().update(_model(), replay).concept_probabilities == (
        _engine().update(_model(), unique).concept_probabilities
    )


def test_bkt_uses_only_the_latest_version_of_one_reviewed_audit() -> None:
    """Teacher overrides must replace, rather than supplement, old evidence."""

    from course_insight.contracts.learning_models import ConceptResponseSequence

    original = _response("learner_1", 0, True)
    reviewed = original.model_copy(
        update={
            "observation_id": "reviewed_observation",
            "attempt_id": "reviewed_attempt",
            "is_correct": False,
            "source_audit_version": 2,
            "occurred_at": NOW + timedelta(minutes=1),
        }
    )
    history = ConceptResponseSequence(
        sequence_id="reviewed_history",
        learner_id=original.learner_id,
        course_id=original.course_id,
        class_id=original.class_id,
        concept_id=original.concept_id,
        responses=[original, reviewed],
        watermark="reviewed_history",
        created_at=reviewed.occurred_at,
    )
    latest_only = history.model_copy(
        update={
            "sequence_id": "reviewed_latest_only",
            "responses": [reviewed],
            "watermark": "reviewed_latest_only",
        }
    )

    actual = _engine().update(_model(), history)
    expected = _engine().update(_model(), latest_only)

    assert actual.observation_count == 1
    assert actual.processed_audit_keys == [
        f"{reviewed.source_audit_id}:v2"
    ]
    assert actual.concept_probabilities == expected.concept_probabilities


def test_bkt_rejects_conflicting_content_for_one_audit_version() -> None:
    """One immutable audit version cannot carry two different outcomes."""

    from course_insight.contracts.learning_models import ConceptResponseSequence

    original = _response("learner_1", 0, True)
    conflicting = original.model_copy(
        update={
            "observation_id": "conflicting_observation",
            "is_correct": False,
        }
    )
    sequence = ConceptResponseSequence(
        sequence_id="conflicting_history",
        learner_id=original.learner_id,
        course_id=original.course_id,
        class_id=original.class_id,
        concept_id=original.concept_id,
        responses=[original, conflicting],
        watermark="conflicting_history",
        created_at=NOW,
    )

    with pytest.raises(DomainError) as captured:
        _engine().update(_model(), sequence)

    assert captured.value.code == "LEARNING_OBSERVATION_AUDIT_CONFLICT"


def test_bkt_training_uses_the_same_latest_audit_rule_as_tracing() -> None:
    """Course fitting and learner tracing must not disagree after a review."""

    sequences = [
        _sequence("learner_1", [True, True, False, True, True]),
        _sequence("learner_2", [False, True, True, True, False]),
        _sequence("learner_3", [False, False, True, True, True]),
        _sequence("learner_4", [True, False, False, True, False]),
    ]
    original = sequences[0].responses[0]
    reviewed = original.model_copy(
        update={
            "observation_id": "reviewed_training_observation",
            "is_correct": False,
            "source_audit_version": 2,
        }
    )
    with_history = [
        sequences[0].model_copy(
            update={"responses": [*sequences[0].responses, reviewed]}
        ),
        *sequences[1:],
    ]
    latest_only = [
        sequences[0].model_copy(
            update={"responses": [reviewed, *sequences[0].responses[1:]]}
        ),
        *sequences[1:],
    ]
    engine = _engine(
        min_students=4,
        min_observations_per_student=5,
        max_iterations=200,
    )

    actual = engine.fit(with_history)
    expected = engine.fit(latest_only)

    assert actual == expected
    assert actual.observation_count == 20


def test_fit_rejects_sequences_below_the_governed_data_threshold() -> None:
    """Catch inventing course BKT parameters from a tiny cohort."""

    engine = _engine(min_students=4, min_observations_per_student=5)
    sequences = [
        _sequence(f"learner_{index}", [True, False, True, False, True])
        for index in range(3)
    ]

    with pytest.raises(DomainError) as captured:
        engine.fit(sequences)

    assert captured.value.code == "INSUFFICIENT_MODEL_DATA"


def test_fit_is_deterministic_and_keeps_four_parameters_bounded() -> None:
    """Catch unstable model versions or invalid four-parameter estimates."""

    sequences = [
        _sequence("learner_1", [True, True, False, True, True]),
        _sequence("learner_2", [False, True, True, True, False]),
        _sequence("learner_3", [False, False, True, True, True]),
        _sequence("learner_4", [True, False, False, True, False]),
    ]
    engine = _engine(
        min_students=4,
        min_observations_per_student=5,
        max_iterations=50,
    )

    first = engine.fit(sequences)
    second = engine.fit(list(reversed(sequences)))
    parameters = first.concept_parameters[0]

    assert first == second
    assert 0.01 <= parameters.prior <= 0.99
    assert all(
        0.01 <= value <= 0.40
        for value in (
            parameters.learn,
            parameters.guess,
            parameters.slip,
        )
    )


def test_m5_service_persists_bkt_model_and_recovers_trace_after_restart(
    tmp_path: Path,
) -> None:
    """Catch restart loss, duplicate updates, or mutable BKT history."""

    from course_insight.infrastructure.sqlite.m5_repository import SQLiteM5Repository
    from course_insight.modules.m5_learner_class_state.aggregation import (
        DeterministicClassAggregationPolicy,
    )
    from course_insight.modules.m5_learner_class_state.bkt import BktEngine
    from course_insight.modules.m5_learner_class_state.service import M5StateService
    from course_insight.modules.m5_learner_class_state.update_policy import (
        DeterministicStateUpdatePolicy,
    )

    repository = SQLiteM5Repository(tmp_path / "m5-bkt.sqlite3")
    repository.initialize()
    engine = BktEngine(
        min_students=4,
        min_observations_per_student=5,
        max_iterations=200,
    )

    def build_service() -> M5StateService:
        return M5StateService(
            repository,
            DeterministicStateUpdatePolicy(),
            DeterministicClassAggregationPolicy(),
            bkt_engine=engine,
        )

    sequences = [
        _sequence("learner_1", [True, True, False, True, True]),
        _sequence("learner_2", [False, True, True, True, False]),
        _sequence("learner_3", [False, False, True, True, True]),
        _sequence("learner_4", [True, False, False, True, False]),
    ]
    first_service = build_service()
    model = first_service.fit_bkt_model(sequences)
    first_trace = first_service.update_knowledge_trace(model, sequences[0])

    restarted_service = build_service()
    replayed_trace = restarted_service.update_knowledge_trace(model, sequences[0])
    extended = sequences[0].model_copy(
        update={
            "responses": [
                *sequences[0].responses,
                _response("learner_1", 5, True),
            ],
            "watermark": "watermark_learner_1_concept_1_extended",
        }
    )
    continued_after_restart = restarted_service.update_knowledge_trace(
        model,
        extended,
    )

    assert replayed_trace == first_trace
    assert continued_after_restart == engine.update(model, extended)
    assert repository.get_bkt_model(
        course_id="course_1",
        model_version=model.model_version,
    ) == model
    assert repository.get_knowledge_trace(
        trace_id=continued_after_restart.trace_id
    ) == continued_after_restart


def test_m5_service_does_not_publish_a_non_converged_bkt_model(
    tmp_path: Path,
) -> None:
    """Catch storing an unfinished BKT fit as the latest usable model."""

    from course_insight.infrastructure.sqlite.m5_repository import SQLiteM5Repository
    from course_insight.modules.m5_learner_class_state.aggregation import (
        DeterministicClassAggregationPolicy,
    )
    from course_insight.modules.m5_learner_class_state.bkt import BktEngine
    from course_insight.modules.m5_learner_class_state.service import M5StateService
    from course_insight.modules.m5_learner_class_state.update_policy import (
        DeterministicStateUpdatePolicy,
    )

    sequences = [
        _sequence("learner_1", [True, True, False, True, True]),
        _sequence("learner_2", [False, True, True, True, False]),
        _sequence("learner_3", [False, False, True, True, True]),
        _sequence("learner_4", [True, False, False, True, False]),
    ]
    repository = SQLiteM5Repository(tmp_path / "m5-bkt-non-converged.sqlite3")
    repository.initialize()
    engine = BktEngine(
        min_students=4,
        min_observations_per_student=5,
        max_iterations=1,
    )
    assert not engine.fit(sequences).converged
    service = M5StateService(
        repository,
        DeterministicStateUpdatePolicy(),
        DeterministicClassAggregationPolicy(),
        bkt_engine=engine,
    )

    with pytest.raises(DomainError) as captured:
        service.fit_bkt_model(sequences)

    assert captured.value.code == "MODEL_NOT_CONVERGED"
    assert repository.get_latest_bkt_model(
        course_id="course_1",
        class_id="class_1",
    ) is None


def test_sqlite_bkt_history_rejects_identity_conflicts_and_missing_scope(
    tmp_path: Path,
) -> None:
    """Append-only BKT history must roll back changed retries."""

    from course_insight.infrastructure.sqlite.m5_repository import SQLiteM5Repository

    repository = SQLiteM5Repository(tmp_path / "m5-bkt-conflicts.sqlite3")
    repository.initialize()
    model = _model()
    trace = _engine().update(
        model,
        _sequence("learner_1", [True, False, True, True, False]),
    )

    assert repository.insert_or_get_bkt_model(model) == model
    assert repository.get_latest_bkt_model(
        course_id=model.course_id,
        class_id=model.class_id,
    ) == model
    assert repository.get_latest_bkt_model(
        course_id=model.course_id,
        class_id="missing_class",
    ) is None
    assert repository.get_bkt_model(
        course_id=model.course_id,
        model_version="missing_version",
    ) is None
    with pytest.raises(RuntimeError, match="BKT model conflict"):
        repository.insert_or_get_bkt_model(
            model.model_copy(update={"log_likelihood": -999.0})
        )

    assert repository.insert_or_get_knowledge_trace(trace) == trace
    assert repository.get_knowledge_trace(trace_id="missing_trace") is None
    with pytest.raises(RuntimeError, match="BKT trace conflict"):
        repository.insert_or_get_knowledge_trace(
            trace.model_copy(
                update={"concept_probabilities": {"concept_1": 0.123}}
            )
        )
    with pytest.raises(ValueError, match="course and class scope"):
        repository.insert_or_get_knowledge_trace(
            trace.model_copy(update={"course_id": None, "class_id": None})
        )
