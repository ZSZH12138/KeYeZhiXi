"""Deterministic 2PL-information adaptive item selection."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime

from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import ItemCard
from course_insight.contracts.learning_models import (
    AbilityEstimate,
    AdaptiveSelectionPolicy,
    AdaptiveSelectionResult,
    IRTItemParameters,
    IRTParameterSet,
    ItemExposureSnapshot,
)


@dataclass(frozen=True)
class _Candidate:
    item: ItemCard
    parameters: IRTItemParameters
    information: float


class AdaptiveItemSelector:
    """Choose approved items under content, history, and exposure constraints."""

    def select(
        self,
        *,
        policy: AdaptiveSelectionPolicy,
        ability_estimate: AbilityEstimate,
        parameter_set: IRTParameterSet,
        candidate_items: list[ItemCard],
        administered_item_ids: frozenset[str],
        exposure_snapshot: ItemExposureSnapshot,
        requested_at: datetime,
    ) -> AdaptiveSelectionResult:
        """Return the most informative deterministic feasible item sequence."""

        selected_at = requested_at.astimezone(UTC)
        self._validate_inputs(
            policy,
            ability_estimate,
            parameter_set,
            candidate_items,
            administered_item_ids,
            exposure_snapshot,
        )
        candidates = self._eligible_candidates(
            policy=policy,
            ability_estimate=ability_estimate,
            parameter_set=parameter_set,
            candidate_items=candidate_items,
            administered_item_ids=administered_item_ids,
            exposure_snapshot=exposure_snapshot,
        )
        remaining_quotas = self._remaining_quotas(
            policy,
            candidate_items,
            administered_item_ids,
        )
        selected: list[_Candidate] = []
        available = list(candidates)
        while len(selected) < policy.max_items and available:
            unmet = {
                concept_id
                for concept_id, count in remaining_quotas.items()
                if count > 0
            }
            constrained = [
                candidate
                for candidate in available
                if unmet.intersection(candidate.item.concept_ids)
            ]
            if unmet and not constrained:
                return self._failed_result(
                    policy,
                    ability_estimate,
                    parameter_set,
                    candidate_items,
                    administered_item_ids,
                    exposure_snapshot,
                    selected_at,
                )
            pool = constrained if unmet else available
            winner = min(
                pool,
                key=lambda candidate: (
                    -candidate.information,
                    candidate.item.item_id,
                    candidate.item.version,
                ),
            )
            selected = [*selected, winner]
            available = [
                candidate
                for candidate in available
                if candidate.item.item_id != winner.item.item_id
            ]
            remaining_quotas = {
                concept_id: max(
                    0,
                    count - int(concept_id in winner.item.concept_ids),
                )
                for concept_id, count in remaining_quotas.items()
            }

        if not selected or any(count > 0 for count in remaining_quotas.values()):
            return self._failed_result(
                policy,
                ability_estimate,
                parameter_set,
                candidate_items,
                administered_item_ids,
                exposure_snapshot,
                selected_at,
            )
        item_ids = [candidate.item.item_id for candidate in selected]
        selection_id = self._selection_id(
            status="selected",
            policy=policy,
            ability_estimate=ability_estimate,
            parameter_set=parameter_set,
            candidate_items=candidate_items,
            administered_item_ids=administered_item_ids,
            exposure_snapshot=exposure_snapshot,
            selected_at=selected_at,
            item_ids=item_ids,
        )
        return AdaptiveSelectionResult(
            selection_id=selection_id,
            policy_id=policy.policy_id,
            learner_id=ability_estimate.learner_id,
            item_ids=item_ids,
            ability_estimate=ability_estimate.model_copy(deep=True),
            status="selected",
            failure_code=None,
            selected_at=selected_at,
        )

    @staticmethod
    def information(
        *,
        theta: float,
        discrimination: float,
        difficulty: float,
    ) -> float:
        """Return 2PL Fisher information at one ability value."""

        argument = discrimination * (theta - difficulty)
        if argument >= 0.0:
            probability = 1.0 / (1.0 + math.exp(-argument))
        else:
            exponential = math.exp(argument)
            probability = exponential / (1.0 + exponential)
        return discrimination * discrimination * probability * (1.0 - probability)

    def _eligible_candidates(
        self,
        *,
        policy: AdaptiveSelectionPolicy,
        ability_estimate: AbilityEstimate,
        parameter_set: IRTParameterSet,
        candidate_items: list[ItemCard],
        administered_item_ids: frozenset[str],
        exposure_snapshot: ItemExposureSnapshot,
    ) -> list[_Candidate]:
        parameter_by_key = {
            (item.item_id, item.item_version): item
            for item in parameter_set.item_parameters
        }
        lower_difficulty, upper_difficulty = policy.difficulty_range
        eligible: list[_Candidate] = []
        for item in candidate_items:
            parameters = parameter_by_key.get((item.item_id, item.version))
            if (
                parameters is None
                or not item.is_approved()
                or item.item_id in administered_item_ids
                or not lower_difficulty
                <= parameters.difficulty
                <= upper_difficulty
                or self._at_exposure_cap(
                    item.item_id,
                    policy,
                    exposure_snapshot,
                )
            ):
                continue
            eligible.append(
                _Candidate(
                    item=item.model_copy(deep=True),
                    parameters=parameters.model_copy(deep=True),
                    information=self.information(
                        theta=float(ability_estimate.theta),
                        discrimination=parameters.discrimination,
                        difficulty=parameters.difficulty,
                    ),
                )
            )
        return eligible

    @staticmethod
    def _at_exposure_cap(
        item_id: str,
        policy: AdaptiveSelectionPolicy,
        exposure_snapshot: ItemExposureSnapshot,
    ) -> bool:
        if exposure_snapshot.total_sessions == 0:
            return False
        count = exposure_snapshot.item_administered_counts.get(item_id, 0)
        return (
            count / exposure_snapshot.total_sessions
            >= policy.max_item_exposure_rate
        )

    @staticmethod
    def _remaining_quotas(
        policy: AdaptiveSelectionPolicy,
        candidate_items: list[ItemCard],
        administered_item_ids: frozenset[str],
    ) -> dict[str, int]:
        item_by_id = {item.item_id: item for item in candidate_items}
        completed: dict[str, int] = {}
        for item_id in administered_item_ids:
            item = item_by_id.get(item_id)
            if item is None:
                continue
            for concept_id in item.concept_ids:
                completed = {
                    **completed,
                    concept_id: completed.get(concept_id, 0) + 1,
                }
        return {
            concept_id: max(0, quota - completed.get(concept_id, 0))
            for concept_id, quota in policy.concept_quotas.items()
        }

    @staticmethod
    def _validate_inputs(
        policy: AdaptiveSelectionPolicy,
        ability_estimate: AbilityEstimate,
        parameter_set: IRTParameterSet,
        candidate_items: list[ItemCard],
        administered_item_ids: frozenset[str],
        exposure_snapshot: ItemExposureSnapshot,
    ) -> None:
        if parameter_set.status != "approved":
            raise DomainError(
                code="IRT_PARAMETER_NOT_APPROVED",
                module="m8",
                message="adaptive selection requires approved IRT parameters",
                recoverable=True,
            )
        identities_match = (
            policy.status == "configured"
            and policy.parameter_set_id == parameter_set.parameter_set_id
            and ability_estimate.status == "estimated"
            and ability_estimate.theta is not None
            and ability_estimate.parameter_set_id == parameter_set.parameter_set_id
            and exposure_snapshot.parameter_set_id
            == parameter_set.parameter_set_id
        )
        item_ids = [item.item_id for item in candidate_items]
        if (
            not identities_match
            or len(item_ids) != len(set(item_ids))
        ):
            raise DomainError(
                code="ADAPTIVE_INPUT_MISMATCH",
                module="m8",
                message="adaptive inputs must share one approved parameter identity",
                recoverable=True,
            )

    def _failed_result(
        self,
        policy: AdaptiveSelectionPolicy,
        ability_estimate: AbilityEstimate,
        parameter_set: IRTParameterSet,
        candidate_items: list[ItemCard],
        administered_item_ids: frozenset[str],
        exposure_snapshot: ItemExposureSnapshot,
        selected_at: datetime,
    ) -> AdaptiveSelectionResult:
        return AdaptiveSelectionResult(
            selection_id=self._selection_id(
                status="failed",
                policy=policy,
                ability_estimate=ability_estimate,
                parameter_set=parameter_set,
                candidate_items=candidate_items,
                administered_item_ids=administered_item_ids,
                exposure_snapshot=exposure_snapshot,
                selected_at=selected_at,
                item_ids=[],
            ),
            policy_id=policy.policy_id,
            learner_id=ability_estimate.learner_id,
            item_ids=[],
            ability_estimate=ability_estimate.model_copy(deep=True),
            status="failed",
            failure_code="ADAPTIVE_POOL_EXHAUSTED",
            selected_at=selected_at,
        )

    @staticmethod
    def _selection_id(
        *,
        status: str,
        policy: AdaptiveSelectionPolicy,
        ability_estimate: AbilityEstimate,
        parameter_set: IRTParameterSet,
        candidate_items: list[ItemCard],
        administered_item_ids: frozenset[str],
        exposure_snapshot: ItemExposureSnapshot,
        selected_at: datetime,
        item_ids: list[str],
    ) -> str:
        payload = {
            "status": status,
            "policy_checksum": policy.content_checksum(),
            "ability_checksum": ability_estimate.content_checksum(),
            "parameter_set_id": parameter_set.parameter_set_id,
            "candidate_versions": sorted(
                (item.item_id, item.version) for item in candidate_items
            ),
            "administered_item_ids": sorted(administered_item_ids),
            "exposure_checksum": exposure_snapshot.content_checksum(),
            "selected_at": selected_at.isoformat(),
            "item_ids": item_ids,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return f"adaptive_{digest[:24]}"


__all__ = ["AdaptiveItemSelector"]
