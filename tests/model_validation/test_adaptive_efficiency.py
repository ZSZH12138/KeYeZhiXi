"""Fixed-seed validation for constrained adaptive item selection."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np

from course_insight.contracts.knowledge import ItemCard
from course_insight.contracts.learning_models import (
    AbilityEstimate,
    AdaptiveSelectionPolicy,
    IRTItemParameters,
    IRTParameterSet,
    ItemExposureSnapshot,
)
from course_insight.modules.m8_assessment_scoring.adaptive_selector import (
    AdaptiveItemSelector,
)


NOW = datetime(2026, 8, 12, 16, 0, tzinfo=UTC)
SEED = 20260812


def _item(item_id: str, concept_id: str) -> ItemCard:
    return ItemCard(
        item_id=item_id,
        version="1.0.0",
        stem=f"Simulation item {item_id}",
        item_type="multiple_choice",
        concept_ids=[concept_id],
        misconception_ids=[],
        difficulty_level=2,
        cognitive_level="apply",
        parameter_rules=[],
        answer_key={"answer": "A", "max_score": 1.0},
        rubric_id=None,
        source_evidence_ids=[f"evidence_{item_id}"],
        status="teacher_approved",
    )


def _items_and_parameters() -> tuple[list[ItemCard], IRTParameterSet]:
    items: list[ItemCard] = []
    parameters: list[IRTItemParameters] = []
    for index in range(200):
        item_id = f"item_{index:03d}"
        concept_id = f"concept_{index % 4}"
        difficulty = -4.0 + 8.0 * index / 199.0
        discrimination = 0.8 + 1.6 * ((index * 37) % 101) / 100.0
        items.append(_item(item_id, concept_id))
        parameters.append(
            IRTItemParameters(
                item_id=item_id,
                item_version="1.0.0",
                discrimination=discrimination,
                difficulty=difficulty,
                guessing=0.0,
                sample_size=1000,
            )
        )
    return items, IRTParameterSet(
        parameter_set_id="irt_approved_simulation",
        model_type="2PL",
        version="irt-simulation-v1",
        item_parameters=parameters,
        sample_size=1000,
        status="approved",
        created_at=NOW,
    )


def _items_needed(
    item_ids: list[str],
    *,
    theta: float,
    parameters: dict[str, IRTItemParameters],
    target_information: float,
) -> int:
    information = 0.0
    for index, item_id in enumerate(item_ids, start=1):
        item = parameters[item_id]
        information += AdaptiveItemSelector.information(
            theta=theta,
            discrimination=item.discrimination,
            difficulty=item.difficulty,
        )
        if information >= target_information:
            return index
    return len(item_ids)


def test_adaptive_selection_meets_constraints_and_reduces_test_length() -> None:
    """Catch a constrained selector that offers no efficiency over fixed order."""

    rng = np.random.default_rng(SEED)
    items, parameter_set = _items_and_parameters()
    parameter_by_id = {
        item.item_id: item for item in parameter_set.item_parameters
    }
    policy = AdaptiveSelectionPolicy(
        policy_id="adaptive-simulation-policy",
        version="1.0.0",
        parameter_set_id=parameter_set.parameter_set_id,
        max_items=40,
        concept_quotas={f"concept_{index}": 1 for index in range(4)},
        max_item_exposure_rate=0.20,
        difficulty_range=(-4.0, 4.0),
        status="configured",
    )
    selector = AdaptiveItemSelector()
    counts: dict[str, int] = {}
    total_sessions = 1000
    adaptive_lengths: list[int] = []
    fixed_lengths: list[int] = []
    exposure_violations = 0
    quota_failures = 0
    duplicate_count = 0
    target_information = 6.25
    fixed_order = [item.item_id for item in items]

    for learner_index, theta_value in enumerate(
        np.clip(rng.normal(0.0, 1.0, size=500), -3.0, 3.0)
    ):
        theta = float(theta_value)
        exposure = ItemExposureSnapshot(
            parameter_set_id=parameter_set.parameter_set_id,
            total_sessions=total_sessions,
            item_administered_counts=dict(counts),
        )
        ability = AbilityEstimate(
            estimate_id=f"ability_sim_{learner_index}",
            learner_id=f"learner_{learner_index}",
            parameter_set_id=parameter_set.parameter_set_id,
            theta=theta,
            standard_error=1.0,
            status="estimated",
            estimated_at=NOW,
        )
        result = selector.select(
            policy=policy,
            ability_estimate=ability,
            parameter_set=parameter_set,
            candidate_items=items,
            administered_item_ids=frozenset(),
            exposure_snapshot=exposure,
            requested_at=NOW + timedelta(seconds=learner_index),
        )

        selected_set = set(result.item_ids)
        duplicate_count += len(result.item_ids) - len(selected_set)
        selected_concepts = {
            concept_id
            for item in items
            if item.item_id in selected_set
            for concept_id in item.concept_ids
        }
        quota_failures += int(
            any(concept_id not in selected_concepts for concept_id in policy.concept_quotas)
        )
        exposure_violations += sum(
            int(
                counts.get(item_id, 0) / total_sessions
                >= policy.max_item_exposure_rate
            )
            for item_id in result.item_ids
        )
        adaptive_lengths.append(
            _items_needed(
                result.item_ids,
                theta=theta,
                parameters=parameter_by_id,
                target_information=target_information,
            )
        )
        fixed_lengths.append(
            _items_needed(
                fixed_order,
                theta=theta,
                parameters=parameter_by_id,
                target_information=target_information,
            )
        )
        counts = {
            **counts,
            **{
                item_id: counts.get(item_id, 0) + 1
                for item_id in result.item_ids
            },
        }
        total_sessions += 1

    assert quota_failures == 0
    assert duplicate_count == 0
    assert exposure_violations == 0
    assert math.fsum(adaptive_lengths) / len(adaptive_lengths) <= 0.80 * (
        math.fsum(fixed_lengths) / len(fixed_lengths)
    )
