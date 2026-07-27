from __future__ import annotations

from dataclasses import replace
import math

import pytest

from course_insight.modules.m6_tutoring_fsm.decision_policy import DecisionSignals
from course_insight.modules.m6_tutoring_fsm.policy_adapter import RulesPolicyAdapter
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    CandidateAction,
    TutoringPolicyContext,
)
from course_insight.modules.m6_tutoring_fsm.linucb import (
    LinUCBModel,
    LinUCBPolicyAdapter,
    select_with_epsilon,
)
from course_insight.modules.m6_tutoring_fsm.policy_artifacts import (
    LoadedPolicyArtifact,
)
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    PolicyArtifactManifest,
)


def _context(**signal_overrides: bool) -> TutoringPolicyContext:
    signals = DecisionSignals(
        needs_teacher_review=False,
        has_diagnosed_misconception=False,
        has_active_misconception=False,
        has_prerequisite_gap=False,
        has_new_evidence=True,
        minimum_recent_correction_rate=1.0,
        minimum_mastery_confidence=1.0,
        maximum_hint_dependency=0.0,
    )
    if signal_overrides:
        signals = DecisionSignals(
            **{name: getattr(signals, name) for name in signals.__dataclass_fields__}
            | signal_overrides
        )
    return TutoringPolicyContext(
        request_fingerprint="request-1",
        current_state="S1",
        task_type="practice",
        turn_count=1,
        score_ratio=0.5,
        target_concept_count=1,
        signals=signals,
        learner_evidence_count=1,
        course_id="course-1",
        class_id="class-1",
    )


def _candidate(candidate_id: str, next_state: str = "S3") -> CandidateAction:
    return CandidateAction(
        candidate_id=candidate_id,
        next_state=next_state,
        action_type="guided_question",
        prompt_template_id="m6.test.v1",
        exploration_allowed=True,
    )


def test_rules_adapter_matches_existing_baseline_and_cannot_escape_candidates() -> None:
    context = _context()
    safe_candidates = (_candidate("guided", "S3"), _candidate("hint", "S2"))

    decision = RulesPolicyAdapter().select(context, safe_candidates)

    assert decision.mode == "rules"
    assert decision.selected_candidate_id == "guided"
    assert decision.candidate_ids == ("guided", "hint")
    assert decision.prediction is None

    with pytest.raises(ValueError, match="baseline action"):
        RulesPolicyAdapter().select(context, (_candidate("hint", "S2"),))


def test_linucb_scores_by_hand_and_breaks_ties_by_candidate_order() -> None:
    # theta·x = 1*3 + 2*4 = 11; sqrt(xᵀIx) = 5, so score = 13.5.
    model = LinUCBModel(
        policy_id="policy-1",
        alpha=0.5,
        dimension=2,
        actions={
            "first": {"theta": [1.0, 2.0], "inverse_covariance": [[1.0, 0.0], [0.0, 1.0]]},
            "second": {"theta": [1.0, 2.0], "inverse_covariance": [[1.0, 0.0], [0.0, 1.0]]},
        },
    )

    ranked = model.rank((3.0, 4.0), (_candidate("first"), _candidate("second")))

    assert [prediction.candidate_id for prediction in ranked] == ["first", "second"]
    assert [prediction.score for prediction in ranked] == [13.5, 13.5]
    assert [prediction.uncertainty for prediction in ranked] == [5.0, 5.0]
    assert model.choose((3.0, 4.0), (_candidate("first"), _candidate("second"))).candidate_id == "first"


@pytest.mark.parametrize(
    "actions, features",
    [
        ({"first": {"theta": [1.0], "inverse_covariance": [[1.0]]}}, (1.0, 2.0)),
        ({"first": {"theta": [math.inf, 1.0], "inverse_covariance": [[1.0, 0.0], [0.0, 1.0]]}}, (1.0, 2.0)),
        ({"first": {"theta": [1.0, 1.0], "inverse_covariance": [[1.0, 0.0]]}}, (1.0, 2.0)),
    ],
)
def test_linucb_rejects_dimension_or_nonfinite_inputs(actions: dict[str, object], features: tuple[float, ...]) -> None:
    with pytest.raises(ValueError):
        LinUCBModel(policy_id="policy-1", alpha=0.1, dimension=2, actions=actions)


