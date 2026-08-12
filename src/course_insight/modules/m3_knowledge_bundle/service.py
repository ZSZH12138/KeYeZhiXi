"""Report-first M3 knowledge-bundle publication and restore boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from course_insight.contracts.course import CoursePackage
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.infrastructure.json_io import dumps_json, read_json
from course_insight.modules.m3_knowledge_bundle.repository import M3Repository
from course_insight.modules.m3_knowledge_bundle.seed_snapshot import (
    M3SeedSnapshot,
    M3ValidationReport,
    capture_seed_snapshot,
    seed_snapshot_from_bytes,
    seed_snapshot_to_bytes,
    validation_report_core_bytes,
    validation_report_from_bytes,
    validation_report_to_bytes,
)
from course_insight.modules.m3_knowledge_bundle.selection import select_blueprint_items
from course_insight.modules.m3_knowledge_bundle.teacher_review import (
    TeacherReviewRecord,
    TeacherReviewWorkflow,
)
from course_insight.modules.m3_knowledge_bundle.validation import validate_seed_snapshot


_VALIDATION_ERROR_FALLBACK = "KNOWLEDGE_SEED_INVALID"
_PUBLIC_VALIDATION_PRIORITY = {
    "Q_MATRIX_CONFLICT": 60,
    "KNOWLEDGE_SEED_READ_FAILED": 50,
    "KNOWLEDGE_SEED_INVALID": 40,
    "KNOWLEDGE_COURSE_BINDING_INVALID": 30,
    "KNOWLEDGE_REFERENCE_MISSING": 20,
    "KNOWLEDGE_ENTITY_INVALID": 10,
}
_VALIDATION_ERROR_CODE_BY_ISSUE = {
    "SEED_READ_FAILED": "KNOWLEDGE_SEED_READ_FAILED",
    "SEED_TOO_LARGE": "KNOWLEDGE_SEED_INVALID",
    "SEED_SCHEMA_INVALID": "KNOWLEDGE_SEED_INVALID",
    "SEED_JSON_INVALID": "KNOWLEDGE_SEED_INVALID",
    "SCHEMA_VALIDATOR_ERROR": "KNOWLEDGE_SEED_INVALID",
    "SCHEMA_VALIDATOR_REJECTED": "KNOWLEDGE_SEED_INVALID",
    "COURSE_PACKAGE_INVALID": "KNOWLEDGE_COURSE_BINDING_INVALID",
    "COURSE_PACKAGE_NOT_READY": "KNOWLEDGE_COURSE_BINDING_INVALID",
    "COURSE_PACKAGE_CHECKSUM_INVALID": "KNOWLEDGE_COURSE_BINDING_INVALID",
    "COURSE_BINDING_MISMATCH": "KNOWLEDGE_COURSE_BINDING_INVALID",
    "BLUEPRINT_COURSE_INVALID": "KNOWLEDGE_COURSE_BINDING_INVALID",
    "CONCEPT_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "ITEM_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "RUBRIC_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "BLUEPRINT_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "PREREQUISITE_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "MISCONCEPTION_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "CONCEPT_STATUS_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "ITEM_STATUS_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "RUBRIC_STATUS_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "BLUEPRINT_STATUS_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "CONCEPT_NAME_COLLISION": "KNOWLEDGE_ENTITY_INVALID",
    "CONCEPT_ALIAS_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "CONCEPT_IDENTIFIER_DUPLICATE": "KNOWLEDGE_ENTITY_INVALID",
    "ITEM_VERSION_DUPLICATE": "KNOWLEDGE_ENTITY_INVALID",
    "RUBRIC_IDENTIFIER_DUPLICATE": "KNOWLEDGE_ENTITY_INVALID",
    "BLUEPRINT_IDENTIFIER_DUPLICATE": "KNOWLEDGE_ENTITY_INVALID",
    "MISCONCEPTION_IDENTIFIER_DUPLICATE": "KNOWLEDGE_ENTITY_INVALID",
    "ITEM_MULTIPLE_APPROVED_VERSIONS": "KNOWLEDGE_ENTITY_INVALID",
    "SUBJECTIVE_RUBRIC_REQUIRED": "KNOWLEDGE_ENTITY_INVALID",
    "RUBRIC_TOTAL_MISMATCH": "KNOWLEDGE_ENTITY_INVALID",
    "PREREQUISITE_CYCLE": "KNOWLEDGE_ENTITY_INVALID",
    "RUBRIC_CRITERIA_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "BLUEPRINT_UNSATISFIABLE": "KNOWLEDGE_ENTITY_INVALID",
    "KNOWLEDGE_BUNDLE_INVALID": "KNOWLEDGE_ENTITY_INVALID",
    "PREREQUISITE_REFERENCE_MISSING": "KNOWLEDGE_REFERENCE_MISSING",
    "MISCONCEPTION_REFERENCE_MISSING": "KNOWLEDGE_REFERENCE_MISSING",
    "ITEM_MISCONCEPTION_REFERENCE_MISSING": "KNOWLEDGE_REFERENCE_MISSING",
    "ITEM_CONCEPT_REFERENCE_MISSING": "KNOWLEDGE_REFERENCE_MISSING",
    "ITEM_RUBRIC_REFERENCE_MISSING": "KNOWLEDGE_REFERENCE_MISSING",
    "CONCEPT_EVIDENCE_INVALID": "KNOWLEDGE_REFERENCE_MISSING",
    "ITEM_EVIDENCE_INVALID": "KNOWLEDGE_REFERENCE_MISSING",
    "RUBRIC_EVIDENCE_INVALID": "KNOWLEDGE_REFERENCE_MISSING",
    "BLUEPRINT_CONCEPT_INVALID": "KNOWLEDGE_REFERENCE_MISSING",
    "ANCHOR_ITEM_VERSION_INVALID": "KNOWLEDGE_REFERENCE_MISSING",
    "Q_MATRIX_CONFLICT": "Q_MATRIX_CONFLICT",
}


class M3KnowledgeBundleService:
    """Publish and restore only complete, deterministic M3 artifacts."""

    def __init__(
        self,
        repository: M3Repository,
        schema_validator: Any,
        *,
        review_workflow: TeacherReviewWorkflow | None = None,
        require_teacher_approval: bool = False,
    ) -> None:
        self._repository = repository
        self._schema_validator = schema_validator
        self._review_workflow = review_workflow
        self._require_teacher_approval = require_teacher_approval
        self._last_course_package: CoursePackage | None = None

    def create_teacher_review_draft(
        self,
        *,
        review_id: str,
        subject_id: str,
        validation_report_ref: str,
        now: datetime,
        concept_seed_path: Path,
        item_seed_path: Path,
        rubric_seed_path: Path,
        blueprint_seed_path: Path,
        prerequisite_seed_path: Path | None = None,
        misconception_seed_path: Path | None = None,
    ) -> TeacherReviewRecord:
        """Create a review bound to the exact captured seed snapshot checksum."""

        snapshot = capture_seed_snapshot(
            concept_seed_path=concept_seed_path,
            item_seed_path=item_seed_path,
            rubric_seed_path=rubric_seed_path,
            blueprint_seed_path=blueprint_seed_path,
            prerequisite_seed_path=prerequisite_seed_path,
            misconception_seed_path=misconception_seed_path,
        )
        return self._require_review_workflow().create_draft(
            review_id=review_id,
            subject_id=subject_id,
            input_checksum=snapshot.checksum,
            validation_report_ref=validation_report_ref,
            now=now,
        )

    def submit_teacher_review(
        self,
        review_id: str,
        reviewer_pseudonym: str,
        reason: str,
        expected_version: int,
        now: datetime,
    ) -> TeacherReviewRecord:
        """Submit one teacher review through the CAS workflow."""

        return self._require_review_workflow().submit(
            review_id,
            reviewer_pseudonym,
            reason,
            expected_version,
            now,
        )

    def approve_teacher_review(
        self,
        review_id: str,
        reviewer_pseudonym: str,
        reason: str,
        expected_version: int,
        now: datetime,
    ) -> TeacherReviewRecord:
        """Approve one submitted teacher review through the CAS workflow."""

        return self._require_review_workflow().approve(
            review_id,
            reviewer_pseudonym,
            reason,
            expected_version,
            now,
        )

    def reject_teacher_review(
        self,
        review_id: str,
        reviewer_pseudonym: str,
        reason: str,
        expected_version: int,
        now: datetime,
    ) -> TeacherReviewRecord:
        """Reject one submitted teacher review through the CAS workflow."""

        return self._require_review_workflow().reject(
            review_id,
            reviewer_pseudonym,
            reason,
            expected_version,
            now,
        )

    def recall_teacher_review(
        self,
        review_id: str,
        reviewer_pseudonym: str,
        reason: str,
        expected_version: int,
        now: datetime,
    ) -> TeacherReviewRecord:
        """Recall one published review through the CAS workflow."""

        return self._require_review_workflow().recall(
            review_id,
            reviewer_pseudonym,
            reason,
            expected_version,
            now,
        )

    def build_knowledge_bundle(
        self,
        course_package: CoursePackage,
        concept_seed_path: Path,
        item_seed_path: Path,
        rubric_seed_path: Path,
        blueprint_seed_path: Path,
        prerequisite_seed_path: Path | None = None,
        misconception_seed_path: Path | None = None,
    ) -> KnowledgeBundle:
        """Capture six seed roles once, validate, then persist before returning."""
        if self._require_teacher_approval:
            raise DomainError(
                code="M3_REVIEW_REQUIRED",
                module="m3",
                message="teacher approval is required before publication",
                recoverable=True,
            )
        snapshot = capture_seed_snapshot(
            concept_seed_path=concept_seed_path,
            item_seed_path=item_seed_path,
            rubric_seed_path=rubric_seed_path,
            blueprint_seed_path=blueprint_seed_path,
            prerequisite_seed_path=prerequisite_seed_path,
            misconception_seed_path=misconception_seed_path,
        )
        return self._build_from_snapshot(course_package, snapshot)

    def build_knowledge_bundle_after_approval(
        self,
        *,
        workflow: TeacherReviewWorkflow | None = None,
        review_id: str,
        review_version: int,
        course_package: CoursePackage,
        concept_seed_path: Path,
        item_seed_path: Path,
        rubric_seed_path: Path,
        blueprint_seed_path: Path,
        prerequisite_seed_path: Path | None = None,
        misconception_seed_path: Path | None = None,
    ) -> KnowledgeBundle:
        """Publish only a teacher-approved, checksum-bound seed snapshot."""

        workflow = workflow or self._review_workflow
        if workflow is None:
            raise DomainError(
                code="M3_REVIEW_WORKFLOW_UNAVAILABLE",
                module="m3",
                message="teacher review workflow is unavailable",
                recoverable=True,
            )
        snapshot = capture_seed_snapshot(
            concept_seed_path=concept_seed_path,
            item_seed_path=item_seed_path,
            rubric_seed_path=rubric_seed_path,
            blueprint_seed_path=blueprint_seed_path,
            prerequisite_seed_path=prerequisite_seed_path,
            misconception_seed_path=misconception_seed_path,
        )
        review = workflow.require_approved(review_id, review_version)
        if review.input_checksum != snapshot.checksum:
            raise DomainError(
                code="M3_REVIEW_INPUT_MISMATCH",
                module="m3",
                message="approved review does not match current seed snapshot",
                recoverable=True,
            )
        result = workflow.publish_approved(
            review_id,
            lambda: self._build_from_snapshot(course_package, snapshot),
            expected_version=review.version,
        )
        if not isinstance(result, KnowledgeBundle):
            raise DomainError(
                code="KNOWLEDGE_ARTIFACT_INVALID",
                module="m3",
                message="approved publication did not return a knowledge bundle",
            )
        return result

    def _require_review_workflow(self) -> TeacherReviewWorkflow:
        if self._review_workflow is None:
            raise DomainError(
                code="M3_REVIEW_WORKFLOW_UNAVAILABLE",
                module="m3",
                message="teacher review workflow is unavailable",
                recoverable=True,
            )
        return self._review_workflow

    def _build_from_snapshot(
        self,
        course_package: CoursePackage,
        snapshot: M3SeedSnapshot,
    ) -> KnowledgeBundle:
        """Build from one captured immutable snapshot shared by approval and publish."""

        outcome = validate_seed_snapshot(
            course_package=course_package,
            snapshot=snapshot,
            schema_validator=self._schema_validator,
        )
        if outcome.bundle is None:
            self._save_rejected(outcome.report, snapshot)
            raise DomainError(
                code=_validation_error_code(outcome.report),
                module="m3",
                message="knowledge validation rejected the bundle",
                details={"report_id": outcome.report.report_id},
            ) from None
        bundle_bytes = _bundle_bytes(outcome.bundle)
        expected_bundle = _bundle_from_bytes(bundle_bytes)
        bundle_to_save = _bundle_from_bytes(bundle_bytes)
        report_to_save = validation_report_from_bytes(
            validation_report_to_bytes(outcome.report)
        )
        snapshot_to_save = seed_snapshot_from_bytes(seed_snapshot_to_bytes(snapshot))
        self._save_approved(bundle_to_save, report_to_save, snapshot_to_save)
        self._verify_saved_artifact(
            bundle=expected_bundle,
            report=report_to_save,
            snapshot=snapshot_to_save,
        )
        self._last_course_package = course_package.model_copy(deep=True)
        return _bundle_from_bytes(bundle_bytes)

    def restore_knowledge_bundle(
        self,
        *,
        course_package: CoursePackage,
        knowledge_bundle_id: str,
        bundle_version: str,
    ) -> KnowledgeBundle:
        """Restore exactly one artifact and prove it by deterministic rebuild."""

        try:
            artifact = self._repository.load_bundle_artifact(
                knowledge_bundle_id,
                bundle_version,
            )
        except Exception as error:
            self._raise_safe_repository_error(error)
        if artifact is None:
            raise DomainError(
                code="KNOWLEDGE_NOT_READY",
                module="m3",
                message="knowledge bundle artifact is not ready",
            )
        try:
            bundle, report, snapshot = artifact
            self._verify_artifact_bindings(
                course_package=course_package,
                knowledge_bundle_id=knowledge_bundle_id,
                bundle_version=bundle_version,
                bundle=bundle,
                report=report,
                snapshot=snapshot,
            )
            outcome = validate_seed_snapshot(
                course_package=course_package,
                snapshot=snapshot,
                schema_validator=self._schema_validator,
            )
            if outcome.bundle is None:
                raise ValueError
            for blueprint in outcome.bundle.blueprints:
                select_blueprint_items(outcome.bundle, blueprint)
            if (
                _bundle_bytes(outcome.bundle) != _bundle_bytes(bundle)
                or validation_report_core_bytes(outcome.report)
                != validation_report_core_bytes(report)
            ):
                raise ValueError
        except Exception:
            raise DomainError(
                code="KNOWLEDGE_ARTIFACT_INVALID",
                module="m3",
                message="knowledge artifact is invalid",
            ) from None
        self._last_course_package = course_package.model_copy(deep=True)
        return bundle.model_copy(deep=True)

    def _save_approved(
        self,
        bundle: KnowledgeBundle,
        report: M3ValidationReport,
        snapshot: M3SeedSnapshot,
    ) -> None:
        try:
            self._repository.save_bundle_artifact(bundle, report, snapshot)
        except Exception as error:
            self._raise_safe_repository_error(error)

    def _save_rejected(
        self,
        report: M3ValidationReport,
        snapshot: M3SeedSnapshot,
    ) -> None:
        try:
            self._repository.save_rejected_validation(report, snapshot)
        except Exception as error:
            self._raise_safe_repository_error(error)

    @staticmethod
    def _load_seed(path: Path) -> dict[str, Any]:
        """Legacy-safe one-file reader retained outside the publication flow."""

        try:
            payload = read_json(path)
        except OSError:
            raise DomainError(
                code="KNOWLEDGE_SEED_READ_FAILED",
                module="m3",
                message="knowledge seed file could not be read",
                details={},
            ) from None
        except (UnicodeError, ValueError, RecursionError):
            raise DomainError(
                code="KNOWLEDGE_SEED_INVALID",
                module="m3",
                message="knowledge seed JSON is invalid",
                details={},
            ) from None
        except Exception:
            raise DomainError(
                code="KNOWLEDGE_SEED_READ_FAILED",
                module="m3",
                message="knowledge seed file could not be read",
                details={},
            ) from None
        if not isinstance(payload, Mapping):
            raise DomainError(
                code="KNOWLEDGE_SEED_INVALID",
                module="m3",
                message="knowledge seed root must be a JSON object",
                details={},
            ) from None
        return dict(payload)

    def _verify_saved_artifact(
        self,
        *,
        bundle: KnowledgeBundle,
        report: M3ValidationReport,
        snapshot: M3SeedSnapshot,
    ) -> None:
        try:
            stored = self._repository.load_bundle_artifact(
                bundle.knowledge_bundle_id,
                bundle.bundle_version,
            )
        except Exception as error:
            self._raise_safe_repository_error(error)
        try:
            if stored is None:
                raise ValueError
            stored_bundle, stored_report, stored_snapshot = stored
            if (
                _bundle_bytes(stored_bundle) != _bundle_bytes(bundle)
                or validation_report_to_bytes(stored_report)
                != validation_report_to_bytes(report)
                or seed_snapshot_to_bytes(stored_snapshot)
                != seed_snapshot_to_bytes(snapshot)
            ):
                raise ValueError
        except Exception:
            raise DomainError(
                code="KNOWLEDGE_ARTIFACT_INVALID",
                module="m3",
                message="knowledge artifact is invalid",
            ) from None

    @staticmethod
    def _raise_safe_repository_error(error: Exception) -> None:
        code = "KNOWLEDGE_ARTIFACT_INVALID"
        if (
            isinstance(error, DomainError)
            and error.module == "m3"
            and error.details == {}
            and error.code in {"KNOWLEDGE_ARTIFACT_INVALID", "KNOWLEDGE_VERSION_CONFLICT"}
        ):
            code = error.code
        message = (
            "knowledge bundle version conflicts"
            if code == "KNOWLEDGE_VERSION_CONFLICT"
            else "knowledge artifact is invalid"
        )
        raise DomainError(code=code, module="m3", message=message) from None

    @staticmethod
    def _verify_artifact_bindings(
        *,
        course_package: CoursePackage,
        knowledge_bundle_id: str,
        bundle_version: str,
        bundle: KnowledgeBundle,
        report: M3ValidationReport,
        snapshot: M3SeedSnapshot,
    ) -> None:
        seed_snapshot_to_bytes(snapshot)
        validation_report_to_bytes(report)
        validated_bundle = KnowledgeBundle.model_validate(bundle)
        if (
            report.status != "approved"
            or report.issues
            or report.seed_snapshot_checksum != snapshot.checksum
            or report.knowledge_bundle_id != knowledge_bundle_id
            or report.bundle_version != bundle_version
            or validated_bundle.knowledge_bundle_id != knowledge_bundle_id
            or validated_bundle.bundle_version != bundle_version
            or report.bundle_checksum != validated_bundle.content_checksum()
            or validated_bundle.course_package_id != course_package.course_package_id
            or report.course_package_id != course_package.course_package_id
            or validated_bundle.course_id != course_package.course_id
            or validated_bundle.course_package_checksum != course_package.checksum
            or report.course_package_checksum != course_package.checksum
        ):
            raise ValueError


def _bundle_bytes(bundle: KnowledgeBundle) -> bytes:
    validated = KnowledgeBundle.model_validate(bundle)
    return dumps_json(validated.model_dump(mode="json")).encode("utf-8")


def _bundle_from_bytes(payload: bytes) -> KnowledgeBundle:
    value = json.loads(payload)
    if dumps_json(value).encode("utf-8") != payload:
        raise ValueError
    return KnowledgeBundle.model_validate(value)


def _validation_error_code(report: M3ValidationReport) -> str:
    """Project report issues to one deterministic, path-free public code."""

    selected_code = _VALIDATION_ERROR_FALLBACK
    selected_priority = -1
    for issue in report.issues:
        public_code = _VALIDATION_ERROR_CODE_BY_ISSUE.get(issue.code)
        if public_code is None:
            continue
        priority = _PUBLIC_VALIDATION_PRIORITY[public_code]
        if priority > selected_priority:
            selected_code = public_code
            selected_priority = priority
    return selected_code
