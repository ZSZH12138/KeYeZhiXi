"""Deterministic teacher-answer matching and audit scoring."""

from __future__ import annotations

import math
from typing import Any

from course_insight.contracts.assessment import (
    CriterionScore,
    ItemInstance,
    ScoreAuditRecord,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import ItemCard
from course_insight.modules.m8_assessment_scoring.clock import Clock, SystemUTCClock


class RuleScorer:
    """Score supported answers against teacher-listed exact strings."""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = SystemUTCClock() if clock is None else clock

    def score(
        self,
        *,
        attempt_id: str,
        item_instance: ItemInstance,
        item: ItemCard,
        raw_answer: Any,
    ) -> ScoreAuditRecord:
        aligned = (
            item.item_id == item_instance.item_id
            and item.version == item_instance.item_version
        )
        exact_subjective = (
            item_instance.is_subjective()
            and not item.is_objective()
            and self.has_teacher_answers(item)
        )
        if not aligned or not (
            (item.is_objective() and not item_instance.is_subjective())
            or exact_subjective
        ):
            raise DomainError(
                code="ANSWER_FORMAT_INVALID",
                module="m8",
                message=(
                    "rule scoring requires one aligned objective item "
                    "or exact-answer subjective item"
                ),
            )
        expected_values = self.accepted_answers(item)
        frozen = item_instance.parameters.get("_frozen_answers")
        if type(frozen) is list and frozen:
            expected_values = list(frozen)
        supplied = self.normalize(raw_answer)
        expected = {self.normalize(value) for value in expected_values}
        correct = supplied in expected
        score = item_instance.max_score if correct else 0.0
        evidence = self.display(raw_answer)
        criterion = CriterionScore(
            criterion_id=(
                f"exact_{item.item_id}"
                if item_instance.is_subjective()
                else f"objective_{item.item_id}"
            ),
            score=score,
            student_evidence=evidence,
            course_evidence_id=(
                item_instance.source_evidence_ids[0]
                if item_instance.source_evidence_ids
                else None
            ),
            reason=(
                (
                    "The submitted answer exactly matches the approved answer key."
                    if item_instance.is_subjective()
                    else "The normalized answer matches the approved answer key."
                )
                if correct
                else (
                    "The submitted answer does not exactly match the approved answer key."
                    if item_instance.is_subjective()
                    else "The normalized answer does not match the approved answer key."
                )
            ),
        )
        return ScoreAuditRecord(
            audit_id=f"audit_{attempt_id}_{item_instance.item_instance_id}",
            audit_version=1,
            attempt_id=attempt_id,
            item_instance_id=item_instance.item_instance_id,
            criterion_scores=[criterion],
            total_score=score,
            max_score=item_instance.max_score,
            confidence=1.0,
            scoring_method="rule",
            review_status="not_required",
            review_reason=[],
            created_at=self._clock.now(),
        )

    @staticmethod
    def has_teacher_answers(item: ItemCard) -> bool:
        """Return whether an item has at least one teacher-listed answer."""

        answers = item.answer_key.get("answers")
        return type(answers) is list and bool(answers)

    @staticmethod
    def accepted_answers(item: ItemCard) -> list[Any]:
        """Return a defensive copy of the teacher-approved answers."""

        answers = item.answer_key.get("answers")
        if answers is not None:
            if type(answers) is not list or not answers:
                raise DomainError(
                    code="ANSWER_FORMAT_INVALID",
                    module="m8",
                    message="objective answer key is missing",
                    details={"item_id": item.item_id},
                )
            return list(answers)
        if "answer" not in item.answer_key:
            raise DomainError(
                code="ANSWER_FORMAT_INVALID",
                module="m8",
                message="objective answer key is missing",
                details={"item_id": item.item_id},
            )
        return [item.answer_key["answer"]]

    @staticmethod
    def normalize(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if type(value) is bool:
            return "true" if value else "false"
        if type(value) is int:
            return str(value)
        if type(value) is float and math.isfinite(value):
            return format(value, ".15g")
        raise DomainError(
            code="ANSWER_FORMAT_INVALID",
            module="m8",
            message="objective answer must be a finite JSON scalar",
        )

    @staticmethod
    def display(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if type(value) is bool:
            return "true" if value else "false"
        if type(value) in {int, float}:
            return str(value)
        return ""
