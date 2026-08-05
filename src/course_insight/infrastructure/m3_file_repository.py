"""Immutable, complete-file persistence for M3 knowledge publications."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.runtime_artifacts import (
    ArtifactStoreError,
    ImmutableArtifactStore,
)
from course_insight.modules.m3_knowledge_bundle.seed_snapshot import (
    M3SeedSnapshot,
    M3ValidationReport,
    seed_snapshot_from_bytes,
    seed_snapshot_to_bytes,
    validation_report_from_bytes,
    validation_report_to_bytes,
)


_MODULE = "m3"
_BUNDLE_TYPE = "knowledge_bundle"
_REJECTION_TYPE = "knowledge_validation"
_BUNDLE_FORMAT = "m3_knowledge_bundle/v1"
_REJECTION_FORMAT = "m3_knowledge_validation/v1"
_BUNDLE_PAYLOADS = frozenset({"bundle.json", "seed_snapshot.json", "validation_report.json"})
_REJECTION_PAYLOADS = frozenset({"seed_snapshot.json", "validation_report.json"})


def _invalid() -> DomainError:
    return DomainError(
        code="KNOWLEDGE_ARTIFACT_INVALID", module="m3",
        message="knowledge artifact is invalid",
    )


def _version_conflict() -> DomainError:
    return DomainError(
        code="KNOWLEDGE_VERSION_CONFLICT", module="m3",
        message="knowledge bundle version conflicts",
    )


class FileM3Repository:
    """Store only fully self-verifying M3 bundle and rejection artifacts."""

    def __init__(self, runtime_dir: Path) -> None:
        # A rejected artifact nests a content-addressed report ID below its
        # content-addressed immutable checksum.  Use Windows' long-path form
        # internally so that the approved logical identity remains literal.
        root = Path(runtime_dir).resolve()
        if os.name == "nt" and not str(root).startswith("\\\\?\\"):
            root = Path("\\\\?\\" + str(root))
        self._store = ImmutableArtifactStore(root)

    def save_knowledge_bundle(self, bundle: KnowledgeBundle) -> None:
        """Reject legacy bundle-only writes because they cannot be restored safely."""

        del bundle
        raise _invalid()

    def get_knowledge_bundle(
        self, knowledge_bundle_id: str, bundle_version: str
    ) -> KnowledgeBundle | None:
        """Reject legacy bundle-only reads because they bypass validation evidence."""

        del knowledge_bundle_id, bundle_version
        raise _invalid()

    def save_bundle_artifact(
        self,
        bundle: KnowledgeBundle,
        report: M3ValidationReport,
        snapshot: M3SeedSnapshot,
    ) -> None:
        try:
            payloads, metadata = self._approved_payloads(bundle, report, snapshot)
            self._store.publish(
                module=_MODULE,
                object_type=_BUNDLE_TYPE,
                object_id=bundle.knowledge_bundle_id,
                object_version=bundle.bundle_version,
                payloads=payloads,
                metadata=metadata,
            )
        except ArtifactStoreError as error:
            if error.reason == "version_conflict":
                raise _version_conflict() from None
            raise _invalid() from None
        except Exception:
            raise _invalid() from None

    def load_bundle_artifact(
        self, knowledge_bundle_id: str, bundle_version: str
    ) -> tuple[KnowledgeBundle, M3ValidationReport, M3SeedSnapshot] | None:
        try:
            loaded = self._store.load(
                module=_MODULE,
                object_type=_BUNDLE_TYPE,
                object_id=knowledge_bundle_id,
                object_version=bundle_version,
            )
        except ArtifactStoreError as error:
            if error.reason == "missing_artifact":
                return None
            raise _invalid() from None
        try:
            return self._decode_approved(
                loaded.payloads, loaded.metadata, knowledge_bundle_id, bundle_version,
            )
        except Exception:
            raise _invalid() from None

    def save_rejected_validation(
        self, report: M3ValidationReport, snapshot: M3SeedSnapshot
    ) -> None:
        try:
            payloads, metadata = self._rejected_payloads(report, snapshot)
            self._store.publish(
                module=_MODULE,
                object_type=_REJECTION_TYPE,
                object_id=report.course_package_id,
                object_version=report.report_id,
                payloads=payloads,
                metadata=metadata,
            )
        except Exception:
            # Rejections are content-addressed by report ID.  A collision is not
            # a bundle-version conflict and never permits replacement.
            raise _invalid() from None

    def load_rejected_validation(
        self, course_package_id: str, report_id: str
    ) -> tuple[M3ValidationReport, M3SeedSnapshot] | None:
        try:
            loaded = self._store.load(
                module=_MODULE,
                object_type=_REJECTION_TYPE,
                object_id=course_package_id,
                object_version=report_id,
            )
        except ArtifactStoreError as error:
            if error.reason == "missing_artifact":
                return None
            raise _invalid() from None
        try:
            return self._decode_rejected(loaded.payloads, loaded.metadata, course_package_id, report_id)
        except Exception:
            raise _invalid() from None

    @classmethod
    def _approved_payloads(
        cls,
        bundle: KnowledgeBundle,
        report: M3ValidationReport,
        snapshot: M3SeedSnapshot,
    ) -> tuple[dict[str, bytes], dict[str, str]]:
        bundle_bytes = cls._bundle_to_bytes(bundle)
        report_bytes = validation_report_to_bytes(report)
        snapshot_bytes = seed_snapshot_to_bytes(snapshot)
        bundle_value = cls._bundle_from_bytes(bundle_bytes)
        report_value = validation_report_from_bytes(report_bytes)
        snapshot_value = seed_snapshot_from_bytes(snapshot_bytes)
        cls._validate_approved(bundle_value, report_value, snapshot_value)
        payloads = {
            "bundle.json": bundle_bytes,
            "seed_snapshot.json": snapshot_bytes,
            "validation_report.json": report_bytes,
        }
        return payloads, cls._approved_metadata(bundle_value, report_value, snapshot_value)

    @classmethod
    def _rejected_payloads(
        cls, report: M3ValidationReport, snapshot: M3SeedSnapshot
    ) -> tuple[dict[str, bytes], dict[str, str]]:
        report_bytes = validation_report_to_bytes(report)
        snapshot_bytes = seed_snapshot_to_bytes(snapshot)
        report_value = validation_report_from_bytes(report_bytes)
        snapshot_value = seed_snapshot_from_bytes(snapshot_bytes)
        cls._validate_rejected(report_value, snapshot_value)
        payloads = {
            "seed_snapshot.json": snapshot_bytes,
            "validation_report.json": report_bytes,
        }
        return payloads, cls._rejected_metadata(report_value, snapshot_value)

    @classmethod
    def _decode_approved(
        cls,
        payloads: Mapping[str, bytes],
        metadata: Mapping[str, Any],
        knowledge_bundle_id: str,
        bundle_version: str,
    ) -> tuple[KnowledgeBundle, M3ValidationReport, M3SeedSnapshot]:
        if not isinstance(payloads, Mapping) or set(payloads) != _BUNDLE_PAYLOADS:
            raise ValueError("payload set differs")
        bundle = cls._bundle_from_bytes(payloads["bundle.json"])
        report = validation_report_from_bytes(payloads["validation_report.json"])
        snapshot = seed_snapshot_from_bytes(payloads["seed_snapshot.json"])
        if (
            cls._bundle_to_bytes(bundle) != payloads["bundle.json"]
            or validation_report_to_bytes(report) != payloads["validation_report.json"]
            or seed_snapshot_to_bytes(snapshot) != payloads["seed_snapshot.json"]
            or bundle.knowledge_bundle_id != knowledge_bundle_id
            or bundle.bundle_version != bundle_version
        ):
            raise ValueError("noncanonical bundle artifact")
        cls._validate_approved(bundle, report, snapshot)
        if not isinstance(metadata, Mapping) or dict(metadata) != cls._approved_metadata(bundle, report, snapshot):
            raise ValueError("metadata differs")
        return bundle.model_copy(deep=True), report, snapshot

    @classmethod
    def _decode_rejected(
        cls,
        payloads: Mapping[str, bytes],
        metadata: Mapping[str, Any],
        course_package_id: str,
        report_id: str,
    ) -> tuple[M3ValidationReport, M3SeedSnapshot]:
        if not isinstance(payloads, Mapping) or set(payloads) != _REJECTION_PAYLOADS:
            raise ValueError("payload set differs")
        report = validation_report_from_bytes(payloads["validation_report.json"])
        snapshot = seed_snapshot_from_bytes(payloads["seed_snapshot.json"])
        if (
            validation_report_to_bytes(report) != payloads["validation_report.json"]
            or seed_snapshot_to_bytes(snapshot) != payloads["seed_snapshot.json"]
            or report.course_package_id != course_package_id
            or report.report_id != report_id
        ):
            raise ValueError("noncanonical rejected artifact")
        cls._validate_rejected(report, snapshot)
        if not isinstance(metadata, Mapping) or dict(metadata) != cls._rejected_metadata(report, snapshot):
            raise ValueError("metadata differs")
        return report, snapshot

    @staticmethod
    def _bundle_to_bytes(bundle: KnowledgeBundle) -> bytes:
        validated = KnowledgeBundle.model_validate(bundle)
        validated.validate_business_rules()
        return dumps_json(validated.model_dump(mode="json")).encode("utf-8")

    @staticmethod
    def _bundle_from_bytes(payload: bytes) -> KnowledgeBundle:
        if type(payload) is not bytes:
            raise ValueError("bundle payload is invalid")

        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate bundle key")
                value[key] = item
            return value

        value = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicates)
        if type(value) is not dict:
            raise ValueError("bundle root is invalid")
        bundle = KnowledgeBundle.model_validate(value)
        if FileM3Repository._bundle_to_bytes(bundle) != payload:
            raise ValueError("bundle is not canonical")
        return bundle

    @staticmethod
    def _validate_approved(
        bundle: KnowledgeBundle,
        report: M3ValidationReport,
        snapshot: M3SeedSnapshot,
    ) -> None:
        bundle.validate_business_rules()
        if (
            bundle.status != "published"
            or bundle.course_package_checksum is None
            or report.status != "approved"
            or report.issues
            or report.seed_snapshot_checksum != snapshot.checksum
            or report.knowledge_bundle_id != bundle.knowledge_bundle_id
            or report.bundle_version != bundle.bundle_version
            or report.bundle_checksum != bundle.content_checksum()
            or report.course_package_id != bundle.course_package_id
            or report.course_package_checksum != bundle.course_package_checksum
        ):
            raise ValueError("approved bindings differ")

    @staticmethod
    def _validate_rejected(report: M3ValidationReport, snapshot: M3SeedSnapshot) -> None:
        if (
            report.status != "rejected"
            or not report.issues
            or report.seed_snapshot_checksum != snapshot.checksum
            or any(value is not None for value in (
                report.knowledge_bundle_id, report.bundle_version, report.bundle_checksum,
            ))
        ):
            raise ValueError("rejected bindings differ")

    @staticmethod
    def _approved_metadata(
        bundle: KnowledgeBundle, report: M3ValidationReport, snapshot: M3SeedSnapshot
    ) -> dict[str, str]:
        return {
            "format": _BUNDLE_FORMAT,
            "course_package_id": bundle.course_package_id,
            "course_package_checksum": bundle.course_package_checksum or "",
            "seed_snapshot_checksum": snapshot.checksum,
            "validation_report_checksum": report.checksum,
            "bundle_checksum": bundle.content_checksum(),
            "status": "approved",
        }

    @staticmethod
    def _rejected_metadata(
        report: M3ValidationReport, snapshot: M3SeedSnapshot
    ) -> dict[str, str]:
        return {
            "format": _REJECTION_FORMAT,
            "course_package_id": report.course_package_id,
            "course_package_checksum": report.course_package_checksum,
            "seed_snapshot_checksum": snapshot.checksum,
            "validation_report_checksum": report.checksum,
            "status": "rejected",
        }


__all__ = ["FileM3Repository"]
