"""M5 atomic state history and v14 runtime schema tests."""

import sqlite3

import pytest

from course_insight.contracts.state import (
    DiagnosisResult,
    ItemDiagnosis,
    StateUpdateResult,
)
from course_insight.infrastructure.postgresql.migration_runner import load_migrations
from course_insight.infrastructure.sqlite.m5_repository import SQLiteM5Repository
from course_insight.infrastructure.sqlite.migrations import SCHEMA_VERSION
from course_insight.modules.m5_learner_class_state.aggregation import (
    DeterministicClassAggregationPolicy,
)
from tests.factories.m5_m8 import UTC_TIME
from tests.unit.test_m5_class_replacement import _learner, _policy


def _state_result() -> StateUpdateResult:
    learner = _learner("learner_1", 0.6)
    class_state = DeterministicClassAggregationPolicy().aggregate(
        learner,
        None,
        _policy(),
    )
    return StateUpdateResult(
        diagnosis_result=DiagnosisResult(
            diagnosis_id="diagnosis_1",
            attempt_id="attempt_1",
            learner_id="learner_1",
            item_diagnoses=[
                ItemDiagnosis(
                    item_instance_id="opaque-A",
                    concept_ids=["concept_1"],
                    misconception_ids=[],
                    error_type="correct",
                    confidence=1.0,
                    evidence_audit_ids=["audit_1:1"],
                    prerequisite_gap_ids=[],
                )
            ],
            priority_concept_ids=["concept_1"],
            priority_misconception_ids=[],
            generated_at=UTC_TIME,
        ),
        learner_state_snapshot=learner,
        class_state_snapshot=class_state,
        processed_audit_ids=["audit_1:1"],
        updated_at=UTC_TIME,
    )


def test_failed_atomic_update_leaves_no_partial_rows(tmp_path, monkeypatch) -> None:
    repository = SQLiteM5Repository(tmp_path / "state.sqlite3")
    repository.initialize()
    result = _state_result()

    def fail_class_insert(*_args, **_kwargs) -> None:
        raise RuntimeError("injected class insert failure")

    monkeypatch.setattr(repository, "_insert_or_validate_class", fail_class_insert)

    with pytest.raises(RuntimeError, match="injected class insert failure"):
        repository.insert_or_get_state_update(result)

    assert repository.get_state_update("attempt_1") is None
    assert repository.get_latest_learner_state(
        "course_1", "class_1", "learner_1"
    ) is None


def test_stale_class_baseline_leaves_no_partial_rows(tmp_path) -> None:
    repository = SQLiteM5Repository(tmp_path / "baseline.sqlite3")
    repository.initialize()
    result = _state_result()

    with pytest.raises(RuntimeError, match="class-state baseline conflict"):
        repository.insert_or_get_state_update(
            result,
            expected_previous_class_snapshot_id="stale_snapshot",
        )

    assert repository.get_state_update("attempt_1") is None
    assert repository.get_latest_learner_state(
        "course_1", "class_1", "learner_1"
    ) is None


def test_v14_creates_all_m5_m8_model_runtime_tables(tmp_path) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    repository = SQLiteM5Repository(database_path)
    repository.initialize()
    expected = {
        "m5_learning_observations",
        "m5_dina_models",
        "m5_bkt_models",
        "m5_knowledge_traces",
        "m8_irt_calibration_runs",
        "m8_irt_parameter_sets",
        "m8_ability_estimates",
        "m8_adaptive_selections",
        "m8_calibration_reviews",
    }
    with sqlite3.connect(database_path) as connection:
        actual = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert SCHEMA_VERSION == 14
    assert expected <= actual
    assert load_migrations()[-1].path.name == "0014_m5_m8_model_runtime.sql"
