"""Process-local course runtime restoration from immutable M1-M3 artifacts."""

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
from course_insight.modules.m1_course_governance.repository import M1Repository
from course_insight.modules.m2_evidence_retrieval.service import (
    M2EvidenceRetrievalService,
)
from course_insight.modules.m2_evidence_retrieval.repository import M2Repository
from course_insight.modules.m3_knowledge_bundle.repository import M3Repository


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
    """Restore M1-M3 immutable artifacts and publish one isolated context."""

    m0_service: M0PlatformService
    m2_service: M2EvidenceRetrievalService
    runtime_dir: Path
    m1_repository: M1Repository | None = None
    m2_repository: M2Repository | None = None
    m3_repository: M3Repository | None = None
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
        """Load, cross-check, and atomically publish one artifact-backed context."""

        if not course_id.strip():
            self._raise_invalid("identity_mismatch")
        paths = self._resolve_refs(refs)
        try:
            package_snapshot = self.m0_service.load_contract_snapshot(
                CoursePackage,
                paths.course_package_ref,
            )
            index_snapshot = self.m0_service.load_contract_snapshot(
                EvidenceIndexRef,
                paths.evidence_index_ref,
            )
            bundle_snapshot = self.m0_service.load_contract_snapshot(
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

        if (
            self.m1_repository is None
            or self.m2_repository is None
            or self.m3_repository is None
        ):
            self._raise_invalid("repository_unavailable")

        try:
            package = self.m1_repository.get_course_package(
                package_snapshot.course_package_id,
                package_snapshot.package_version,
            )
        except DomainError as error:
            self._raise_invalid("artifact_invalid", cause=error)
        except Exception as error:
            self._raise_invalid("artifact_invalid", cause=error)
        if package is None:
            self._raise_invalid("artifact_unavailable")

        if index_snapshot.backend == "pgvector":
            # pgvector rows are durable in the vector store, not in the
            # lexical artifact codec.  The snapshot supplies identity while
            # M2 proves the persisted metadata before publishing runtime state.
            expected_index = index_snapshot
        else:
            try:
                loaded_index = self.m2_repository.load_index_artifact(
                    index_snapshot.index_id,
                    index_snapshot.index_version,
                )
            except DomainError as error:
                self._raise_invalid("artifact_invalid", cause=error)
            except Exception as error:
                self._raise_invalid("artifact_invalid", cause=error)
            if loaded_index is None:
                self._raise_invalid("artifact_unavailable")
            try:
                expected_index, _index_payload = loaded_index
            except (TypeError, ValueError) as error:
                self._raise_invalid("artifact_invalid", cause=error)

        try:
            loaded_bundle = self.m3_repository.load_bundle_artifact(
                bundle_snapshot.knowledge_bundle_id,
                bundle_snapshot.bundle_version,
            )
        except DomainError as error:
            self._raise_invalid("artifact_invalid", cause=error)
        except Exception as error:
            self._raise_invalid("artifact_invalid", cause=error)
        if loaded_bundle is None:
            self._raise_invalid("artifact_unavailable")
        try:
            bundle, _validation_report, _seed_snapshot = loaded_bundle
        except (TypeError, ValueError) as error:
            self._raise_invalid("artifact_invalid", cause=error)

        self._validate_snapshot_bindings(
            package_snapshot=package_snapshot,
            index_snapshot=index_snapshot,
            bundle_snapshot=bundle_snapshot,
            package=package,
            expected_index=expected_index,
            bundle=bundle,
        )
        self._validate_contracts(
            course_id=course_id,
            package=package,
            expected_index=expected_index,
            bundle=bundle,
        )

        try:
            if expected_index.backend == "pgvector":
                loaded_index_ref = self.m2_service.restore_vector_index_from_store(
                    course_package=package,
                    index_id=expected_index.index_id,
                    index_version=expected_index.index_version,
                )
            else:
                loaded_index_ref = self.m2_service.restore_index(
                    course_package=package,
                    evidence_index_ref=expected_index,
                )
        except DomainError as error:
            self._raise_invalid("artifact_invalid", cause=error)
        except Exception as error:
            self._raise_invalid("artifact_invalid", cause=error)
        self._validate_loaded_index(expected_index, loaded_index_ref)

        context = CourseRuntimeContext(
            course_package=package.model_copy(deep=True),
            evidence_index_ref=loaded_index_ref.model_copy(deep=True),
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
            or bundle.course_package_checksum != package.checksum
            or expected_index.course_package_id != package.course_package_id
        ):
            cls._raise_invalid("identity_mismatch")
        if package.status != "ready" or bundle.status != "published":
            cls._raise_invalid("context_not_ready")
        if expected_index.status != "ready":
            cls._raise_invalid("index_not_ready")
        if (
            expected_index.course_package_checksum != package.checksum
            or not expected_index.matches(package)
        ):
            cls._raise_invalid("identity_mismatch")

    @classmethod
    def _validate_snapshot_bindings(
        cls,
        *,
        package_snapshot: CoursePackage,
        index_snapshot: EvidenceIndexRef,
        bundle_snapshot: KnowledgeBundle,
        package: CoursePackage,
        expected_index: EvidenceIndexRef,
        bundle: KnowledgeBundle,
    ) -> None:
        """Treat snapshots as identity cross-checks, never as runtime data."""

        if (
            package_snapshot.course_package_id != package.course_package_id
            or package_snapshot.package_version != package.package_version
            or package_snapshot.course_id != package.course_id
            or package_snapshot.checksum != package.checksum
            or index_snapshot.index_id != expected_index.index_id
            or index_snapshot.index_version != expected_index.index_version
            or index_snapshot.course_package_id != expected_index.course_package_id
            or index_snapshot.course_package_checksum
            != expected_index.course_package_checksum
            or index_snapshot.checksum != expected_index.checksum
            or bundle_snapshot.knowledge_bundle_id != bundle.knowledge_bundle_id
            or bundle_snapshot.bundle_version != bundle.bundle_version
            or bundle_snapshot.course_package_id != bundle.course_package_id
            or bundle_snapshot.course_id != bundle.course_id
            or bundle_snapshot.course_package_checksum
            != bundle.course_package_checksum
            or bundle_snapshot.content_checksum() != bundle.content_checksum()
        ):
            cls._raise_invalid("artifact_binding_mismatch")

    @classmethod
    def _validate_loaded_index(
        cls,
        expected: EvidenceIndexRef,
        loaded: EvidenceIndexRef,
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
            getattr(expected, name) != getattr(loaded, name)
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
