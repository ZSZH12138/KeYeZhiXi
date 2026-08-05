"""Deterministic local M3 service stub."""

from __future__ import annotations

import json
from typing import Any

from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.infrastructure.json_io import dumps_json
from course_insight.modules.m3_knowledge_bundle.repository import M3Repository
from course_insight.modules.m3_knowledge_bundle.seed_snapshot import (
    M3SeedSnapshot,
    M3ValidationReport,
    seed_snapshot_from_bytes,
    seed_snapshot_to_bytes,
    validation_report_from_bytes,
    validation_report_to_bytes,
)
from course_insight.modules.m3_knowledge_bundle.service import M3KnowledgeBundleService


class _MemoryM3Repository:
    def __init__(self) -> None:
        self.bundles: dict[tuple[str, str], KnowledgeBundle] = {}
        self._approved: dict[tuple[str, str], tuple[bytes, bytes, bytes]] = {}
        self._rejected: dict[tuple[str, str], tuple[bytes, bytes]] = {}

    def save_knowledge_bundle(self, bundle: KnowledgeBundle) -> None:
        del bundle
        raise DomainError(
            code="KNOWLEDGE_ARTIFACT_INVALID",
            module="m3",
            message="complete knowledge artifacts are required",
        )

    def get_knowledge_bundle(
        self,
        knowledge_bundle_id: str,
        bundle_version: str,
    ) -> KnowledgeBundle | None:
        artifact = self.load_bundle_artifact(knowledge_bundle_id, bundle_version)
        return None if artifact is None else artifact[0]

    def save_bundle_artifact(
        self,
        bundle: KnowledgeBundle,
        report: M3ValidationReport,
        snapshot: M3SeedSnapshot,
    ) -> None:
        try:
            bundle_bytes = _bundle_to_bytes(bundle)
            report_bytes = validation_report_to_bytes(report)
            snapshot_bytes = seed_snapshot_to_bytes(snapshot)
            _validate_approved(bundle, report, snapshot)
        except Exception:
            raise DomainError(
                code="KNOWLEDGE_ARTIFACT_INVALID",
                module="m3",
                message="knowledge artifact is invalid",
            ) from None
        key = (bundle.knowledge_bundle_id, bundle.bundle_version)
        candidate = (bundle_bytes, report_bytes, snapshot_bytes)
        stored_bundle = _bundle_from_bytes(bundle_bytes)
        existing = self._approved.get(key)
        if existing is not None and existing != candidate:
            raise DomainError(
                code="KNOWLEDGE_VERSION_CONFLICT",
                module="m3",
                message="knowledge bundle version conflicts",
            )
        if existing == candidate:
            return
        sentinel = object()
        previous_approved = self._approved.get(key, sentinel)
        previous_bundle = self.bundles.get(key, sentinel)
        try:
            self.bundles[key] = stored_bundle
            self._approved[key] = candidate
        except Exception:
            _restore_mapping(self._approved, key, previous_approved, sentinel)
            _restore_mapping(self.bundles, key, previous_bundle, sentinel)
            raise DomainError(
                code="KNOWLEDGE_ARTIFACT_INVALID",
                module="m3",
                message="knowledge artifact is invalid",
            ) from None

    def load_bundle_artifact(
        self,
        knowledge_bundle_id: str,
        bundle_version: str,
    ) -> tuple[KnowledgeBundle, M3ValidationReport, M3SeedSnapshot] | None:
        payload = self._approved.get((knowledge_bundle_id, bundle_version))
        if payload is None:
            return None
        try:
            bundle = _bundle_from_bytes(payload[0])
            report = validation_report_from_bytes(payload[1])
            snapshot = seed_snapshot_from_bytes(payload[2])
            _validate_approved(bundle, report, snapshot)
            return bundle, report, snapshot
        except Exception:
            raise DomainError(
                code="KNOWLEDGE_ARTIFACT_INVALID",
                module="m3",
                message="knowledge artifact is invalid",
            ) from None

    def save_rejected_validation(
        self,
        report: M3ValidationReport,
        snapshot: M3SeedSnapshot,
    ) -> None:
        try:
            report_bytes = validation_report_to_bytes(report)
            snapshot_bytes = seed_snapshot_to_bytes(snapshot)
            _validate_rejected(report, snapshot)
        except Exception:
            raise DomainError(
                code="KNOWLEDGE_ARTIFACT_INVALID",
                module="m3",
                message="knowledge artifact is invalid",
            ) from None
        key = (report.course_package_id, report.report_id)
        candidate = (report_bytes, snapshot_bytes)
        existing = self._rejected.get(key)
        if existing is not None and existing != candidate:
            raise DomainError(
                code="KNOWLEDGE_ARTIFACT_INVALID",
                module="m3",
                message="knowledge artifact is invalid",
            )
        self._rejected[key] = candidate

    def load_rejected_validation(
        self,
        course_package_id: str,
        report_id: str,
    ) -> tuple[M3ValidationReport, M3SeedSnapshot] | None:
        payload = self._rejected.get((course_package_id, report_id))
        if payload is None:
            return None
        try:
            report = validation_report_from_bytes(payload[0])
            snapshot = seed_snapshot_from_bytes(payload[1])
            _validate_rejected(report, snapshot)
            return report, snapshot
        except Exception:
            raise DomainError(
                code="KNOWLEDGE_ARTIFACT_INVALID",
                module="m3",
                message="knowledge artifact is invalid",
            ) from None


def _bundle_to_bytes(bundle: KnowledgeBundle) -> bytes:
    validated = KnowledgeBundle.model_validate(bundle)
    return dumps_json(validated.model_dump(mode="json")).encode("utf-8")


def _bundle_from_bytes(payload: bytes) -> KnowledgeBundle:
    if type(payload) is not bytes:
        raise ValueError
    value: Any = json.loads(payload)
    if dumps_json(value).encode("utf-8") != payload:
        raise ValueError
    return KnowledgeBundle.model_validate(value)


def _validate_approved(
    bundle: KnowledgeBundle,
    report: M3ValidationReport,
    snapshot: M3SeedSnapshot,
) -> None:
    bundle.validate_business_rules()
    if (
        report.status != "approved"
        or report.issues
        or report.seed_snapshot_checksum != snapshot.checksum
        or report.knowledge_bundle_id != bundle.knowledge_bundle_id
        or report.bundle_version != bundle.bundle_version
        or report.bundle_checksum != bundle.content_checksum()
        or report.course_package_id != bundle.course_package_id
        or report.course_package_checksum != bundle.course_package_checksum
    ):
        raise ValueError


def _validate_rejected(report: M3ValidationReport, snapshot: M3SeedSnapshot) -> None:
    if (
        report.status != "rejected"
        or not report.issues
        or report.seed_snapshot_checksum != snapshot.checksum
        or any(value is not None for value in (report.knowledge_bundle_id, report.bundle_version, report.bundle_checksum))
    ):
        raise ValueError


def _restore_mapping(
    mapping: dict[tuple[str, str], Any],
    key: tuple[str, str],
    previous: Any,
    sentinel: object,
) -> None:
    if previous is sentinel:
        mapping.pop(key, None)
    else:
        mapping[key] = previous


def _accept_schema(bundle: KnowledgeBundle) -> bool:
    return bool(bundle.concepts and bundle.items and bundle.blueprints)


class M3KnowledgeBundleServiceStub(M3KnowledgeBundleService):
    """Instantiate M3 with fixed local placeholder dependencies."""

    def __init__(self) -> None:
        repository: M3Repository = _MemoryM3Repository()
        super().__init__(repository, _accept_schema)