def test_epsilon_selection_records_exact_propensity_and_sums_to_one() -> None:
    candidates = (_candidate("winner"), _candidate("other"))
    decision = select_with_epsilon(
        policy_id="policy-1",
        request_fingerprint="request-1",
        candidates=candidates,
        scores={"winner": 2.0, "other": 1.0},
        uncertainties={"winner": 0.25, "other": 0.5},
        epsilon=0.2,
        context=_context(),
    )

    expected = {"winner": 0.9, "other": 0.1}
    assert decision.prediction is not None
    assert decision.prediction.propensity == expected[decision.selected_candidate_id]
    assert decision.prediction.uncertainty == {
        "winner": 0.25,
        "other": 0.5,
    }[decision.selected_candidate_id]
    assert sum(expected.values()) == 1.0


def test_epsilon_exploration_is_banned_for_remediation_and_single_candidate() -> None:
    candidates = (_candidate("winner"), _candidate("other"))
    remediating = select_with_epsilon(
        policy_id="policy-1",
        request_fingerprint="request-1",
        candidates=candidates,
        scores={"winner": 2.0, "other": 1.0},
        uncertainties={"winner": 0.1, "other": 0.2},
        epsilon=1.0,
        context=_context(has_prerequisite_gap=True),
    )
    single = select_with_epsilon(
        policy_id="policy-1",
        request_fingerprint="request-1",
        candidates=(candidates[0],),
        scores={"winner": 2.0},
        uncertainties={"winner": 0.1},
        epsilon=1.0,
        context=_context(),
    )

    assert remediating.selected_candidate_id == "winner"
    assert remediating.prediction is not None and remediating.prediction.propensity == 1.0
    assert single.prediction is not None and single.prediction.propensity == 1.0


def test_production_linucb_adapter_builds_features_and_scores_safe_subset() -> None:
    dimension = 23
    identity = [
        [1.0 if row == column else 0.0 for column in range(dimension)]
        for row in range(dimension)
    ]
    actions = {
        "guided": {
            "theta": [0.0, 2.0, *([0.0] * (dimension - 2))],
            "inverse_covariance": identity,
        },
        "hint": {
            "theta": [0.0] * dimension,
            "inverse_covariance": identity,
        },
        "unused-full-action": {
            "theta": [0.0] * dimension,
            "inverse_covariance": identity,
        },
    }
    manifest = PolicyArtifactManifest(
        policy_id="policy-1",
        adapter_id="m6-linucb-adapter",
        adapter_version="v1",
        algorithm="linucb",
        state_graph_version="m6-state-graph-v1",
        baseline_policy_version="m6-policy-v1",
        feature_schema_version="m6-features-v1",
        action_space_version="m6-action-space-v1",
        reward_version="m6-reward-v1",
        gate_policy_version="m6-active-gate-v1",
        training_data_watermark="2026-07-27T00:00:00Z",
        training_data_checksum="b" * 64,
        artifact_sha256="a" * 64,
        status="approved",
        created_at="2026-07-27T01:00:00Z",
        artifact_reference="model.json",
        allowed_scopes=("course:course-1", "class:class-1"),
    )
    loaded = LoadedPolicyArtifact(
        manifest=manifest,
        payload={
            "policy_id": "policy-1",
            "adapter_id": "m6-linucb-adapter",
            "adapter_version": "v1",
            "feature_schema_version": "m6-features-v1",
            "action_space_version": "m6-action-space-v1",
            "dimension": dimension,
            "alpha": 0.1,
            "actions": actions,
        },
    )
    adapter = LinUCBPolicyAdapter(loaded, exploration_rate=0.0)

    decision = adapter.select(
        _context(),
        (_candidate("guided", "S3"), _candidate("hint", "S2")),
    )

    assert decision.selected_candidate_id == "guided"
    assert decision.prediction is not None
    assert decision.prediction.score > 2.0
    assert decision.prediction.uncertainty > 0.0
    with pytest.raises(ValueError, match="action parameters"):
        adapter.select(_context(), (_candidate("unknown", "S3"),))
    incompatible = LoadedPolicyArtifact(
        manifest=replace(
            manifest,
            feature_schema_version="m6-features-v2",
        ),
        payload={
            **loaded.payload,
            "feature_schema_version": "m6-features-v2",
        },
    )
    with pytest.raises(ValueError, match="feature schema"):
        LinUCBPolicyAdapter(incompatible, exploration_rate=0.0)
