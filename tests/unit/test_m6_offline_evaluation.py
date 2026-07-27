"""De-identified offline dataset and deterministic M6 OPE tests."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from course_insight.infrastructure.sqlite.m6_repository import SQLiteM6Repository
from course_insight.modules.m6_tutoring_fsm.offline_dataset import (
    EXPORT_FIELDS,
    OfflinePolicyRow,
    build_offline_row,
    canonical_jsonl,
    dataset_identity,
    grouped_time_split,
)
from course_insight.modules.m6_tutoring_fsm.offline_evaluation import (
    OPEConfig,
    evaluate_offline_policy,
    persist_evaluation,
)
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    PolicyEvaluationRecord,
    PolicyObservation,
    PolicyRewardRecord,
)


def _row(
    *,
    fingerprint: str,
    raw_group_id: str,
    raw_session_id: str,
    event_time: int,
    state: str = "S2",
    selected_action: str = "action-a",
    candidate_actions: tuple[str, ...] = ("action-a", "action-b"),
    logging_propensity: float = 0.5,
    reward: float = 1.0,
    target_propensities: dict[str, float] | None = None,
    direct_estimates: dict[str, float] | None = None,
) -> OfflinePolicyRow:
    observation = PolicyObservation(
        policy_execution_fingerprint=fingerprint,
        request_fingerprint=f"request-{fingerprint}",
        feature_schema_version="m6-features-v1",
        candidate_ids=candidate_actions,
        selected_candidate_id=selected_action,
        propensity=logging_propensity,
    )
    reward_record = PolicyRewardRecord(
        policy_execution_fingerprint=fingerprint,
        outcome_identity=f"outcome-{fingerprint}",
        status="observed",
        reward=reward,
    )
    return build_offline_row(
        observation,
        reward_record,
        deidentification_key=b"k" * 32,
        raw_group_id=raw_group_id,
        raw_session_id=raw_session_id,
        event_time=event_time,
        state=state,
        target_propensities=(
            {"action-a": 0.75, "action-b": 0.25}
            if target_propensities is None
            else target_propensities
        ),
        direct_estimates=(
            {"action-a": 0.8, "action-b": 0.2}
            if direct_estimates is None
            else direct_estimates
        ),
    )


def _relaxed_config(**overrides: object) -> OPEConfig:
    values: dict[str, object] = {
        "minimum_rows": 1,
        "minimum_effective_sample_size": 0.0,
        "minimum_action_coverage": 0.0,
        "minimum_support_coverage": 0.0,
        "minimum_logging_propensity": 0.0,
        "approval_minimum_dr": -10.0,
        "bootstrap_samples": 40,
        "confidence_level": 0.90,
    }
    values.update(overrides)
    return OPEConfig(**values)  # type: ignore[arg-type]


def test_jsonl_export_has_an_exact_allowlist_and_no_raw_identity_or_text() -> None:
    """Catch raw identity, host paths, free text, or accidental fields in export."""

    raw_group = "student@example.edu"
    raw_session = r"C:\Users\Teacher\answers\attempt-1"
    execution = "e" * 64
    row = _row(
        fingerprint=execution,
        raw_group_id=raw_group,
        raw_session_id=raw_session,
        event_time=10,
    )

    exported = canonical_jsonl((row,))
    payload = json.loads(exported)

    assert set(payload) == set(EXPORT_FIELDS)
    assert raw_group not in exported
    assert raw_session not in exported
    assert execution not in exported
    assert "request-" not in exported
    assert len(payload["group_id"]) == 64
    assert len(payload["session_id"]) == 64
    assert exported == (
        '{"candidate_actions":["action-a","action-b"],'
        '"direct_estimates":{"action-a":0.8,"action-b":0.2},'
        '"event_time":10,"group_id":"d7c98d760a51d0851a12322747314d43'
        'e925a657f1fa39bd41d5bc5db5070725","logging_propensity":0.5,'
        '"reward":1.0,"selected_action":"action-a",'
        '"session_id":"313c41bdbf50933fdf2cc287ce49296d4ef1d423bc6202c267'
        'aad1adfe6367f3","state":"S2",'
        '"target_propensities":{"action-a":0.75,"action-b":0.25}}'
    )
    assert dataset_identity((row,)) == (
        "774a14186eba841449c7a4697375c234255546665fbd4fdabda3338b2b8174fb"
    )


def test_dataset_rejects_free_text_or_host_paths_disguised_as_actions() -> None:
    """Catch non-structured answer text or host paths crossing the allowlist."""

    with pytest.raises(ValueError, match="candidate"):
        _row(
            fingerprint="x" * 64,
            raw_group_id="learner",
            raw_session_id="session",
            event_time=1,
            selected_action=r"C:\Users\Teacher\answer.txt",
            candidate_actions=(r"C:\Users\Teacher\answer.txt",),
            target_propensities={r"C:\Users\Teacher\answer.txt": 1.0},
            direct_estimates={r"C:\Users\Teacher\answer.txt": 0.5},
        )


def test_deidentification_is_keyed_and_rejects_weak_keys() -> None:
    """Catch low-entropy identities being reversible through a public hash."""

    observation = PolicyObservation(
        policy_execution_fingerprint="keyed-row",
        request_fingerprint="request-keyed-row",
        feature_schema_version="m6-features-v1",
        candidate_ids=("action-a",),
        selected_candidate_id="action-a",
        propensity=1.0,
    )
    reward = PolicyRewardRecord(
        policy_execution_fingerprint="keyed-row",
        outcome_identity="outcome-keyed-row",
        status="observed",
        reward=1.0,
    )
    values = {
        "raw_group_id": "student@example.edu",
        "raw_session_id": "predictable-session",
        "event_time": 1,
        "state": "S2",
        "target_propensities": {"action-a": 1.0},
        "direct_estimates": {"action-a": 0.5},
    }

    first = build_offline_row(
        observation,
        reward,
        deidentification_key=b"a" * 32,
        **values,  # type: ignore[arg-type]
    )
    second = build_offline_row(
        observation,
        reward,
        deidentification_key=b"b" * 32,
        **values,  # type: ignore[arg-type]
    )

    assert first.group_id != second.group_id
    assert first.session_id != second.session_id
    assert (b"a" * 32).hex() not in canonical_jsonl((first,))
    with pytest.raises(ValueError, match="deidentification_key"):
        build_offline_row(
            observation,
            reward,
            deidentification_key=b"short",
            **values,  # type: ignore[arg-type]
        )


def test_grouped_time_split_keeps_every_group_and_session_on_one_side() -> None:
    """Catch group/session leakage or selecting old groups for evaluation."""

    rows = (
        _row(
            fingerprint="a1",
            raw_group_id="group-a",
            raw_session_id="session-a1",
            event_time=1,
        ),
        _row(
            fingerprint="a2",
            raw_group_id="group-a",
            raw_session_id="session-a2",
            event_time=2,
        ),
        _row(
            fingerprint="b1",
            raw_group_id="group-b",
            raw_session_id="session-b",
            event_time=10,
        ),
        _row(
            fingerprint="c1",
            raw_group_id="group-c",
            raw_session_id="session-c",
            event_time=20,
        ),
    )

    split = grouped_time_split(rows, evaluation_fraction=1 / 3)

    train_groups = {row.group_id for row in split.train}
    evaluation_groups = {row.group_id for row in split.evaluation}
    assert train_groups.isdisjoint(evaluation_groups)
    assert {row.session_id for row in split.train}.isdisjoint(
        row.session_id for row in split.evaluation
    )
    assert [row.event_time for row in split.evaluation] == [20]
    assert max(row.event_time for row in split.train) < min(
        row.event_time for row in split.evaluation
    )


def test_grouped_time_split_rejects_overlapping_whole_group_intervals() -> None:
    """Catch max-time ordering silently leaking future rows into training."""

    rows = (
        _row(
            fingerprint="a-early",
            raw_group_id="group-a",
            raw_session_id="session-a",
            event_time=1,
        ),
        _row(
            fingerprint="a-late",
            raw_group_id="group-a",
            raw_session_id="session-a",
            event_time=100,
        ),
        _row(
            fingerprint="b-early",
            raw_group_id="group-b",
            raw_session_id="session-b",
            event_time=50,
        ),
        _row(
            fingerprint="b-late",
            raw_group_id="group-b",
            raw_session_id="session-b",
            event_time=60,
        ),
    )

    with pytest.raises(ValueError, match="time boundary"):
        grouped_time_split(rows, evaluation_fraction=0.5)


def test_ope_estimators_match_independent_hand_calculation_and_emit_slices() -> None:
    """Catch incorrect IPS, SNIPS, DM, DR, ESS, coverage, or slice formulas."""

    rows = (
        _row(
            fingerprint="row-1",
            raw_group_id="group-1",
            raw_session_id="session-1",
            event_time=1,
            state="S2",
            selected_action="action-a",
            logging_propensity=0.5,
            reward=1.0,
            target_propensities={"action-a": 0.75, "action-b": 0.25},
            direct_estimates={"action-a": 0.8, "action-b": 0.2},
        ),
        _row(
            fingerprint="row-2",
            raw_group_id="group-2",
            raw_session_id="session-2",
            event_time=2,
            state="S2",
            selected_action="action-b",
            logging_propensity=0.5,
            reward=0.0,
            target_propensities={"action-a": 0.25, "action-b": 0.75},
            direct_estimates={"action-a": 0.4, "action-b": 0.1},
        ),
    )

    evaluation = evaluate_offline_policy(
        "policy-candidate",
        rows,
        config=_relaxed_config(approval_minimum_dr=0.0),
    )
    metrics = dict(evaluation.metrics)

    assert metrics["ips"] == pytest.approx(0.75)
    assert metrics["snips"] == pytest.approx(0.50)
    assert metrics["dm"] == pytest.approx(0.4125)
    assert metrics["dr"] == pytest.approx(0.4875)
    assert evaluation.effective_sample_size == pytest.approx(2.0)
    assert evaluation.action_coverage == pytest.approx(1.0)
    assert evaluation.support_coverage == pytest.approx(1.0)
    assert evaluation.status == "sufficient_data"
    assert evaluation.approved is True
    assert {key for key, _, _ in evaluation.state_slices} == {"S2"}
    assert len(evaluation.group_slices) == 2


def test_target_support_cannot_be_borrowed_from_another_tutoring_state() -> None:
    """Catch global action coverage hiding an unsupported state-action pair."""

    rows = (
        _row(
            fingerprint="s2-action-a",
            raw_group_id="group-1",
            raw_session_id="session-1",
            event_time=1,
            state="S2",
            selected_action="action-a",
            target_propensities={"action-a": 0.5, "action-b": 0.5},
        ),
        _row(
            fingerprint="s4-action-b",
            raw_group_id="group-2",
            raw_session_id="session-2",
            event_time=2,
            state="S4",
            selected_action="action-b",
            candidate_actions=("action-b",),
            target_propensities={"action-b": 1.0},
            direct_estimates={"action-b": 0.2},
        ),
    )

    evaluation = evaluate_offline_policy(
        "policy-candidate",
        rows,
        config=_relaxed_config(),
    )

    assert evaluation.action_coverage == pytest.approx(2 / 3)
    assert evaluation.support_coverage == pytest.approx(2 / 3)
    assert evaluation.status == "insufficient_data"
    assert evaluation.approved is False
    assert "poor_action_coverage" in evaluation.safety_reasons
    assert "low_support" in evaluation.safety_reasons


def test_one_supported_row_cannot_hide_a_low_propensity_row_for_same_pair() -> None:
    """Catch set coverage letting a high-support duplicate mask low support."""

    rows = (
        _row(
            fingerprint="s2-action-a-low",
            raw_group_id="group-1",
            raw_session_id="session-1",
            event_time=1,
            state="S2",
            selected_action="action-a",
            candidate_actions=("action-a",),
            logging_propensity=0.001,
            target_propensities={"action-a": 1.0},
            direct_estimates={"action-a": 0.5},
        ),
        _row(
            fingerprint="s2-action-a-high",
            raw_group_id="group-2",
            raw_session_id="session-2",
            event_time=2,
            state="S2",
            selected_action="action-a",
            candidate_actions=("action-a",),
            logging_propensity=1.0,
            target_propensities={"action-a": 1.0},
            direct_estimates={"action-a": 0.5},
        ),
    )

    evaluation = evaluate_offline_policy(
        "policy-candidate",
        rows,
        config=_relaxed_config(minimum_logging_propensity=0.01),
    )

    assert evaluation.action_coverage == pytest.approx(1.0)
    assert evaluation.support_coverage == pytest.approx(1.0)
    assert evaluation.status == "insufficient_data"
    assert evaluation.approved is False
    assert "low_support" in evaluation.safety_reasons


def test_bootstrap_is_reproducible_from_canonical_dataset_identity() -> None:
    """Catch process randomness or input ordering changing bootstrap intervals."""

    rows = (
        _row(
            fingerprint="row-1",
            raw_group_id="group-1",
            raw_session_id="session-1",
            event_time=1,
        ),
        _row(
            fingerprint="row-2",
            raw_group_id="group-2",
            raw_session_id="session-2",
            event_time=2,
            selected_action="action-b",
            reward=0.0,
        ),
    )
    config = _relaxed_config(bootstrap_samples=75)

    first = evaluate_offline_policy("policy-candidate", rows, config=config)
    reordered = evaluate_offline_policy(
        "policy-candidate",
        tuple(reversed(rows)),
        config=config,
    )

    assert first.dataset_identity == dataset_identity(rows)
    assert first.confidence_intervals == reordered.confidence_intervals
    assert first.identity == reordered.identity


@pytest.mark.parametrize(
    ("rows_factory", "config", "expected_reason"),
    [
        (
            lambda: (
                replace(
                    _row(
                        fingerprint="missing",
                        raw_group_id="g1",
                        raw_session_id="s1",
                        event_time=1,
                    ),
                    logging_propensity=None,
                ),
            ),
            _relaxed_config(minimum_support_coverage=1.0),
            "invalid_propensity",
        ),
        (
            lambda: (
                _row(
                    fingerprint="low-support",
                    raw_group_id="g1",
                    raw_session_id="s1",
                    event_time=1,
                    logging_propensity=0.001,
                ),
            ),
            _relaxed_config(minimum_logging_propensity=0.01),
            "low_support",
        ),
        (
            lambda: (
                _row(
                    fingerprint="weight-100",
                    raw_group_id="g1",
                    raw_session_id="s1",
                    event_time=1,
                    candidate_actions=("action-a",),
                    selected_action="action-a",
                    logging_propensity=0.01,
                    target_propensities={"action-a": 1.0},
                    direct_estimates={"action-a": 0.5},
                ),
                _row(
                    fingerprint="weight-1",
                    raw_group_id="g2",
                    raw_session_id="s2",
                    event_time=2,
                    candidate_actions=("action-a",),
                    selected_action="action-a",
                    logging_propensity=1.0,
                    target_propensities={"action-a": 1.0},
                    direct_estimates={"action-a": 0.5},
                ),
            ),
            _relaxed_config(minimum_effective_sample_size=1.5),
            "low_effective_sample_size",
        ),
        (
            lambda: (
                _row(
                    fingerprint="only-a-1",
                    raw_group_id="g1",
                    raw_session_id="s1",
                    event_time=1,
                    selected_action="action-a",
                    target_propensities={"action-a": 0.5, "action-b": 0.5},
                ),
                _row(
                    fingerprint="only-a-2",
                    raw_group_id="g2",
                    raw_session_id="s2",
                    event_time=2,
                    selected_action="action-a",
                    target_propensities={"action-a": 0.5, "action-b": 0.5},
                ),
            ),
            _relaxed_config(minimum_action_coverage=0.75),
            "poor_action_coverage",
        ),
    ],
)
def test_unreliable_ope_is_insufficient_and_can_never_approve_active(
    rows_factory: object,
    config: OPEConfig,
    expected_reason: str,
) -> None:
    """Catch invalid propensity, low support/ESS, or poor coverage approving active."""

    rows = rows_factory()  # type: ignore[operator]
    evaluation = evaluate_offline_policy(
        "policy-candidate",
        rows,
        config=config,
    )

    assert evaluation.status == "insufficient_data"
    assert evaluation.approved is False
    assert expected_reason in evaluation.safety_reasons


def test_complete_evaluation_payload_round_trips_through_m6_private_repository(
    tmp_path: Path,
) -> None:
    """Catch Task4 persistence dropping metrics, intervals, slices, or verdict."""

    rows = (
        _row(
            fingerprint="row-1",
            raw_group_id="group-1",
            raw_session_id="session-1",
            event_time=1,
        ),
        _row(
            fingerprint="row-2",
            raw_group_id="group-2",
            raw_session_id="session-2",
            event_time=2,
            selected_action="action-b",
            reward=0.0,
        ),
    )
    evaluation = evaluate_offline_policy(
        "policy-candidate",
        rows,
        config=_relaxed_config(),
    )
    repository = SQLiteM6Repository(tmp_path / "m6.sqlite3")
    repository.initialize()

    stored = persist_evaluation(repository, evaluation)
    restarted = SQLiteM6Repository(tmp_path / "m6.sqlite3")
    loaded = restarted.get_policy_evaluation(
        evaluation.policy_id,
        evaluation.dataset_identity,
    )

    assert stored == evaluation
    assert loaded == evaluation
    assert loaded is not None
    assert loaded.metrics
    assert loaded.confidence_intervals
    assert loaded.state_slices
    assert loaded.group_slices


def test_legacy_summary_replay_is_append_only_and_requires_exact_old_fields(
    tmp_path: Path,
) -> None:
    """Catch a rich recomputation conflicting with its exact legacy summary."""

    rows = (
        _row(
            fingerprint="legacy-row-a",
            raw_group_id="group-1",
            raw_session_id="session-1",
            event_time=1,
        ),
        _row(
            fingerprint="legacy-row-b",
            raw_group_id="group-2",
            raw_session_id="session-2",
            event_time=2,
            selected_action="action-b",
            reward=0.0,
        ),
    )
    rich = evaluate_offline_policy(
        "policy-candidate",
        rows,
        config=_relaxed_config(),
    )
    legacy = PolicyEvaluationRecord(
        policy_id=rich.policy_id,
        dataset_identity=rich.dataset_identity,
        status=rich.status,
        approved=rich.approved,
        effective_sample_size=rich.effective_sample_size,
        action_coverage=rich.action_coverage,
        observation_count=rich.observation_count,
    )
    repository = SQLiteM6Repository(tmp_path / "legacy.sqlite3")
    repository.initialize()
    repository.save_policy_evaluation(legacy)

    replay = persist_evaluation(repository, rich)

    assert replay == legacy
    assert replay.metrics == ()
    assert repository.get_policy_evaluation(
        rich.policy_id,
        rich.dataset_identity,
    ) == legacy

    conflicting_repository = SQLiteM6Repository(
        tmp_path / "legacy-conflict.sqlite3"
    )
    conflicting_repository.initialize()
    conflicting_repository.save_policy_evaluation(
        replace(
            legacy,
            effective_sample_size=legacy.effective_sample_size + 1.0,
        )
    )
    with pytest.raises(ValueError, match="conflict"):
        persist_evaluation(conflicting_repository, rich)
