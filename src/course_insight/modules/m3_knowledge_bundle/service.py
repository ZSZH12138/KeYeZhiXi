"""Report-first M3 knowledge-bundle publication and restore boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping
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
from course_insight.modules.m3_knowledge_bundle.validation import validate_seed_snapshot


class M3KnowledgeBundleService:
    """Publish and restore only complete, deterministic M3 artifacts."""

    def __init__(self, repository: M3Repository, schema_validator: Any) -> None:
        self._repository = repository
        self._schema_validator = schema_validator
        self._last_course_package: CoursePackage | None = None

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

        snapshot = capture_seed_snapshot(
            concept_seed_path=concept_seed_path,
            item_seed_path=item_seed_path,
            rubric_seed_path=rubric_seed_path,
            blueprint_seed_path=blueprint_seed_path,
            prerequisite_seed_path=prerequisite_seed_path,
            misconception_seed_path=misconception_seed_path,
        )
        outcome = validate_seed_snapshot(
            course_package=course_package,
            snapshot=snapshot,
            schema_validator=self._schema_validator,
        )
        if outcome.bundle is None:
            self._save_rejected(outcome.report, snapshot)
            raise DomainError(
                code="Q_MATRIX_CONFLICT",
                module="m3",
                message="knowledge validation rejected the bundle",
                details={"report_id": outcome.report.report_id},
            )
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
        except (OSError, UnicodeError, ValueError):
            raise DomainError(
                code="Q_MATRIX_CONFLICT",
                module="m3",
                message="knowledge seed file could not be read",
                details={"input": "knowledge_seed"},
            ) from None
        if not isinstance(payload, Mapping):
            raise DomainError(
                code="Q_MATRIX_CONFLICT",
                module="m3",
                message="knowledge seed root must be a JSON object",
                details={"input": "knowledge_seed"},
            )
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
