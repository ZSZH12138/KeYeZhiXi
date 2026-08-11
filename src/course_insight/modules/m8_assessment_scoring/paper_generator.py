"""Deterministic, teacher-constraint-preserving M8 paper generation."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
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


FIXED_TIME = datetime(2026, 7, 15, 9, 0, tzinfo=timezone(timedelta(hours=8)))
_SCORE_TOLERANCE = 1e-9


class PaperGenerator:
    """Select and freeze approved items without random or model behavior."""

    def __init__(self, clock: Any = None) -> None:
        # M8-09: use injectable clock instead of hardcoded FIXED_TIME
        self._clock = clock

    def _now(self) -> datetime:
        """Return current time from injected clock, or FIXED_TIME as default."""

        if self._clock is not None and callable(getattr(self._clock, "now", None)):
            return self._clock.now()
        return FIXED_TIME

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
            "generated_at": self._now(),
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
            or task_plan.course_package_id
            != knowledge_bundle.course_package_id
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
        learner_state: LearnerStateSnapshot | None = None,
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

        # M8-02: when learner state is available, prioritise items testing weak concepts
        weak_concepts: set[str] = set()
        if learner_state is not None:
            for cs in learner_state.concept_states:
                if cs.mastery_probability < 0.5:
                    weak_concepts.add(cs.concept_id)

        # M8-03: track concept coverage for quota enforcement
        concept_quota = dict(section.concept_weights) if section.concept_weights else {}
        concept_coverage: dict[str, int] = {c: 0 for c in concept_quota}

        # Count concepts already covered by anchors
        for item in anchors:
            for cid in item.concept_ids:
                if cid in concept_coverage:
                    concept_coverage[cid] += 1

        # Build a priority key for each remaining item:
        # 1. Items covering weak concepts come first (M8-02)
        # 2. Items covering underrepresented concepts come next (M8-03)
        # 3. Objective items before subjective (existing behavior)
        def _priority_key(item: ItemCard) -> tuple[int, int, int]:
            covers_weak = any(cid in weak_concepts for cid in item.concept_ids)
            underrepresented = min(
                (concept_coverage.get(cid, 0) for cid in item.concept_ids if cid in concept_coverage),
                default=0,
            ) if concept_quota else 0
            is_objective = 0 if item.is_objective() else 1
            return (
                0 if covers_weak else 1,
                underrepresented,
                is_objective,
            )

        ordered_candidates = sorted(remaining, key=_priority_key)
        selected = [*anchors, *ordered_candidates][0 : section.item_count]
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

        # M8-03: verify concept quota is satisfied when concept_weights are defined
        if concept_quota:
            final_coverage: dict[str, int] = {c: 0 for c in concept_quota}
            for item in selected:
                for cid in item.concept_ids:
                    if cid in final_coverage:
                        final_coverage[cid] += 1
            # Every concept in the quota should have at least one item covering it
            uncovered = [
                cid for cid, count in final_coverage.items() if count == 0
            ]
            if uncovered:
                self._raise_section_unsatisfiable(
                    section,
                    "concept quota not satisfied: concepts without any items",
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
