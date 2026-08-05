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
from course_insight.modules.m3_knowledge_bundle.selection import (
    BlueprintSelectionError,
    select_blueprint_items,
)


FIXED_TIME = datetime(2026, 7, 15, 9, 0, tzinfo=timezone(timedelta(hours=8)))
_SCORE_TOLERANCE = 1e-9


class PaperGenerator:
    """Select and freeze approved items without random or model behavior."""

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

        try:
            selected_sections = select_blueprint_items(
                knowledge_bundle,
                blueprint,
            )
        except BlueprintSelectionError:
            self._raise_unsatisfiable(
                task_plan,
                "assessment blueprint constraints cannot be satisfied",
            )
        sections = [
            self._build_section(section, selected, knowledge_bundle)
            for section, selected in zip(
                blueprint.sections,
                selected_sections,
                strict=True,
            )
        ]
        paper_payload: dict[str, Any] = {
            "paper_id": f"paper_{task_plan.task_id}",
            "task_id": task_plan.task_id,
            "blueprint_id": blueprint.blueprint_id,
            "blueprint_version": blueprint.version,
            "learner_id": task_plan.learner_id,
            "sections": sections,
            "generated_at": FIXED_TIME,
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
        selected: tuple[ItemCard, ...],
        knowledge_bundle: KnowledgeBundle,
    ) -> PaperSection:
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
