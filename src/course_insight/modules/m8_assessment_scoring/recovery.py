"""Exact historical-result reads for restart-safe application workflows."""

from __future__ import annotations

from course_insight.contracts.assessment import ScoringResultBundle


class M8HistoricalRecoveryMixin:
    """Expose isolated historical bundles without widening public contracts."""

    _repository: object

    def get_scoring_result_by_checksum(
        self,
        attempt_id: str,
        checksum: str,
    ) -> ScoringResultBundle | None:
        bundle = self._repository.get_scoring_result_by_checksum(
            attempt_id,
            checksum,
        )
        return None if bundle is None else bundle.model_copy(deep=True)

    def get_scoring_result_for_audit(
        self,
        attempt_id: str,
        audit_id: str,
        audit_version: int,
    ) -> ScoringResultBundle | None:
        bundle = self._repository.get_scoring_result_for_audit(
            attempt_id,
            audit_id,
            audit_version,
        )
        return None if bundle is None else bundle.model_copy(deep=True)
