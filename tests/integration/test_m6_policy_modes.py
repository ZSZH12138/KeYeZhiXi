"""Task 7 cross-mode and private payload compatibility regressions."""

from __future__ import annotations

from pathlib import Path

from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite import connect_sqlite
from course_insight.modules.m6_tutoring_fsm.policy_runtime import PolicyRuntime
from course_insight.modules.m6_tutoring_fsm.policy_types import PolicyOutcome
from course_insight.modules.m6_tutoring_fsm.rewards import reward_from_outcome
from course_insight.modules.m6_tutoring_fsm.service import (
    M6TutoringControlService,
)
from course_insight.modules.m6_tutoring_fsm.state_machine import (
    DEFAULT_STATE_MACHINE,
)
from tests.integration.test_m6_persistence import (
    _inputs,
    _only_request_fingerprint,
    _policy_execution,
    _repository,
    _snapshot,
)
from tests.unit.test_m6_tutoring_fsm import (
    _learned_runtime,
    _session,
    _valid_inputs,
)


def _service(
    database_path: Path,
    runtime: PolicyRuntime,
) -> M6TutoringControlService:
    return M6TutoringControlService(
        DEFAULT_STATE_MACHINE,
        _repository(database_path),
        policy_runtime=runtime,
    )


def test_rules_runtime_keeps_the_public_result_byte_for_byte_stable(
    tmp_path: Path,
) -> None:
    """Catch private policy plumbing changing the deterministic public payload."""

    implicit = _service(
        tmp_path / "implicit.sqlite3",
        PolicyRuntime(),
    ).decide_next_action(*_inputs(), _snapshot(0))
    explicit = _service(
        tmp_path / "explicit.sqlite3",
        PolicyRuntime(mode="rules"),
    ).decide_next_action(*_inputs(), _snapshot(0))

    assert dumps_json(implicit.to_dict()) == dumps_json(explicit.to_dict())
    assert implicit.content_checksum() == explicit.content_checksum()


def test_shadow_is_isolated_and_active_remains_inside_the_safety_envelope(
    tmp_path: Path,
) -> None:
    """Catch shadow control leakage or active selection outside safe candidates."""

    shadow_path = tmp_path / "shadow.sqlite3"
    shadow_service = _service(shadow_path, _learned_runtime("shadow"))
    mode_inputs = _valid_inputs()
    shadow_result = shadow_service.decide_next_action(
        *mode_inputs,
        _session("S1"),
    )
    shadow_request = _only_request_fingerprint(shadow_path)
    shadow_record = _repository(shadow_path).get_decision_by_request(
        shadow_request
    )

    assert shadow_result.next_state() == "S3"
    assert shadow_record is not None
    assert shadow_record.policy_observation is not None
    assert shadow_record.policy_observation.chosen_action_id.endswith(
        "s1_to_s3.v1"
    )
    assert shadow_record.policy_observation.shadow_action_id.endswith(
        "s1_to_s2.v1"
    )
    assert shadow_record.policy_observation.propensity == 1.0
    assert dict(
        shadow_record.policy_observation.action_probabilities
    ) == {
        "m6.transition.s1_to_s3.v1": 1.0,
        "m6.transition.s1_to_s2.v1": 0.0,
    }

    active_path = tmp_path / "active.sqlite3"
    active_result = _service(
        active_path,
        _learned_runtime("active"),
    ).decide_next_action(*mode_inputs, _session("S1"))
    active_record = _repository(active_path).get_decision_by_request(
        _only_request_fingerprint(active_path)
    )

    assert active_result.next_state() == "S2"
    assert active_result.teaching_action.must_not_reveal_answer is True
    assert (
        active_result.evidence_query.course_package_id
        == mode_inputs[0].course_package_id
    )
    assert active_record is not None
    assert active_record.policy_observation is not None
    assert active_record.policy_observation.decision_source == "active_policy"
    assert active_record.policy_observation.chosen_action_id.endswith(
        "s1_to_s2.v1"
    )


def test_new_rich_and_legacy_slim_payloads_round_trip_without_checksum_drift(
    tmp_path: Path,
) -> None:
    """Catch decoupled execution fingerprints breaking old or new SQLite rows."""

    database_path = tmp_path / "payloads.sqlite3"
    repository = _repository(database_path)
    result = M6TutoringControlService(
        DEFAULT_STATE_MACHINE,
        repository,
    ).decide_next_action(*_inputs(), _snapshot(0))
    request_key = _only_request_fingerprint(database_path)
    stored = repository.get_decision_by_request(request_key)

    assert stored is not None
    assert stored.result == result
    assert stored.policy_execution_ref is not None
    assert stored.policy_execution_ref.input_fingerprint == stored.input_fingerprint
    assert (
        stored.policy_execution_ref.policy_execution_fingerprint
        != stored.policy_execution_ref.identity
    )
    assert stored.policy_observation is not None
    assert stored.policy_observation.decision_id == stored.decision_id
    assert stored.policy_observation.input_fingerprint == stored.input_fingerprint
    assert stored.policy_observation.created_at == "2026-07-22T08:00:00+00:00"

    reward = reward_from_outcome(
        PolicyOutcome(
            policy_execution_fingerprint=(
                stored.policy_execution_ref.policy_execution_fingerprint
            ),
            status="observed",
            transfer_success=0.9,
            hint_count=1,
            loop_count=0,
            independent_correction_success=True,
            outcome_event_ids=("event-1",),
            outcome_watermark="event-1",
            observed_at="2026-07-27T13:00:00+00:00",
        )
    )
    assert repository.save_policy_reward(reward) == reward

    legacy = _policy_execution(request_fingerprint="f" * 64)
    assert repository.commit_policy_execution(legacy) == legacy
    restarted = _repository(database_path)
    assert restarted.get_decision_by_request(request_key) == stored
    assert restarted.get_policy_reward(
        reward.policy_execution_fingerprint,
        reward.reward_version,
    ) == reward
    assert restarted.get_policy_execution_by_request(
        legacy.request_fingerprint
    ) == legacy

    with connect_sqlite(database_path) as connection:
        rows = {
            str(row["request_fingerprint"]): row
            for row in connection.execute(
                """
                SELECT request_fingerprint,
                       policy_execution_fingerprint,
                       payload_checksum
                FROM m6_policy_executions
                """
            ).fetchall()
        }
    assert rows[request_key]["policy_execution_fingerprint"] != (
        rows[request_key]["payload_checksum"]
    )
    assert rows[legacy.request_fingerprint]["policy_execution_fingerprint"] == (
        rows[legacy.request_fingerprint]["payload_checksum"]
    )
