"""Deterministic, teacher-constraint-preserving M8 paper generation."""

from __future__ import annotations

import math
from typing import Any

from course_insight.contracts.assessment import (
    AssessmentPaper,
    ItemInstance,
    PaperSection,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import (
    BlueprintSection,
    ItemCard,
    KnowledgeBundle,
    ParameterRule,
)
from course_insight.contracts.state import DiagnosisResult, LearnerStateSnapshot
from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m8_assessment_scoring.clock import Clock, SystemUTCClock


_SCORE_TOLERANCE = 1e-9


def allocate_concept_targets(
    concept_weights: dict[str, float],
    item_count: int,
) -> dict[str, int]:
    """Allocate integer quotas with the stable largest-remainder method."""

    if item_count < 0 or not concept_weights:
        if item_count < 0:
            raise ValueError("item count must not be negative")
        return {}
    total = math.fsum(concept_weights.values())
    if any(
        not math.isfinite(weight) or weight < 0.0
        for weight in concept_weights.values()
    ) or not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("concept weights must be finite and sum to one")
    raw = {
        concept_id: weight * item_count
        for concept_id, weight in concept_weights.items()
    }
    targets = {
        concept_id: math.floor(value)
        for concept_id, value in raw.items()
    }
    remaining = item_count - sum(targets.values())
    order = sorted(
        raw,
        key=lambda concept_id: (
            -(raw[concept_id] - targets[concept_id]),
            concept_id,
        ),
    )
    for concept_id in order[:remaining]:
        targets = {**targets, concept_id: targets[concept_id] + 1}
    return targets


class PaperGenerator:
    """Select and freeze approved items without random or model behavior."""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = SystemUTCClock() if clock is None else clock

    def generate(
        self,
        task_plan: TaskPlan,
        knowledge_bundle: KnowledgeBundle,
        learner_state_snapshot: LearnerStateSnapshot | None,
        diagnosis_result: DiagnosisResult | None,
    ) -> AssessmentPaper:
        self._validate_references(
            task_plan,
            knowledge_bundle,
            learner_state_snapshot,
            diagnosis_result,
        )
        if task_plan.blueprint_id is None:
            self._raise_unsatisfiable(task_plan, "assessment blueprint is missing")
        blueprint = knowledge_bundle.get_blueprint(task_plan.blueprint_id)
        if (
            blueprint.course_id != task_plan.course_id
            or " ".join(blueprint.status.split()).casefold()
            != "teacher_approved"
        ):
            self._raise_unsatisfiable(
                task_plan,
                "assessment blueprint is not approved for the task course",
            )

        used_item_ids: set[str] = set()
        sections = [
            self._build_section(
                section,
                knowledge_bundle,
                used_item_ids,
                learner_state_snapshot,
                diagnosis_result,
            )
            for section in blueprint.sections
        ]
        paper_payload: dict[str, Any] = {
            "paper_id": f"paper_{task_plan.task_id}",
            "task_id": task_plan.task_id,
            "blueprint_id": blueprint.blueprint_id,
            "blueprint_version": blueprint.version,
            "learner_id": task_plan.learner_id,
            "sections": sections,
            "generated_at": self._clock.now(),
            "immutable_checksum": "pending",
        }
        paper = AssessmentPaper(**paper_payload)
        frozen = AssessmentPaper(
            **{**paper_payload, "immutable_checksum": paper.freeze()}
        )
        if not math.isclose(
            frozen.total_score(),
            blueprint.total_score,
            rel_tol=0.0,
            abs_tol=_SCORE_TOLERANCE,
        ):
            self._raise_unsatisfiable(
                task_plan,
                "paper maximum does not match the blueprint total",
            )
        return frozen

    @staticmethod
    def _validate_references(
        task_plan: TaskPlan,
        knowledge_bundle: KnowledgeBundle,
        learner_state_snapshot: LearnerStateSnapshot | None,
        diagnosis_result: DiagnosisResult | None,
    ) -> None:
        if (
            not task_plan.requires_assessment()
            or task_plan.knowledge_bundle_id
            != knowledge_bundle.knowledge_bundle_id
            or task_plan.course_id != knowledge_bundle.course_id
            or "M8" not in task_plan.workflow
        ):
            PaperGenerator._raise_unsatisfiable(
                task_plan,
                "task plan and knowledge bundle are not assessment-aligned",
            )
        if learner_state_snapshot is not None and (
            learner_state_snapshot.course_id != task_plan.course_id
            or learner_state_snapshot.class_id != task_plan.class_id
            or learner_state_snapshot.learner_id != task_plan.learner_id
        ):
            PaperGenerator._raise_unsatisfiable(
                task_plan,
                "learner state is not aligned with the task",
            )
        if diagnosis_result is not None and (
            diagnosis_result.learner_id != task_plan.learner_id
        ):
            PaperGenerator._raise_unsatisfiable(
                task_plan,
                "diagnosis is not aligned with the task learner",
            )

    def _build_section(
        self,
        section: BlueprintSection,
        knowledge_bundle: KnowledgeBundle,
        used_item_ids: set[str],
        learner_state_snapshot: LearnerStateSnapshot | None,
        diagnosis_result: DiagnosisResult | None,
    ) -> PaperSection:
        approved = [
            item
            for item in knowledge_bundle.approved_items()
            if item.item_id not in used_item_ids and section.accepts(item)
        ]
        by_id = {item.item_id: item for item in approved}
        if any(anchor_id not in by_id for anchor_id in section.anchor_item_ids):
            self._raise_section_unsatisfiable(section, "anchor item is unavailable")

        anchors = [by_id[anchor_id] for anchor_id in section.anchor_item_ids]
        if len(anchors) > section.item_count:
            self._raise_section_unsatisfiable(
                section,
                "anchor count exceeds the section item count",
            )
        anchored_ids = {item.item_id for item in anchors}
        remaining = [item for item in approved if item.item_id not in anchored_ids]
        ordered_candidates = [
            *self._personalize_candidates(
                [item for item in remaining if item.is_objective()],
                learner_state_snapshot,
                diagnosis_result,
            ),
            *self._personalize_candidates(
                [item for item in remaining if not item.is_objective()],
                learner_state_snapshot,
                diagnosis_result,
            ),
        ]
        if (
            not section.concept_weights
            and len(anchors) + len(ordered_candidates) < section.item_count
        ):
            self._raise_section_unsatisfiable(
                section,
                "not enough approved items",
            )
        selected = (
            self._select_weighted_items(
                section,
                knowledge_bundle,
                anchors,
                ordered_candidates,
            )
            if section.concept_weights
            else self._select_unweighted_items(
                section,
                knowledge_bundle,
                anchors,
                ordered_candidates,
            )
        )
        if len(selected) != section.item_count:
            self._raise_section_unsatisfiable(section, "not enough approved items")

        item_total = math.fsum(
            item.max_score(knowledge_bundle) for item in selected
        )
        if not math.isclose(
            item_total,
            section.score,
            rel_tol=0.0,
            abs_tol=_SCORE_TOLERANCE,
        ):
            self._raise_section_unsatisfiable(
                section,
                "selected item maxima do not match the section score",
            )

        used_item_ids.update(item.item_id for item in selected)
        instances = [
            self._freeze_item(item, knowledge_bundle) for item in selected
        ]
        return PaperSection(
            section_id=section.section_id,
            name=section.name,
            items=instances,
            score=section.score,
        )

    @staticmethod
    def _personalize_candidates(
        candidates: list[ItemCard],
        learner_state_snapshot: LearnerStateSnapshot | None,
        diagnosis_result: DiagnosisResult | None,
    ) -> list[ItemCard]:
        """Rank interchangeable items by current diagnosis and weakest mastery."""

        if learner_state_snapshot is None and diagnosis_result is None:
            return list(candidates)
        priority_ranks = {
            concept_id: rank
            for rank, concept_id in enumerate(
                diagnosis_result.priority_concept_ids
                if diagnosis_result is not None
                else []
            )
        }
        mastery = {
            state.concept_id: state.mastery_probability
            for state in (
                learner_state_snapshot.concept_states
                if learner_state_snapshot is not None
                else []
            )
        }
        if not priority_ranks and not mastery:
            return list(candidates)
        no_priority_rank = len(priority_ranks)

        def personalization_key(item: ItemCard) -> tuple[int, float]:
            item_priority_ranks = [
                priority_ranks[concept_id]
                for concept_id in item.concept_ids
                if concept_id in priority_ranks
            ]
            item_mastery = [
                mastery[concept_id]
                for concept_id in item.concept_ids
                if concept_id in mastery
            ]
            return (
                min(item_priority_ranks, default=no_priority_rank),
                min(item_mastery, default=1.0),
            )

        return sorted(candidates, key=personalization_key)

    def _select_unweighted_items(
        self,
        section: BlueprintSection,
        knowledge_bundle: KnowledgeBundle,
        anchors: list[ItemCard],
        candidates: list[ItemCard],
    ) -> list[ItemCard]:
        ordered = [*anchors, *candidates]
        scores = [item.max_score(knowledge_bundle) for item in ordered]
        failed: set[tuple[int, int, float]] = set()

        def search(
            index: int,
            slots: int,
            score: float,
        ) -> list[ItemCard] | None:
            key = (index, slots, round(score, 9))
            if key in failed:
                return None
            if slots == 0:
                if math.isclose(
                    score,
                    section.score,
                    rel_tol=0.0,
                    abs_tol=_SCORE_TOLERANCE,
                ):
                    return []
                failed.add(key)
                return None
            if (
                index >= len(ordered)
                or len(ordered) - index < slots
                or score > section.score + _SCORE_TOLERANCE
            ):
                failed.add(key)
                return None

            item = ordered[index]
            tail = search(
                index + 1,
                slots - 1,
                score + scores[index],
            )
            if tail is not None:
                return [item, *tail]
            if index >= len(anchors):
                tail = search(index + 1, slots, score)
                if tail is not None:
                    return tail
            failed.add(key)
            return None

        selected = search(0, section.item_count, 0.0)
        if selected is None:
            self._raise_section_unsatisfiable(
                section,
                "no item combination has maxima matching the section score",
            )
        return selected

    def _select_weighted_items(
        self,
        section: BlueprintSection,
        knowledge_bundle: KnowledgeBundle,
        anchors: list[ItemCard],
        candidates: list[ItemCard],
    ) -> list[ItemCard]:
        concept_ids = tuple(sorted(section.concept_weights))
        initial_targets = allocate_concept_targets(
            section.concept_weights,
            section.item_count,
        )
        ordered = [*anchors, *candidates]
        scores = [item.max_score(knowledge_bundle) for item in ordered]
        failed: set[tuple[int, int, float, tuple[int, ...]]] = set()

        def search(
            index: int,
            slots: int,
            score: float,
            remaining: dict[str, int],
        ) -> list[ItemCard] | None:
            key = (
                index,
                slots,
                round(score, 9),
                tuple(remaining[concept_id] for concept_id in concept_ids),
            )
            if key in failed:
                return None
            if slots == 0:
                if all(value == 0 for value in remaining.values()) and math.isclose(
                    score,
                    section.score,
                    rel_tol=0.0,
                    abs_tol=_SCORE_TOLERANCE,
                ):
                    return []
                failed.add(key)
                return None
            if (
                index >= len(ordered)
                or len(ordered) - index < slots
                or score > section.score + _SCORE_TOLERANCE
            ):
                failed.add(key)
                return None

            item = ordered[index]
            options = sorted(
                (
                    concept_id
                    for concept_id in item.concept_ids
                    if remaining.get(concept_id, 0) > 0
                ),
                key=lambda concept_id: (-remaining[concept_id], concept_id),
            )
            for concept_id in options:
                next_remaining = {
                    **remaining,
                    concept_id: remaining[concept_id] - 1,
                }
                tail = search(
                    index + 1,
                    slots - 1,
                    score + scores[index],
                    next_remaining,
                )
                if tail is not None:
                    return [item, *tail]
            if index >= len(anchors):
                tail = search(index + 1, slots, score, remaining)
                if tail is not None:
                    return tail
            failed.add(key)
            return None

        selected = search(0, section.item_count, 0.0, initial_targets)
        if selected is None:
            self._raise_section_unsatisfiable(
                section,
                "no item combination satisfies the concept quotas",
            )
        return selected

    @staticmethod
    def _freeze_item(
        item: ItemCard,
        knowledge_bundle: KnowledgeBundle,
    ) -> ItemInstance:
        prefix, separator, suffix = item.item_id.rpartition("_")
        instance_id = (
            f"{prefix}_instance_{suffix}"
            if separator and suffix.isdigit()
            else f"{item.item_id}_instance"
        )
        parameters: dict[str, Any] = {}
        for rule in item.parameter_rules:
            value = PaperGenerator._fixed_parameter(rule)
            if not rule.accepts(value):
                raise DomainError(
                    code="BLUEPRINT_UNSATISFIABLE",
                    module="m8",
                    message="fixed parameter does not satisfy its approved rule",
                    details={"item_id": item.item_id, "parameter": rule.name},
                    recoverable=True,
                )
            parameters = {**parameters, rule.name: value}
        return ItemInstance(
            item_instance_id=instance_id,
            item_id=item.item_id,
            item_version=item.version,
            stem=item.stem,
            parameters=parameters,
            concept_ids=list(item.concept_ids),
            rubric_id=item.rubric_id,
            max_score=item.max_score(knowledge_bundle),
            source_evidence_ids=list(item.source_evidence_ids),
        )

    @staticmethod
    def _fixed_parameter(rule: ParameterRule) -> Any:
        if rule.choices:
            return rule.choices[0]
        normalized_type = " ".join(rule.value_type.split()).casefold()
        if normalized_type in {"integer", "int"}:
            return int(rule.minimum) if rule.minimum is not None else 0
        if normalized_type in {"number", "float"}:
            return float(rule.minimum) if rule.minimum is not None else 0.0
        if normalized_type in {"boolean", "bool"}:
            return False
        return "fixed"

    @staticmethod
    def _raise_section_unsatisfiable(
        section: BlueprintSection,
        reason: str,
    ) -> None:
        raise DomainError(
            code="BLUEPRINT_UNSATISFIABLE",
            module="m8",
            message=reason,
            details={"section_id": section.section_id},
            recoverable=True,
        )

    @staticmethod
    def _raise_unsatisfiable(task_plan: TaskPlan, reason: str) -> None:
        raise DomainError(
            code="BLUEPRINT_UNSATISFIABLE",
            module="m8",
            message=reason,
            details={"task_id": task_plan.task_id},
            recoverable=True,
        )


__all__ = ["PaperGenerator", "allocate_concept_targets"]
