"""Immutable paper evidence retained for scoring and model observations."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import Field

from course_insight.contracts.assessment import AssessmentPaper
from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import Rubric


class FrozenAssessmentRecord(ContractModel):
    """Paper, execution scope, and exact rubric versions frozen together."""

    paper: AssessmentPaper
    course_id: str = Field(min_length=1)
    class_id: str = Field(min_length=1)
    frozen_rubrics: list[Rubric]

    def validate_business_rules(self) -> None:
        if self.paper.immutable_checksum != self.paper.freeze():
            raise DomainError(
                code="PAPER_FREEZE_INVALID",
                module="m8",
                message="frozen assessment record contains a modified paper",
            )
        rubric_ids = [rubric.rubric_id for rubric in self.frozen_rubrics]
        required_ids = {
            item.rubric_id
            for item in self.paper.all_items()
            if item.rubric_id is not None
        }
        if (
            len(rubric_ids) != len(set(rubric_ids))
            or set(rubric_ids) != required_ids
        ):
            raise DomainError(
                code="FROZEN_RUBRIC_REFERENCE_MISMATCH",
                module="m8",
                message="frozen rubrics must exactly cover subjective paper items",
                details={"paper_id": self.paper.paper_id},
            )

    def get_rubric(self, rubric_id: str) -> Rubric:
        for rubric in self.frozen_rubrics:
            if rubric.rubric_id == rubric_id:
                return rubric.model_copy(deep=True)
        raise DomainError(
            code="FROZEN_RUBRIC_NOT_FOUND",
            module="m8",
            message="paper rubric was not retained with the frozen record",
            details={"paper_id": self.paper.paper_id, "rubric_id": rubric_id},
        )


@dataclass(frozen=True, slots=True)
class ModelObservationPolicy:
    """One explicit, versioned conversion from scores to binary outcomes."""

    version: str
    correct_threshold: float
    allowed_item_types: frozenset[str]

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("observation policy version must not be blank")
        if not 0.0 <= self.correct_threshold <= 1.0:
            raise ValueError("correct threshold must be in [0, 1]")
        if not self.allowed_item_types:
            raise ValueError("observation policy must allow at least one item type")

    def classify(
        self,
        *,
        score: float,
        max_score: float,
        item_type: str,
    ) -> str:
        if item_type not in self.allowed_item_types:
            raise DomainError(
                code="OBSERVATION_ITEM_TYPE_NOT_ALLOWED",
                module="m8",
                message="item type is excluded from the model observation policy",
                details={"item_type": item_type, "policy_version": self.version},
            )
        if max_score <= 0.0 or not 0.0 <= score <= max_score:
            raise DomainError(
                code="LEARNING_OBSERVATION_INVALID",
                module="m8",
                message="score cannot be classified as a model observation",
            )
        return (
            "correct"
            if score / max_score >= self.correct_threshold
            else "incorrect"
        )


DEFAULT_MODEL_OBSERVATION_POLICY = ModelObservationPolicy(
    version="1.0.0",
    correct_threshold=1.0,
    allowed_item_types=frozenset({"objective", "subjective"}),
)


__all__ = [
    "DEFAULT_MODEL_OBSERVATION_POLICY",
    "FrozenAssessmentRecord",
    "ModelObservationPolicy",
]
