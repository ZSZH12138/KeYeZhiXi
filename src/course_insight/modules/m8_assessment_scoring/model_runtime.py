"""Persisted IRT approval and learner-ability service behavior."""

from __future__ import annotations

import hashlib
from typing import Any

from course_insight.contracts.errors import DomainError
from course_insight.contracts.learning_models import (
    AbilityEstimate,
    CalibrationReviewDecision,
    CalibrationRunResult,
    IRTParameterSet,
    LearningObservation,
    ModelQualityReport,
)


class M8ModelRuntimeMixin:
    """M8 model lifecycle methods shared by the assessment service."""

    _repository: Any
    _irt_calibrator: Any

    def _persist_calibration_if_supported(
        self,
        result: CalibrationRunResult,
        observations: list[LearningObservation],
    ) -> CalibrationRunResult:
        """Persist production runs while retaining lightweight test adapters."""

        inserter = getattr(self._repository, "insert_or_get_calibration_run", None)
        if not callable(inserter) or not observations:
            return result
        course_ids = {observation.course_id for observation in observations}
        if len(course_ids) != 1:
            raise DomainError(
                code="CALIBRATION_SCOPE_MISMATCH",
                module="m8",
                message="calibration observations must belong to one course",
                recoverable=True,
            )
        return self.store_calibration_result(
            result,
            course_id=next(iter(course_ids)),
        )

    def store_calibration_result(
        self,
        result: CalibrationRunResult,
        *,
        course_id: str,
    ) -> CalibrationRunResult:
        """Persist one calibration run and its parameter snapshot atomically."""

        inserter = getattr(self._repository, "insert_or_get_calibration_run", None)
        if not callable(inserter):
            raise RuntimeError("M8 repository does not support calibration persistence")
        stored = inserter(result.model_copy(deep=True), course_id=course_id)
        if stored != result:
            raise RuntimeError("M8 IRT calibration-run conflict")
        return stored.model_copy(deep=True)

    def apply_calibration_review(
        self,
        run_id: str,
        quality_report: ModelQualityReport,
        decision: CalibrationReviewDecision,
    ) -> IRTParameterSet:
        """Apply one immutable teacher decision to a persisted shadow run."""

        run = self._repository.get_calibration_run(run_id)
        if run is None:
            raise DomainError(
                code="CALIBRATION_RUN_NOT_FOUND",
                module="m8",
                message="calibration run does not exist",
                details={"run_id": run_id},
                recoverable=True,
            )
        course_id = self._repository.get_calibration_run_course_id(run_id)
        if course_id is None:
            raise RuntimeError("M8 calibration run is missing course scope")
        self._validate_review_inputs(run, quality_report, decision)
        existing = self._repository.get_calibration_review(run_id)
        if existing is not None and existing != decision:
            raise DomainError(
                code="CALIBRATION_REVIEW_CONFLICT",
                module="m8",
                message="calibration review conflict for this run",
                details={"run_id": run_id},
                recoverable=True,
            )
        reviewed = self._reviewed_parameter_set(run, quality_report, decision)
        try:
            stored_decision, stored_parameters = (
                self._repository.insert_or_get_calibration_review(
                    decision.model_copy(deep=True),
                    reviewed,
                    course_id=course_id,
                )
            )
        except RuntimeError as error:
            if "review conflict" in str(error):
                raise DomainError(
                    code="CALIBRATION_REVIEW_CONFLICT",
                    module="m8",
                    message="calibration review conflict for this run",
                    details={"run_id": run_id},
                    recoverable=True,
                ) from error
            raise
        if stored_decision != decision or stored_parameters != reviewed:
            raise RuntimeError("M8 calibration review persistence conflict")
        return stored_parameters.model_copy(deep=True)

    def activate_parameter_set(self, parameter_set_id: str) -> IRTParameterSet:
        """Return one approved parameter set or reject activation."""

        parameter_set = self.get_parameter_set(parameter_set_id)
        if parameter_set.status != "approved":
            raise DomainError(
                code="IRT_PARAMETER_NOT_APPROVED",
                module="m8",
                message="IRT parameter set must be approved before activation",
                details={"parameter_set_id": parameter_set_id},
                recoverable=True,
            )
        return parameter_set

    def get_active_parameter_set(self, parameter_set_id: str) -> IRTParameterSet:
        """Recover one approved parameter set after restart."""

        return self.activate_parameter_set(parameter_set_id)

    def get_parameter_set(self, parameter_set_id: str) -> IRTParameterSet:
        """Load one immutable IRT parameter set."""

        parameter_set = self._repository.get_parameter_set(parameter_set_id)
        if parameter_set is None:
            raise DomainError(
                code="IRT_PARAMETER_SET_NOT_FOUND",
                module="m8",
                message="IRT parameter set does not exist",
                details={"parameter_set_id": parameter_set_id},
                recoverable=True,
            )
        return parameter_set.model_copy(deep=True)

    def estimate_ability(
        self,
        parameter_set_id: str,
        responses: list[LearningObservation],
        *,
        course_id: str,
    ) -> AbilityEstimate:
        """Estimate and persist finite EAP ability from approved 2PL parameters."""

        parameter_set = self.activate_parameter_set(parameter_set_id)
        stored_course_id = self._repository.get_parameter_set_course_id(
            parameter_set_id
        )
        if stored_course_id != course_id:
            raise DomainError(
                code="IRT_PARAMETER_SCOPE_MISMATCH",
                module="m8",
                message="IRT parameter set does not belong to the requested course",
                details={
                    "parameter_set_id": parameter_set_id,
                    "course_id": course_id,
                },
                recoverable=True,
            )
        try:
            estimate = self._irt_calibrator.estimate_ability(
                parameter_set,
                list(responses),
            )
        except ValueError as error:
            raise DomainError(
                code="ABILITY_ESTIMATION_INVALID",
                module="m8",
                message=str(error),
                recoverable=True,
            ) from error
        stored = self._repository.insert_or_get_ability_estimate(
            estimate,
            course_id=course_id,
        )
        if stored != estimate:
            raise RuntimeError("M8 ability-estimate persistence conflict")
        return stored.model_copy(deep=True)

    def get_ability_estimate(self, estimate_id: str) -> AbilityEstimate:
        """Recover one immutable learner ability estimate."""

        estimate = self._repository.get_ability_estimate(estimate_id)
        if estimate is None:
            raise DomainError(
                code="ABILITY_ESTIMATE_NOT_FOUND",
                module="m8",
                message="ability estimate does not exist",
                details={"estimate_id": estimate_id},
                recoverable=True,
            )
        return estimate.model_copy(deep=True)

    @staticmethod
    def _validate_review_inputs(
        run: CalibrationRunResult,
        quality_report: ModelQualityReport,
        decision: CalibrationReviewDecision,
    ) -> None:
        parameter_set = run.parameter_set
        if (
            run.status != "shadow"
            or not run.converged
            or parameter_set.status != "shadow"
        ):
            raise DomainError(
                code="CALIBRATION_RUN_NOT_REVIEWABLE",
                module="m8",
                message="only converged shadow calibration runs can be reviewed",
                recoverable=True,
            )
        if (
            decision.calibration_run_id != run.run_id
            or decision.target_parameter_version != parameter_set.version
            or quality_report.subject_ref != run.run_id
        ):
            raise DomainError(
                code="CALIBRATION_REVIEW_IDENTITY_MISMATCH",
                module="m8",
                message="review evidence does not match the calibration run",
                recoverable=True,
            )
        if decision.decision == "approve" and quality_report.status != "ready":
            raise DomainError(
                code="QUALITY_REPORT_NOT_READY",
                module="m8",
                message="QUALITY_REPORT_NOT_READY: approval requires ready evidence",
                recoverable=True,
            )

    @staticmethod
    def _reviewed_parameter_set(
        run: CalibrationRunResult,
        quality_report: ModelQualityReport,
        decision: CalibrationReviewDecision,
    ) -> IRTParameterSet:
        original = run.parameter_set
        if decision.decision == "defer":
            return original.model_copy(deep=True)
        status = "approved" if decision.decision == "approve" else "rejected"
        digest = hashlib.sha256(
            (
                f"{original.content_checksum()}:"
                f"{quality_report.content_checksum()}:"
                f"{decision.content_checksum()}:{status}"
            ).encode("utf-8")
        ).hexdigest()
        return original.model_copy(
            update={
                "parameter_set_id": f"irt_{status}_{digest[:24]}",
                "version": f"{original.version}-{status}-{digest[:12]}",
                "status": status,
                "created_at": decision.reviewed_at,
            },
            deep=True,
        )


__all__ = ["M8ModelRuntimeMixin"]
