from datetime import datetime, timezone
from pathlib import Path

from course_insight.contracts.state import ConceptState, LearnerStateSnapshot
from course_insight.modules.m5_learner_class_state.aggregation import (
    DeterministicClassAggregationPolicy,
)
from course_insight.modules.m5_learner_class_state.update_policy import StatePolicy


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = PROJECT_ROOT / "config"


def test_configured_class_aggregation_policy_fits_three_active_students() -> None:
    active_students = (
        "pseudonym_policy_student_001",
        "pseudonym_policy_student_002",
        "pseudonym_policy_student_003",
    )
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    learner_states = [
        LearnerStateSnapshot(
            snapshot_id=f"{actor_id}_state_v1",
            course_id="course_network",
            class_id="class_01",
            learner_id=actor_id,
            state_version=1,
            concept_states=[
                ConceptState(
                    concept_id="concept_network",
                    mastery_probability=0.5,
                    mastery_confidence=1.0,
                    misconceptions=[],
                    hint_dependency=0.0,
                    recent_correction_rate=0.0,
                    evidence_count=1,
                    updated_at=now,
                )
            ],
            overall_mastery=0.5,
            evidence_count=1,
            updated_at=now,
        )
        for actor_id in active_students
    ]

    snapshot = DeterministicClassAggregationPolicy().aggregate_all(
        learner_states,
        StatePolicy.from_path(CONFIG_ROOT / "state.json"),
        class_version=1,
    )

    assert snapshot.assessed_count == 3
    assert snapshot.class_size == 3
    assert snapshot.coverage_rate == 1.0
