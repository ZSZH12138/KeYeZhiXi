"""Unconfigured rubric-scoring adapter boundary for M7."""

from __future__ import annotations

from course_insight.contracts.assessment import (
    RubricScoringResult,
    RubricScoringTask,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceBundle


class PlaceholderRubricAdapter:
    """Expose the rubric adapter interface until a scorer is configured."""

    def score(
        self,
        task: RubricScoringTask,
        evidence_bundle: EvidenceBundle,
    ) -> RubricScoringResult:
        """Fail explicitly until a governed scoring implementation is supplied."""

        raise DomainError(
            code="MODEL_ADAPTER_UNCONFIGURED",
            module="m7",
            message="no rubric scoring adapter is configured",
            recoverable=True,
        )
