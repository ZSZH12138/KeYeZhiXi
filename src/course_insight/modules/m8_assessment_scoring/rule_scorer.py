"""Deterministic objective-answer matching and audit scoring."""

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
    """Score objective values against teacher-listed exact answer strings."""

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
        if (
            not item.is_objective()
            or item_instance.is_subjective()
            or item.item_id != item_instance.item_id
            or item.version != item_instance.item_version
        ):
            raise DomainError(
                code="ANSWER_FORMAT_INVALID",
                module="m8",
                message="rule scoring requires one aligned objective item",
            )
        expected_values = self._accepted_answers(item)
        frozen = item_instance.parameters.get("_frozen_answers")
        if type(frozen) is list and frozen:
            expected_values = list(frozen)
        supplied = self.normalize(raw_answer)
        expected = {self.normalize(value) for value in expected_values}
        correct = supplied in expected
        score = item_instance.max_score if correct else 0.0
        evidence = self.display(raw_answer)
        criterion = CriterionScore(
            criterion_id=f"objective_{item.item_id}",
            score=score,
            student_evidence=evidence,
            course_evidence_id=(
                item_instance.source_evidence_ids[0]
                if item_instance.source_evidence_ids
                else None
            ),
            reason=(
                "The normalized answer matches the approved answer key."
                if correct
                else "The normalized answer does not match the approved answer key."
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
    def _accepted_answers(item: ItemCard) -> list[Any]:
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
