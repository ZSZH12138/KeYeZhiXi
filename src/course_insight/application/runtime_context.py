"""Process-local course runtime restoration from validated snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from threading import RLock
from types import MappingProxyType
from typing import Mapping

from course_insight.contracts.course import CoursePackage
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceIndexRef
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.modules.m0_platform.service import M0PlatformService
from course_insight.modules.m2_evidence_retrieval.service import (
    M2EvidenceRetrievalService,
)


@dataclass(frozen=True, slots=True)
class _DiscardingM2Repository:
    """No-op repository used to validate deterministic rebuild metadata."""

    def save_index(self, index: EvidenceIndexRef) -> None:
        del index

    def get_index(
        self,
        index_id: str,
        index_version: str,
    ) -> EvidenceIndexRef | None:
        del index_id, index_version
        return None


@dataclass(frozen=True, slots=True)
class RuntimeSnapshotRefs:
    """Portable runtime-relative references for one course context."""

    course_package_ref: Path
    evidence_index_ref: Path
    knowledge_bundle_ref: Path


@dataclass(frozen=True, slots=True)
class CourseRuntimeContext:
    """One isolated set of contracts used by request-time application flows."""

    course_package: CoursePackage
    evidence_index_ref: EvidenceIndexRef
    knowledge_bundle: KnowledgeBundle

    def isolated_copy(self) -> "CourseRuntimeContext":
        return CourseRuntimeContext(
            course_package=self.course_package.model_copy(deep=True),
            evidence_index_ref=self.evidence_index_ref.model_copy(deep=True),
            knowledge_bundle=self.knowledge_bundle.model_copy(deep=True),
        )


@dataclass(frozen=True, slots=True)
class CourseRuntimeRegistry:
    """Restore snapshots through M0 and rebuild M2's process-local index."""

    m0_service: M0PlatformService
    m2_service: M2EvidenceRetrievalService
    runtime_dir: Path
    _contexts: Mapping[str, CourseRuntimeContext] = field(
        default_factory=lambda: MappingProxyType({}),
        init=False,
        repr=False,
        compare=False,
    )
    _lock: RLock = field(
        default_factory=RLock,
        init=False,
        repr=False,
        compare=False,
    )

    def restore(
        self,
        course_id: str,
        refs: RuntimeSnapshotRefs,
    ) -> CourseRuntimeContext:
        """Load, cross-check, rebuild, and atomically publish one context."""

        if not course_id.strip():
            self._raise_invalid("identity_mismatch")
        paths = self._resolve_refs(refs)
        try:
            package = self.m0_service.load_contract_snapshot(
                CoursePackage,
                paths.course_package_ref,
            )
            expected_index = self.m0_service.load_contract_snapshot(
                EvidenceIndexRef,
                paths.evidence_index_ref,
            )
            bundle = self.m0_service.load_contract_snapshot(
                KnowledgeBundle,
                paths.knowledge_bundle_ref,
            )
        except DomainError as error:
            reason = (
                "snapshot_unavailable"
                if isinstance(error.__cause__, FileNotFoundError)
                else "snapshot_invalid"
            )
            self._raise_invalid(reason, cause=error)

        self._validate_contracts(
            course_id=course_id,
            package=package,
            expected_index=expected_index,
            bundle=bundle,
        )
        preview_service = M2EvidenceRetrievalService(
            self.runtime_dir / "indexes",
            "lexical",
            _DiscardingM2Repository(),
        )
        try:
            preview_index = preview_service.build_index(package)
        except Exception as error:
            self._raise_invalid("index_rebuild_failed", cause=error)
        self._validate_rebuilt_index(expected_index, preview_index)
        try:
            rebuilt_index = self.m2_service.build_index(package)
        except Exception as error:
            self._raise_invalid("index_rebuild_failed", cause=error)
        self._validate_rebuilt_index(expected_index, rebuilt_index)

        context = CourseRuntimeContext(
            course_package=package.model_copy(deep=True),
            evidence_index_ref=rebuilt_index.model_copy(deep=True),
            knowledge_bundle=bundle.model_copy(deep=True),
        )
        with self._lock:
            updated = {**self._contexts, course_id: context}
            object.__setattr__(self, "_contexts", MappingProxyType(updated))
        return context.isolated_copy()

    def require(self, course_id: str) -> CourseRuntimeContext:
        """Return one isolated ready context or fail closed."""

        with self._lock:
            context = self._contexts.get(course_id)
        if context is None:
            raise DomainError(
                code="RUNTIME_CONTEXT_UNAVAILABLE",
                module="application",
                message="course runtime context is not ready",
                details={"course_id": course_id},
                recoverable=True,
            )
        return context.isolated_copy()

    def ready_course_ids(self) -> tuple[str, ...]:
        """Return stable, non-sensitive ready identities."""

        with self._lock:
            return tuple(sorted(self._contexts))

    def _resolve_refs(
        self,
        refs: RuntimeSnapshotRefs,
    ) -> RuntimeSnapshotRefs:
        try:
            return RuntimeSnapshotRefs(
                course_package_ref=self._resolve_ref(refs.course_package_ref),
                evidence_index_ref=self._resolve_ref(refs.evidence_index_ref),
                knowledge_bundle_ref=self._resolve_ref(
                    refs.knowledge_bundle_ref
                ),
            )
        except (OSError, TypeError, ValueError) as error:
            self._raise_invalid("unsafe_reference", cause=error)

    def _resolve_ref(self, reference: Path) -> Path:
        path = Path(reference)
        windows_path = PureWindowsPath(str(reference))
        if (
            path.is_absolute()
            or path.drive
            or windows_path.is_absolute()
            or windows_path.drive
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("snapshot reference must be runtime-relative")
        runtime_root = self.runtime_dir.resolve()
        resolved = (runtime_root / path).resolve()
        if not resolved.is_relative_to(runtime_root):
            raise ValueError("snapshot reference escaped runtime")
        return resolved

    @classmethod
    def _validate_contracts(
        cls,
        *,
        course_id: str,
        package: CoursePackage,
        expected_index: EvidenceIndexRef,
        bundle: KnowledgeBundle,
    ) -> None:
        if package.checksum != package.recalculate_checksum():
            cls._raise_invalid("package_checksum_mismatch")
        if (
            package.course_id != course_id
            or bundle.course_id != course_id
            or bundle.course_package_id != package.course_package_id
            or expected_index.course_package_id != package.course_package_id
        ):
            cls._raise_invalid("identity_mismatch")
        if package.status != "ready" or bundle.status != "published":
            cls._raise_invalid("context_not_ready")
        if expected_index.status != "ready":
            cls._raise_invalid("index_not_ready")
        if not expected_index.matches(package):
            cls._raise_invalid("identity_mismatch")

    @classmethod
    def _validate_rebuilt_index(
        cls,
        expected: EvidenceIndexRef,
        rebuilt: EvidenceIndexRef,
    ) -> None:
        comparable_fields = (
            "index_id",
            "course_package_id",
            "index_version",
            "storage_ref",
            "backend",
            "source_count",
            "chunk_count",
            "checksum",
            "status",
        )
        if any(
            getattr(expected, name) != getattr(rebuilt, name)
            for name in comparable_fields
        ):
            cls._raise_invalid("index_mismatch")

    @staticmethod
    def _raise_invalid(
        reason: str,
        *,
        cause: BaseException | None = None,
    ) -> None:
        error = DomainError(
            code="RUNTIME_SNAPSHOT_INVALID",
            module="application",
            message="course runtime snapshots could not be restored",
            details={"reason": reason},
            recoverable=True,
        )
        if cause is None:
            raise error
        raise error from cause
