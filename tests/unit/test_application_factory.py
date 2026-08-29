from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from course_insight import cli
from course_insight.application import factory as application_factory
from course_insight.application.factory import (
    ApplicationContainer,
    RepositoryOverrides,
    ServiceOverrides,
    build_application,
)
from course_insight.application.runtime_context import RuntimeSnapshotRefs
from course_insight.contracts.course import (
    ContentChunk,
    CoursePackage,
    SourceAuthorization,
    SourceDocument,
)
from course_insight.contracts.assessment import RemediationPlan, ScoringResultBundle
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceIndexRef, EvidenceQuery
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.platform import ActorContext
from course_insight.contracts.state import (
    ClassStateSnapshot,
    ConceptState,
    DiagnosisResult,
    ItemDiagnosis,
    LearnerStateSnapshot,
    StateUpdateResult,
)
from course_insight.infrastructure.config import (
    DatabaseSettings,
    IntentSettings,
    LoggingSettings,
    PlatformSettings,
    RetrievalSettings,
)
from course_insight.infrastructure.config.models import M6PolicySettings
from course_insight.infrastructure.sqlite.m1_m2_m3_repository import (
    SQLiteM1M2M3Repository,
)
from course_insight.modules.m1_course_governance.parsers import parse_source
from course_insight.modules.m1_course_governance.snapshots import (
    CourseImportSnapshot,
    SourcePayload,
)
from course_insight.modules.m1_course_governance.stubs import (
    M1CourseGovernanceServiceStub,
)
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    CandidateAction,
    PolicyArtifactManifest,
    PolicyEvaluationRecord,
    TutoringPolicyContext,
)
from course_insight.modules.m6_tutoring_fsm.decision_policy import (
    DecisionSignals,
)
from course_insight.infrastructure.postgresql.m0_repository import (
    PostgresM0Repository,
)
from course_insight.infrastructure.postgresql.m1_m2_m3_repository import (
    PostgresM1M2M3Repository,
)
from course_insight.infrastructure.postgresql.m4_repository import (
    PostgresM4Repository,
)
from course_insight.infrastructure.postgresql.m5_repository import (
    PostgresM5Repository,
)
from course_insight.infrastructure.postgresql.m6_repository import (
    PostgresM6Repository,
)
from course_insight.infrastructure.postgresql.m7_repository import (
    PostgresM7Repository,
)
from course_insight.infrastructure.postgresql.m8_repository import (
    PostgresM8Repository,
)
from course_insight.infrastructure.postgresql.m9_repository import (
    PostgresM9Repository,
)
from course_insight.infrastructure.sqlite.m0_repository import SQLiteM0Repository
from course_insight.infrastructure.sqlite.m4_repository import SQLiteM4Repository
from course_insight.infrastructure.sqlite.m5_repository import SQLiteM5Repository
from course_insight.infrastructure.sqlite.m6_repository import SQLiteM6Repository
from course_insight.infrastructure.sqlite.m7_repository import SQLiteM7Repository
from course_insight.infrastructure.sqlite.m8_repository import SQLiteM8Repository
from course_insight.infrastructure.sqlite.m9_repository import SQLiteM9Repository
from course_insight.modules.m0_platform.service import M0PlatformService
from course_insight.modules.m0_platform.outbox_worker import OutboxWorker
from course_insight.modules.m2_evidence_retrieval.stubs import (
    M2EvidenceRetrievalServiceStub,
)
from course_insight.modules.m2_evidence_retrieval.lexical import (
    LexicalIndexSnapshot,
    snapshot_from_payloads,
    snapshot_to_payloads,
)
from course_insight.modules.m2_evidence_retrieval.service import (
    M2EvidenceRetrievalService,
)
from course_insight.modules.m3_knowledge_bundle.stubs import (
    M3KnowledgeBundleServiceStub,
    _MemoryM3Repository,
)
from course_insight.modules.m3_knowledge_bundle.seed_snapshot import (
    create_validation_report,
)
from course_insight.modules.m4_task_orchestration import sklearn_adapter
from course_insight.modules.m4_task_orchestration.intent import IntentStatus
from course_insight.modules.m4_task_orchestration.intent_service import (
    StoredIntentDecision,
)
from course_insight.modules.m4_task_orchestration.routing import (
    resolve_task_type,
)
from course_insight.modules.m4_task_orchestration.sklearn_adapter import (
    IntentArtifactError,
)


NOW = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
MODEL_SHA256 = "a" * 64


def _settings(
    tmp_path: Path,
    *,
    backend: str = "sqlite",
    m6_policy: M6PolicySettings | None = None,
    intent: IntentSettings | None = None,
) -> PlatformSettings:
    database_url = (
        SecretStr("postgresql://user:password@db.invalid/course_insight")
        if backend == "postgresql"
        else None
    )
    runtime_dir = tmp_path / "runtime"
    return PlatformSettings(
        environment="test",
        runtime_dir=runtime_dir,
        config_dir=tmp_path / "config",
        database=DatabaseSettings(
            backend=backend,
            sqlite_path=runtime_dir / "course_insight.sqlite3",
            url=database_url,
        ),
        logging=LoggingSettings(directory=runtime_dir / "logs"),
        m6_policy=m6_policy or M6PolicySettings(),
        intent=IntentSettings() if intent is None else intent,
    )


def _course_package() -> CoursePackage:
    chunk = ContentChunk(
        chunk_id="chunk_1",
        source_id="source_1",
        text="A governed rule explains the target concept.",
        locator="section:1",
        concept_hints=["concept_1"],
        sha256=hashlib.sha256(
            b"A governed rule explains the target concept."
        ).hexdigest(),
    )
    candidate = CoursePackage(
        course_package_id="package_1",
        course_id="course_1",
        package_version="1.0.0",
        source_documents=[
            SourceDocument(
                source_id="source_1",
                file_name="course.md",
                media_type="text/markdown",
                sha256=hashlib.sha256(b"course").hexdigest(),
                page_count=None,
                title="Governed course",
                version="1.0.0",
            )
        ],
        content_chunks=[chunk],
        source_authorizations=[
            SourceAuthorization(
                source_id="source_1",
                authorized_by="Teacher",
                license_note="course use",
                authorized_at=NOW,
            )
        ],
        imported_at=NOW,
        status="ready",
        checksum="pending",
    )
    return candidate.model_copy(
        update={"checksum": candidate.recalculate_checksum()},
        deep=True,
    )


def _runtime_course_package() -> CoursePackage:
    package = _course_package()
    chunk = package.content_chunks[0]
    chunk_id = "chunk_" + hashlib.sha256(
        f"{chunk.source_id}\0{chunk.locator}\0{chunk.sha256}".encode(
            "utf-8"
        )
    ).hexdigest()
    candidate = package.model_copy(
        update={
            "content_chunks": [
                chunk.model_copy(update={"chunk_id": chunk_id}, deep=True)
            ],
            "checksum": "pending",
        },
        deep=True,
    )
    return candidate.model_copy(
        update={"checksum": candidate.recalculate_checksum()},
        deep=True,
    )


def _knowledge_bundle() -> KnowledgeBundle:
    return KnowledgeBundle(
        knowledge_bundle_id="bundle_1",
        course_package_id="package_1",
        course_id="course_1",
        bundle_version="1.0.0",
        concepts=[],
        prerequisite_relations=[],
        misconception_tags=[],
        items=[],
        rubrics=[],
        blueprints=[],
        q_matrix=[],
        status="published",
        published_at=NOW,
    )


def _snapshot_refs(
    container: ApplicationContainer,
    *,
    index_update: dict[str, object] | None = None,
) -> RuntimeSnapshotRefs:
    package = _runtime_course_package()
    source_bytes = b"course"
    metadata_bytes = json.dumps(
        {
            "course_package_id": package.course_package_id,
            "course_id": package.course_id,
            "package_version": package.package_version,
            "course_name": package.source_documents[0].title,
            "imported_at": package.imported_at.isoformat(),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    authorization_bytes = (
        "file_name,source_id,expected_sha256,authorized_by,authorized_at,license_note\n"
        f"course.md,source_1,{hashlib.sha256(source_bytes).hexdigest()},"
        f"Teacher,{NOW.isoformat()},course use\n"
    ).encode("utf-8")
    container.m1_service._repository.save_course_import(  # noqa: SLF001
        package,
        CourseImportSnapshot(
            course_metadata_bytes=metadata_bytes,
            source_authorization_bytes=authorization_bytes,
            source_payloads=(
                SourcePayload(
                    source_id="source_1",
                    file_name="course.md",
                    raw_bytes=source_bytes,
                ),
            ),
        ),
    )
    expected_index = container.m2_service.build_index(package)
    bundle_paths = _m3_seed_paths(
        container.settings.runtime_dir / "teacher-seeds", package
    )
    bundle = container.m3_service.build_knowledge_bundle(
        package,
        bundle_paths["concept"],
        bundle_paths["item"],
        bundle_paths["rubric"],
        bundle_paths["blueprint"],
        bundle_paths["prerequisite"],
        bundle_paths["misconception"],
    )
    snapshot_index = expected_index
    if index_update:
        snapshot_index = snapshot_index.model_copy(
            update=index_update,
            deep=True,
        )
    snapshot_dir = container.settings.runtime_dir / "snapshots"
    refs = RuntimeSnapshotRefs(
        course_package_ref=Path("snapshots/course-package.json"),
        evidence_index_ref=Path("snapshots/evidence-index.json"),
        knowledge_bundle_ref=Path("snapshots/knowledge-bundle.json"),
    )
    container.m0_service.save_contract_snapshot(
        package,
        snapshot_dir / "course-package.json",
    )
    container.m0_service.save_contract_snapshot(
        snapshot_index,
        snapshot_dir / "evidence-index.json",
    )
    container.m0_service.save_contract_snapshot(
        bundle,
        snapshot_dir / "knowledge-bundle.json",
    )
    return refs


def test_real_django_does_not_break_legacy_empty_architecture_scaffold(
    tmp_path: Path,
) -> None:
    container = build_application(_settings(tmp_path))
    actor = ActorContext(
        actor_id="pseudonym_teacher_architecture",
        role="teacher",
        course_ids=["course_1"],
        class_ids=["class_1"],
        issued_at=NOW,
    )

    try:
        result = container.coordinator.run_intelligence_architecture(
            actor_context=actor,
            course_package_id="package_1",
            learner_id="pseudonym_learner_architecture",
            requested_at=NOW,
        )
    finally:
        container.close()

    assert result.is_empty()
    assert result.web_job_status.status == "skipped"


class _RepositorySentinel:
    pass


class _RuntimeM1Repository:
    def __init__(self, package: CoursePackage) -> None:
        self.package: CoursePackage | None = package

    def get_course_package(
        self,
        course_package_id: str,
        package_version: str,
    ) -> CoursePackage | None:
        package = self.package
        if package is None:
            return None
        if (
            package.course_package_id != course_package_id
            or package.package_version != package_version
        ):
            return None
        return package.model_copy(deep=True)


class _RecordingM2Repository:
    """Complete M2 artifact double: records only successful immutable values."""

    def __init__(self, *, fail_save: bool = False, fail_load: bool = False) -> None:
        self.fail_save = fail_save
        self.fail_load = fail_load
        self.artifacts: dict[
            tuple[str, str], tuple[EvidenceIndexRef, dict[str, bytes]]
        ] = {}
        self.save_calls: list[tuple[str, str]] = []
        self.load_calls: list[tuple[str, str]] = []

    @staticmethod
    def _copy_snapshot(snapshot: LexicalIndexSnapshot) -> LexicalIndexSnapshot:
        return snapshot_from_payloads(snapshot_to_payloads(snapshot))

    def save_index(self, index: EvidenceIndexRef) -> None:
        del index
        raise AssertionError("M2 must save complete artifacts")

    def get_index(
        self, index_id: str, index_version: str
    ) -> EvidenceIndexRef | None:
        artifact = self.load_index_artifact(index_id, index_version)
        return None if artifact is None else artifact[0]

    def save_index_artifact(
        self, index: EvidenceIndexRef, snapshot: LexicalIndexSnapshot
    ) -> None:
        self.save_calls.append((index.index_id, index.index_version))
        if self.fail_save:
            raise OSError("recording storage is unavailable")
        payloads = snapshot_to_payloads(snapshot)
        stored = (index.model_copy(deep=True), dict(payloads))
        key = (index.index_id, index.index_version)
        current = self.artifacts.get(key)
        if current is not None and current != stored:
            raise DomainError(
                code="INDEX_VERSION_CONFLICT",
                module="m2",
                message="evidence index version conflicts with an immutable artifact",
                details={},
            )
        self.artifacts.setdefault(key, stored)

    def load_index_artifact(
        self, index_id: str, index_version: str
    ) -> tuple[EvidenceIndexRef, LexicalIndexSnapshot] | None:
        self.load_calls.append((index_id, index_version))
        if self.fail_load:
            raise OSError("recording storage is unavailable")
        current = self.artifacts.get((index_id, index_version))
        if current is None:
            return None
        return current[0].model_copy(deep=True), self._copy_snapshot(
            snapshot_from_payloads(current[1])
        )


class _RecordingM3Repository(_MemoryM3Repository):
    """Complete M3 artifact double with explicit approved/rejected records."""

    def __init__(self, *, fail_save: bool = False, fail_load: bool = False) -> None:
        super().__init__()
        self.fail_save = fail_save
        self.fail_load = fail_load
        self.approved: list[tuple[Any, Any, Any]] = []
        self.rejected: list[tuple[Any, Any]] = []

    def save_bundle_artifact(self, bundle: Any, report: Any, snapshot: Any) -> None:
        self.approved.append((bundle, report, snapshot))
        if self.fail_save:
            raise OSError("recording storage is unavailable")
        super().save_bundle_artifact(bundle, report, snapshot)

    def save_rejected_validation(self, report: Any, snapshot: Any) -> None:
        self.rejected.append((report, snapshot))
        if self.fail_save:
            raise OSError("recording storage is unavailable")
        super().save_rejected_validation(report, snapshot)

    def load_bundle_artifact(
        self,
        knowledge_bundle_id: str,
        bundle_version: str,
    ) -> Any:
        if self.fail_load:
            raise OSError("recording storage is unavailable")
        return super().load_bundle_artifact(knowledge_bundle_id, bundle_version)


def _m3_seed_paths(
    tmp_path: Path,
    package: CoursePackage,
    *,
    valid: bool = True,
) -> dict[str, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    evidence_id = f"evidence_{package.content_chunks[0].chunk_id}"
    roles: dict[str, dict[str, object]] = {
        "concept": {
            "knowledge_bundle_id": "bundle_factory",
            "bundle_version": "v1",
            "published_at": NOW.isoformat(),
            "course_id": package.course_id,
            "course_package_id": package.course_package_id,
            "course_package_checksum": package.checksum,
            "concepts": [
                {
                    "concept_id": "concept_1",
                    "name": "Factory concept",
                    "chapter_id": "chapter_1",
                    "description": "A concept loaded from teacher JSON.",
                    "aliases": [],
                    "status": "published",
                }
            ],
            "concept_evidence_ids": {"concept_1": [evidence_id]},
        },
        "item": {
            "items": [
                {
                    "item_id": "item_1",
                    "version": "v1",
                    "stem": "The factory item is valid.",
                    "item_type": "true_false",
                    "concept_ids": ["concept_1"],
                    "misconception_ids": [],
                    "difficulty_level": 1,
                    "cognitive_level": "remember",
                    "parameter_rules": [],
                    "answer_key": {"answer": True, "max_score": 1.0},
                    "rubric_id": None,
                    "source_evidence_ids": [evidence_id],
                    "status": "teacher_approved",
                }
            ],
            "q_matrix": (
                [
                    {
                        "item_id": "item_1",
                        "item_version": "v1",
                        "concept_id": "concept_1",
                        "weight": 1.0,
                    }
                ]
                if valid
                else []
            ),
        },
        "rubric": {"rubrics": []},
        "blueprint": {
            "blueprints": [
                {
                    "blueprint_id": "blueprint_1",
                    "version": "v1",
                    "course_id": package.course_id,
                    "sections": [
                        {
                            "section_id": "section_1",
                            "name": "Factory section",
                            "item_count": 1,
                            "score": 1.0,
                            "item_types": [],
                            "concept_weights": {"concept_1": 1.0},
                            "difficulty_range": [1, 1],
                            "anchor_item_ids": ["item_1"],
                            "anchor_item_versions": {"item_1": "v1"},
                        }
                    ],
                    "total_score": 1.0,
                    "duration_minutes": 30,
                    "status": "teacher_approved",
                }
            ]
        },
        "prerequisite": {"prerequisite_relations": []},
        "misconception": {"misconception_tags": []},
    }
    paths: dict[str, Path] = {}
    for role, value in roles.items():
        path = tmp_path / f"{role}.json"
        path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        paths[role] = path
    return paths


def test_factory_default_m1_uses_sqlite_s1_s6_repository(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    container = build_application(settings)

    repository = container.m1_service._repository  # noqa: SLF001
    assert isinstance(repository, SQLiteM1M2M3Repository)
    assert repository.is_ready()
    assert repository.database_path == settings.database.sqlite_path


def test_factory_wires_configured_retrieval_policy_to_coordinator(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path).model_copy(
        update={
            "retrieval": RetrievalSettings(
                policy_id="production-vector-v2",
                strategy="vector",
                top_k=5,
                lexical_weight=0.0,
                vector_weight=1.0,
            )
        }
    )

    container = build_application(settings)

    policy = container.coordinator._retrieval_policy  # noqa: SLF001
    assert policy is not None
    assert policy.policy_id == "production-vector-v2"
    assert policy.strategy == "vector"
    assert policy.top_k == 5


def test_factory_default_m2_uses_shared_sqlite_s1_s6_repository(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    container = build_application(settings)

    repository = container.m2_service._repository  # noqa: SLF001
    assert isinstance(repository, SQLiteM1M2M3Repository)
    assert repository is container.m1_service._repository  # noqa: SLF001


def test_factory_default_m3_uses_shared_sqlite_s1_s6_repository(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    container = build_application(settings)

    repository = container.m3_service._repository  # noqa: SLF001
    assert isinstance(repository, SQLiteM1M2M3Repository)
    assert repository is container.m1_service._repository  # noqa: SLF001


def test_sqlite_factory_exposes_one_authoritative_s1_s6_backend(
    tmp_path: Path,
) -> None:
    container = build_application(_settings(tmp_path))

    repository = container.m1_service._repository  # noqa: SLF001
    assert isinstance(repository, SQLiteM1M2M3Repository)
    assert repository is container.m2_service._repository  # noqa: SLF001
    assert repository is container.m3_service._repository  # noqa: SLF001
    assert container.persistence_backend is repository


def test_factory_m3_repository_override_preserves_identity_without_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _RecordingM3Repository()

    def fail_default_repository(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("discarded default M3 repository was constructed")

    monkeypatch.setattr(
        application_factory,
        "FileM3Repository",
        fail_default_repository,
        raising=False,
    )

    container = build_application(
        _settings(tmp_path), repositories=RepositoryOverrides(m3=repository)
    )

    assert container.m3_service._repository is repository  # noqa: SLF001


def test_factory_m3_recording_repository_receives_complete_artifacts_and_restores(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    package = _course_package()
    repository = _RecordingM3Repository()
    paths = _m3_seed_paths(tmp_path / "teacher-seeds", package)
    first = build_application(
        settings, repositories=RepositoryOverrides(m3=repository)
    )

    bundle = first.m3_service.build_knowledge_bundle(
        package,
        paths["concept"],
        paths["item"],
        paths["rubric"],
        paths["blueprint"],
        paths["prerequisite"],
        paths["misconception"],
    )
    first.close()

    assert len(repository.approved) == 1
    _, report, snapshot = repository.approved[0]
    assert report.status == "approved"
    assert report.seed_snapshot_checksum == snapshot.checksum
    restarted = build_application(
        settings, repositories=RepositoryOverrides(m3=repository)
    )
    restored = restarted.m3_service.restore_knowledge_bundle(
        course_package=package,
        knowledge_bundle_id=bundle.knowledge_bundle_id,
        bundle_version=bundle.bundle_version,
    )
    restarted.close()

    assert restored.model_dump(mode="json") == bundle.model_dump(mode="json")


def test_factory_file_m3_restores_real_bundle_for_downstream_interfaces(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    package = _course_package()
    paths = _m3_seed_paths(tmp_path / "teacher-seeds", package)
    first = build_application(settings)
    bundle = first.m3_service.build_knowledge_bundle(
        package,
        paths["concept"],
        paths["item"],
        paths["rubric"],
        paths["blueprint"],
        paths["prerequisite"],
        paths["misconception"],
    )
    first.close()
    shutil.rmtree(paths["concept"].parent)

    restarted = build_application(settings)
    try:
        restarted.m0_service.initialize()
        restored = restarted.m3_service.restore_knowledge_bundle(
            course_package=package,
            knowledge_bundle_id="bundle_factory",
            bundle_version="v1",
        )
        task_plan = restarted.m4_service.create_task_plan(
            student_text="start stage assessment",
            task_type_hint="stage_assessment",
            course_id=package.course_id,
            class_id="class_1",
            learner_id="learner_1",
            session_id="session_1",
            knowledge_bundle=restored,
            learner_state_snapshot=None,
        )
        paper = restarted.m8_service.generate_paper(
            task_plan=task_plan,
            knowledge_bundle=restored,
            learner_state_snapshot=None,
            diagnosis_result=None,
        )
        scoring = ScoringResultBundle(
            attempt_id="attempt_1",
            paper_id=paper.paper_id,
            learner_id="learner_1",
            score_audit_records=[],
            learning_events=[],
            remediation_plan=RemediationPlan(
                plan_id="remediation_1",
                based_on_attempt_id="attempt_1",
                learner_id="learner_1",
                targets=[],
                created_at=NOW,
            ),
            total_score=0.0,
            max_score=0.0,
            finalized_at=NOW,
        )
        state_update = StateUpdateResult(
            diagnosis_result=DiagnosisResult(
                diagnosis_id="diagnosis_1",
                attempt_id="attempt_1",
                learner_id="learner_1",
                item_diagnoses=[
                    ItemDiagnosis(
                        item_instance_id="instance_1",
                        concept_ids=["concept_1"],
                        misconception_ids=[],
                        error_type="none",
                        confidence=1.0,
                        evidence_audit_ids=["audit_1"],
                        prerequisite_gap_ids=[],
                    )
                ],
                priority_concept_ids=["concept_1"],
                priority_misconception_ids=[],
                generated_at=NOW,
            ),
            learner_state_snapshot=LearnerStateSnapshot(
                snapshot_id="learner_snapshot_1",
                course_id=package.course_id,
                class_id="class_1",
                learner_id="learner_1",
                state_version=1,
                concept_states=[
                    ConceptState(
                        concept_id="concept_1",
                        mastery_probability=0.5,
                        mastery_confidence=1.0,
                        misconceptions=[],
                        hint_dependency=0.0,
                        recent_correction_rate=1.0,
                        evidence_count=1,
                        updated_at=NOW,
                    )
                ],
                overall_mastery=0.5,
                evidence_count=1,
                updated_at=NOW,
            ),
            class_state_snapshot=ClassStateSnapshot(
                snapshot_id="class_snapshot_1",
                course_id=package.course_id,
                class_id="class_1",
                aggregation_policy_version="1.0.0",
                scope={"course_id": package.course_id, "class_id": "class_1"},
                class_size=1,
                assessed_count=1,
                coverage_rate=1.0,
                concept_status=[],
                misconception_summary=[],
                evidence_status="sufficient",
                updated_at=NOW,
            ),
            processed_audit_ids=["audit_1"],
            updated_at=NOW,
        )

        assert restored.model_dump(mode="json") == bundle.model_dump(mode="json")
        assert restored.knowledge_bundle_id == "bundle_factory"
        assert restored.bundle_version == "v1"
        assert task_plan.knowledge_bundle_id == restored.knowledge_bundle_id
        assert paper.blueprint_id == task_plan.blueprint_id
        assert restarted.m5_service._state_scope(  # noqa: SLF001
            scoring,
            restored,
            None,
            None,
            authoritative_class_id="class_1",
        ) == (package.course_id, "class_1", "learner_1")
        restarted.m9_service._validate_scope(restored, scoring, state_update)  # noqa: SLF001
    finally:
        restarted.close()


def test_factory_m3_recording_repository_receives_rejected_artifact(
    tmp_path: Path,
) -> None:
    package = _course_package()
    repository = _RecordingM3Repository()
    paths = _m3_seed_paths(tmp_path / "teacher-seeds", package, valid=False)
    service = build_application(
        _settings(tmp_path), repositories=RepositoryOverrides(m3=repository)
    ).m3_service

    with pytest.raises(DomainError) as captured:
        service.build_knowledge_bundle(
            package,
            paths["concept"],
            paths["item"],
            paths["rubric"],
            paths["blueprint"],
            paths["prerequisite"],
            paths["misconception"],
        )

    assert captured.value.code == "Q_MATRIX_CONFLICT"
    assert len(repository.rejected) == 1
    report, snapshot = repository.rejected[0]
    assert report.report_id == captured.value.details["report_id"]
    assert report.seed_snapshot_checksum == snapshot.checksum


@pytest.mark.parametrize("failure", ["save", "load"])
def test_factory_m3_repository_failure_maps_through_service_boundary(
    tmp_path: Path,
    failure: str,
) -> None:
    package = _course_package()
    paths = _m3_seed_paths(tmp_path / "teacher-seeds", package)
    service = build_application(
        _settings(tmp_path),
        repositories=RepositoryOverrides(
            m3=_RecordingM3Repository(
                fail_save=failure == "save",
                fail_load=failure == "load",
            )
        ),
    ).m3_service

    with pytest.raises(DomainError) as captured:
        service.build_knowledge_bundle(
            package,
            paths["concept"],
            paths["item"],
            paths["rubric"],
            paths["blueprint"],
            paths["prerequisite"],
            paths["misconception"],
        )

    assert captured.value.code == "KNOWLEDGE_ARTIFACT_INVALID"
    assert captured.value.module == "m3"
    assert captured.value.details == {}


def test_factory_m3_service_override_does_not_construct_discarded_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement = M3KnowledgeBundleServiceStub()

    def fail_default_repository(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("discarded default M3 repository was constructed")

    def fail_service_constructor(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("discarded M3 validator dependency was constructed")

    monkeypatch.setattr(
        application_factory,
        "FileM3Repository",
        fail_default_repository,
        raising=False,
    )
    monkeypatch.setattr(
        application_factory,
        "M3KnowledgeBundleService",
        fail_service_constructor,
    )

    container = build_application(
        _settings(tmp_path), services=ServiceOverrides(m3=replacement)
    )

    assert container.m3_service is replacement
    assert container.coordinator._m3 is replacement  # noqa: SLF001


def test_factory_m2_repository_override_preserves_identity_without_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _RecordingM2Repository()

    def fail_default_repository(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("discarded default M2 repository was constructed")

    monkeypatch.setattr(
        application_factory,
        "FileM2Repository",
        fail_default_repository,
    )

    container = build_application(
        _settings(tmp_path), repositories=RepositoryOverrides(m2=repository)
    )

    assert container.m2_service._repository is repository  # noqa: SLF001


def test_factory_m2_repository_override_persists_complete_artifact_for_fresh_service(
    tmp_path: Path,
) -> None:
    repository = _RecordingM2Repository()
    settings = _settings(tmp_path)
    first = build_application(
        settings, repositories=RepositoryOverrides(m2=repository)
    )
    package = _course_package()

    ref = first.m2_service.build_index(package)
    assert repository.save_calls == [(ref.index_id, ref.index_version)]
    stored_ref, stored_payloads = repository.artifacts[(ref.index_id, ref.index_version)]
    assert stored_ref.course_package_id == package.course_package_id
    assert stored_ref.course_package_checksum == package.checksum
    assert snapshot_from_payloads(stored_payloads).course_package_checksum == package.checksum

    restarted = build_application(
        settings, repositories=RepositoryOverrides(m2=repository)
    )
    restored = restarted.m2_service.restore_index(
        course_package=package, evidence_index_ref=ref
    )
    evidence = restarted.m2_service.retrieve(
        EvidenceQuery(
            query_id="factory_m2_recording_query",
            course_package_id=package.course_package_id,
            course_package_checksum=package.checksum,
            query_text="governed rule",
            concept_ids=["concept_1"],
            item_id=None,
            use_case="qa",
            top_k=1,
            min_relevance=0.1,
        ),
        restored,
    )

    assert repository.load_calls == [(ref.index_id, ref.index_version)]
    assert [row.chunk_id for row in evidence.evidence_chunks] == ["chunk_1"]


@pytest.mark.parametrize("failure", ["save", "load"])
def test_factory_m2_repository_failure_maps_through_service_boundary(
    tmp_path: Path, failure: str
) -> None:
    package = _course_package()
    repository = _RecordingM2Repository(
        fail_save=failure == "save", fail_load=failure == "load"
    )
    service = build_application(
        _settings(tmp_path), repositories=RepositoryOverrides(m2=repository)
    ).m2_service

    if failure == "save":
        with pytest.raises(DomainError) as captured:
            service.build_index(package)
    else:
        source = _RecordingM2Repository()
        ref = M2EvidenceRetrievalService(Path("indexes"), "lexical", source).build_index(
            package
        )
        repository.artifacts.update(source.artifacts)
        with pytest.raises(DomainError) as captured:
            service.restore_index(course_package=package, evidence_index_ref=ref)

    assert captured.value.code == "INDEX_ARTIFACT_INVALID"
    assert captured.value.details == {}


def test_factory_m2_service_override_does_not_construct_file_or_tokenizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement = M2EvidenceRetrievalServiceStub()

    def fail_default_repository(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("discarded default M2 repository was constructed")

    real_dependency = application_factory._DeterministicDependency

    def selective_dependency(name: str) -> object:
        if name == "lexical":
            raise AssertionError("discarded default M2 tokenizer was constructed")
        return real_dependency(name)

    monkeypatch.setattr(application_factory, "FileM2Repository", fail_default_repository)
    monkeypatch.setattr(application_factory, "_DeterministicDependency", selective_dependency)

    container = build_application(
        _settings(tmp_path), services=ServiceOverrides(m2=replacement)
    )

    assert container.m2_service is replacement


def test_factory_m2_file_artifact_restores_and_retrieves_after_restart(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    package = _course_package()
    first = build_application(settings)
    ref = first.m2_service.build_index(package)
    first.close()

    restarted = build_application(settings)
    restored = restarted.m2_service.restore_index(
        course_package=package, evidence_index_ref=ref
    )
    evidence = restarted.m2_service.retrieve(
        EvidenceQuery(
            query_id="factory_m2_file_query",
            course_package_id=package.course_package_id,
            course_package_checksum=package.checksum,
            query_text="governed rule",
            concept_ids=["concept_1"],
            item_id=None,
            use_case="qa",
            top_k=1,
            min_relevance=0.1,
        ),
        restored,
    )
    restarted.close()

    assert [row.chunk_id for row in evidence.evidence_chunks] == ["chunk_1"]


def test_factory_default_m1_registers_exact_supported_byte_parsers(
    tmp_path: Path,
) -> None:
    container = build_application(_settings(tmp_path))

    assert set(container.m1_service._parser_registry) == {  # noqa: SLF001
        ".md",
        ".txt",
        ".pdf",
        ".docx",
        ".pptx",
    }
    assert all(
        parser is parse_source
        for parser in container.m1_service._parser_registry.values()  # noqa: SLF001
    )


def test_factory_default_m1_persists_authorized_txt_import_for_fresh_container(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = tmp_path / "lesson.txt"
    source_bytes = b"Governed text"
    source.write_bytes(source_bytes)
    metadata = tmp_path / "course.json"
    metadata.write_text(
        json.dumps(
            {
                "course_package_id": "package-factory",
                "course_id": "course-factory",
                "package_version": "v1",
                "course_name": "Factory Course",
                "imported_at": "2026-08-03T00:00:00+00:00",
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    authorization = tmp_path / "authorization.csv"
    authorization.write_text(
        "file_name,source_id,expected_sha256,authorized_by,authorized_at,license_note\n"
        f"lesson.txt,source-factory,{hashlib.sha256(source_bytes).hexdigest()},Teacher,2026-08-01T00:00:00+00:00,course-use\n",
        encoding="utf-8",
    )

    first = build_application(settings)
    package = first.m1_service.import_course(
        [source], metadata, authorization, tmp_path / "output"
    )
    first.close()
    restarted = build_application(settings)
    restored = restarted.m1_service._repository.get_course_package(  # noqa: SLF001
        "package-factory", "v1"
    )
    restarted.close()

    assert restored == package


def test_factory_m1_repository_override_preserves_identity_without_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _RepositorySentinel()

    def fail_default_repository(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("discarded default M1 repository was constructed")

    monkeypatch.setattr(
        application_factory,
        "FileM1Repository",
        fail_default_repository,
    )

    container = build_application(
        _settings(tmp_path), repositories=RepositoryOverrides(m1=repository)
    )

    assert container.m1_service._repository is repository  # noqa: SLF001


def test_factory_m1_service_override_preserves_identity_without_default_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement = M1CourseGovernanceServiceStub()

    def fail_default_repository(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("discarded default M1 repository was constructed")

    monkeypatch.setattr(
        application_factory,
        "FileM1Repository",
        fail_default_repository,
    )

    container = build_application(
        _settings(tmp_path), services=ServiceOverrides(m1=replacement)
    )

    assert container.m1_service is replacement
    assert container.coordinator._m1 is replacement  # noqa: SLF001


def test_m1_stub_accepts_markdown_and_text_byte_parsers(tmp_path: Path) -> None:
    stub = M1CourseGovernanceServiceStub()

    markdown = stub._parse(tmp_path / "lesson.md", b"# Lesson")  # noqa: SLF001
    text = stub._parse(tmp_path / "lesson.txt", b"Lesson")  # noqa: SLF001

    assert markdown.media_type == "text/markdown"
    assert text.media_type == "text/plain"


def test_m1_stub_import_uses_complete_snapshot_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = M1CourseGovernanceServiceStub()
    source = tmp_path / "lesson.txt"
    source_bytes = b"Governed text"
    source.write_bytes(source_bytes)
    metadata = tmp_path / "course.json"
    metadata.write_text(
        json.dumps(
            {
                "course_package_id": "package-stub",
                "course_id": "course-stub",
                "package_version": "v1",
                "course_name": "Stub Course",
                "imported_at": "2026-08-03T00:00:00+00:00",
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    authorization = tmp_path / "authorization.csv"
    authorization.write_text(
        "file_name,source_id,expected_sha256,authorized_by,authorized_at,license_note\n"
        f"lesson.txt,source-stub,{hashlib.sha256(source_bytes).hexdigest()},Teacher,2026-08-01T00:00:00+00:00,course-use\n",
        encoding="utf-8",
    )
    repository = stub._repository  # noqa: SLF001

    def reject_incomplete_save(package: CoursePackage) -> None:
        del package
        raise AssertionError("stub must use save_course_import")

    monkeypatch.setattr(repository, "save_course_package", reject_incomplete_save)

    package = stub.import_course(
        [source], metadata, authorization, tmp_path / "output"
    )
    stored = repository.get_course_package("package-stub", "v1")
    snapshot = repository.snapshots[("package-stub", "v1")]

    assert stored == package
    assert stored is not package
    assert snapshot.source_payloads[0].raw_bytes == source_bytes


class _FalseyRepositorySentinel:
    def __bool__(self) -> bool:
        return False


class _PolicyConfigurationRepository(_RepositorySentinel):
    def __init__(
        self,
        manifest: PolicyArtifactManifest | None,
        evaluation: PolicyEvaluationRecord | None,
    ) -> None:
        self.manifest = manifest
        self.evaluation = evaluation
        self.manifest_reads = 0
        self.evaluation_reads = 0

    def get_policy_artifact(
        self,
        policy_id: str,
    ) -> PolicyArtifactManifest | None:
        self.manifest_reads += 1
        assert policy_id == "policy-1"
        return self.manifest

    def get_policy_evaluation(
        self,
        policy_id: str,
        dataset_identity: str,
    ) -> PolicyEvaluationRecord | None:
        self.evaluation_reads += 1
        assert (policy_id, dataset_identity) == ("policy-1", "dataset-1")
        return self.evaluation


def test_factory_rules_mode_never_reads_policy_repository_or_artifacts(
    tmp_path: Path,
) -> None:
    repository = _PolicyConfigurationRepository(None, None)

    container = build_application(
        _settings(tmp_path),
        repositories=RepositoryOverrides(m6=repository),
    )

    assert container.m6_service._policy_runtime.mode == "rules"  # noqa: SLF001
    assert (  # noqa: SLF001
        container.m6_service._policy_runtime._execution_loader is not None
    )
    assert repository.manifest_reads == 0
    assert repository.evaluation_reads == 0


def test_factory_constructs_real_active_linucb_runtime_from_exact_records(
    tmp_path: Path,
) -> None:
    dimension = 23
    identity = [
        [1.0 if row == column else 0.0 for column in range(dimension)]
        for row in range(dimension)
    ]
    action_ids = (
        "m6.transition.s0_to_s1.v1",
        "m6.transition.s1_to_s2.v1",
        "m6.transition.s1_to_s3.v1",
        "m6.transition.s2_to_s3.v1",
        "m6.transition.s3_to_s4.v1",
        "m6.transition.s4_to_s2.v1",
        "m6.transition.s4_to_s3.v1",
        "m6.transition.s4_to_s5.v1",
    )
    artifact = {
        "policy_id": "policy-1",
        "adapter_id": "m6-linucb-adapter",
        "adapter_version": "v1",
        "feature_schema_version": "m6-features-v1",
        "action_space_version": "m6-action-space-v1",
        "dimension": dimension,
        "alpha": 0.1,
        "actions": {
            action_id: {
                "theta": (
                    [0.0, 1.0, *([0.0] * (dimension - 2))]
                    if action_id == "m6.transition.s1_to_s2.v1"
                    else [0.0] * dimension
                ),
                "inverse_covariance": identity,
            }
            for action_id in action_ids
        },
    }
    artifact_text = json.dumps(
        artifact,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    policy_root = tmp_path / "runtime" / "policies"
    policy_root.mkdir(parents=True)
    (policy_root / "model.json").write_text(artifact_text, encoding="utf-8")
    manifest = PolicyArtifactManifest(
        policy_id="policy-1",
        adapter_id="m6-linucb-adapter",
        adapter_version="v1",
        algorithm="linucb",
        state_graph_version="m6-state-graph-v1",
        baseline_policy_version="m6-rules-v1",
        feature_schema_version="m6-features-v1",
        action_space_version="m6-action-space-v1",
        reward_version="m6-reward-v1",
        gate_policy_version="m6-active-gate-v1",
        training_data_watermark="2026-07-27T00:00:00Z",
        training_data_checksum="b" * 64,
        artifact_sha256=hashlib.sha256(artifact_text.encode()).hexdigest(),
        status="approved",
        created_at="2026-07-27T01:00:00Z",
        artifact_reference="model.json",
        allowed_scopes=("course:course-1", "class:class-1"),
    )
    evaluation = PolicyEvaluationRecord(
        policy_id="policy-1",
        dataset_identity="dataset-1",
        status="sufficient_data",
        approved=True,
        effective_sample_size=20,
        action_coverage=1.0,
        observation_count=20,
        metrics={"ips": 0.5, "snips": 0.5, "dm": 0.5, "dr": 0.5},
        confidence_intervals={
            "ips": (0.4, 0.6),
            "snips": (0.4, 0.6),
            "dm": (0.4, 0.6),
            "dr": (0.4, 0.6),
        },
    )
    repository = _PolicyConfigurationRepository(manifest, evaluation)
    settings = _settings(
        tmp_path,
        m6_policy=M6PolicySettings(
            mode="active",
            policy_id="policy-1",
            evaluation_dataset_identity="dataset-1",
            rollout_percentage=1.0,
            exploration_rate=0.01,
            maximum_exploration_rate=0.05,
            allowed_course_ids=("course-1",),
            allowed_class_ids=("class-1",),
            minimum_support=10,
            maximum_uncertainty=100.0,
            runtime_directory=policy_root,
        ),
    )

    container = build_application(
        settings,
        repositories=RepositoryOverrides(m6=repository),
    )

    runtime = container.m6_service._policy_runtime  # noqa: SLF001
    assert runtime.mode == "active"
    assert runtime._learned_adapter.__class__.__name__ == "LinUCBPolicyAdapter"  # noqa: SLF001
    assert repository.manifest_reads == 1
    assert repository.evaluation_reads == 1

    context = TutoringPolicyContext(
        request_fingerprint="a" * 64,
        current_state="S1",
        task_type="practice",
        turn_count=1,
        score_ratio=0.5,
        target_concept_count=1,
        signals=DecisionSignals(
            needs_teacher_review=False,
            has_diagnosed_misconception=False,
            has_active_misconception=False,
            has_prerequisite_gap=False,
            has_new_evidence=True,
            minimum_recent_correction_rate=0.9,
            minimum_mastery_confidence=0.8,
            maximum_hint_dependency=0.0,
        ),
        learner_evidence_count=1,
        course_id="course-1",
        class_id="class-1",
    )
    candidates = (
        CandidateAction(
            candidate_id="m6.transition.s1_to_s3.v1",
            next_state="S3",
            action_type="guided_question",
            prompt_template_id="m6.s3.guided_question.v1",
            exploration_allowed=True,
        ),
        CandidateAction(
            candidate_id="m6.transition.s1_to_s2.v1",
            next_state="S2",
            action_type="minimal_hint",
            prompt_template_id="m6.s2.minimal_hint.v1",
            exploration_allowed=True,
        ),
    )
    execution = runtime.prepare_execution(
        context,
        candidates,
        input_fingerprint="1" * 64,
    )
    rules_container = build_application(
        _settings(
            tmp_path,
            m6_policy=M6PolicySettings(
                mode="rules",
                runtime_directory=policy_root,
            ),
        ),
        repositories=RepositoryOverrides(m6=repository),
    )
    recovered = rules_container.m6_service._policy_runtime.select(  # noqa: SLF001
        execution,
        context,
        candidates,
        created_at="2026-07-27T02:00:00+00:00",
    )

    assert execution.active_gate_allowed is True
    assert recovered.public_candidate.next_state == "S2"
    assert repository.manifest_reads == 2
    assert repository.evaluation_reads == 1


class _IntentAdapterSentinel:
    adapter_id = "factory-adapter"
    adapter_version = "factory-adapter-v1"

    @staticmethod
    def predict(text: str) -> object:
        del text
        raise AssertionError("factory test does not perform prediction")


def test_rules_factory_never_loads_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sklearn_adapter,
        "load_sklearn_intent_adapter",
        lambda path, **kwargs: pytest.fail(
            f"rules mode loaded model: {path.name}; {kwargs}"
        ),
    )

    app = build_application(_settings(tmp_path))
    try:
        assert app.m4_service is not None
        assert app.m4_service._intent_service is not None  # noqa: SLF001
        assert app.m4_service._intent_service._mode == "rules"  # noqa: SLF001
        assert app.m4_service._intent_service._adapter is None  # noqa: SLF001
    finally:
        app.close()


def test_default_rules_factory_matches_legacy_cross_conflict_refusal(
    tmp_path: Path,
) -> None:
    app = build_application(_settings(tmp_path))
    app.m0_service.initialize()
    request = {
        "student_text": "explain assessment",
        "task_type_hint": None,
        "course_id": "course_1",
        "class_id": "class_1",
        "learner_id": "learner_1",
        "session_id": "session_1",
        "knowledge_bundle": _knowledge_bundle(),
        "learner_state_snapshot": None,
    }
    try:
        with pytest.raises(DomainError) as legacy_error:
            resolve_task_type("explain assessment", None)
        with pytest.raises(DomainError) as factory_error:
            app.m4_service.create_task_plan(**request)
    finally:
        app.close()

    assert legacy_error.value.code == factory_error.value.code == "UNSUPPORTED_TASK"
    assert legacy_error.value.recoverable is factory_error.value.recoverable is True


@pytest.mark.parametrize("mode", ["shadow", "active"])
def test_model_factory_loads_configured_adapter_and_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    model_ref = Path("models/intent-model")
    model_dir = (tmp_path / "runtime" / model_ref).resolve()
    adapter = _IntentAdapterSentinel()
    calls: list[tuple[Path, dict[str, object]]] = []
    monkeypatch.setattr(
        sklearn_adapter,
        "load_sklearn_intent_adapter",
        lambda path, **kwargs: calls.append((path, kwargs)) or adapter,
    )
    settings = _settings(
        tmp_path,
        intent=IntentSettings(
            mode=mode,
            backend="sklearn",
            model_ref=model_ref,
            model_id="factory-intent",
            model_version="2.0.0",
            model_sha256=MODEL_SHA256,
            min_confidence=0.82,
            min_margin=0.23,
            policy_version="factory-policy-v2",
            fallback_to_rules=False,
            fail_closed=False,
        ),
    )

    app = build_application(settings)
    try:
        intent_service = app.m4_service._intent_service  # noqa: SLF001
        assert calls == [
            (
                model_dir,
                {
                    "runtime_dir": (tmp_path / "runtime").resolve(),
                    "expected_model_id": "factory-intent",
                    "expected_model_version": "2.0.0",
                    "expected_model_sha256": MODEL_SHA256,
                },
            )
        ]
        assert intent_service is not None
        assert intent_service.repository is app.m4_service._repository  # noqa: SLF001
        assert intent_service._mode == mode  # noqa: SLF001
        assert intent_service._adapter is adapter  # noqa: SLF001
        assert intent_service._policy.min_confidence == 0.82  # noqa: SLF001
        assert intent_service._policy.min_margin == 0.23  # noqa: SLF001
        assert intent_service._policy.fallback_to_rules is False  # noqa: SLF001
        assert intent_service._policy.fail_closed is False  # noqa: SLF001
        assert intent_service._policy_version == "factory-policy-v2"  # noqa: SLF001
    finally:
        app.close()


def test_model_factory_uses_injected_m4_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _RepositorySentinel()
    adapter = _IntentAdapterSentinel()
    monkeypatch.setattr(
        sklearn_adapter,
        "load_sklearn_intent_adapter",
        lambda path, **kwargs: adapter,
    )
    settings = _settings(
        tmp_path,
        intent=IntentSettings(
            mode="active",
            backend="sklearn",
            model_ref=Path("models/intent-model"),
            model_id="factory-intent",
            model_version="1.0.0",
            model_sha256=MODEL_SHA256,
        ),
    )

    app = build_application(
        settings,
        repositories=RepositoryOverrides(m4=repository),
    )
    try:
        assert app.m4_service._repository is repository  # noqa: SLF001
        assert app.m4_service._intent_service.repository is repository  # noqa: SLF001
    finally:
        app.close()


def test_m4_service_override_avoids_model_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement = _RepositorySentinel()
    monkeypatch.setattr(
        sklearn_adapter,
        "load_sklearn_intent_adapter",
        lambda path, **kwargs: pytest.fail(
            f"discarded service loaded: {path.name}; {kwargs}"
        ),
    )
    settings = _settings(
        tmp_path,
        intent=IntentSettings(
            mode="active",
            backend="sklearn",
            model_ref=Path("models/intent-model"),
            model_id="factory-intent",
            model_version="1.0.0",
            model_sha256=MODEL_SHA256,
        ),
    )

    app = build_application(
        settings,
        services=ServiceOverrides(m4=replacement),  # type: ignore[arg-type]
    )
    try:
        assert app.m4_service is replacement
    finally:
        app.close()


@pytest.mark.parametrize("mode", ["shadow", "active"])
def test_unreadable_model_artifact_fails_closed_without_path_leak(
    tmp_path: Path,
    mode: str,
) -> None:
    private_model_dir = (
        tmp_path / "runtime" / "private-deployment" / "missing-intent-model"
    ).resolve()
    settings = _settings(
        tmp_path,
        intent=IntentSettings(
            mode=mode,
            backend="sklearn",
            model_ref=Path("private-deployment/missing-intent-model"),
            model_id="factory-intent",
            model_version="1.0.0",
            model_sha256=MODEL_SHA256,
        ),
    )

    with pytest.raises(IntentArtifactError) as captured:
        build_application(settings)

    assert str(private_model_dir) not in str(captured.value)


def test_m0_legacy_constructor_and_keyword_repository_share_one_identity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "db.sqlite3"
    runtime_dir = tmp_path / "runtime"
    config_dir = tmp_path / "config"

    legacy = M0PlatformService(database_path, runtime_dir, config_dir)
    injected_repository = _RepositorySentinel()
    injected = M0PlatformService(
        database_path,
        runtime_dir,
        config_dir,
        repository=injected_repository,
    )

    assert isinstance(legacy._repository, SQLiteM0Repository)  # noqa: SLF001
    assert injected._repository is injected_repository  # noqa: SLF001
    assert injected._event_store._repository is injected_repository  # noqa: SLF001


def test_sqlite_factory_uses_all_real_durable_repositories(
    tmp_path: Path,
) -> None:
    container = build_application(_settings(tmp_path))

    assert isinstance(container, ApplicationContainer)
    assert isinstance(container.m0_service._repository, SQLiteM0Repository)  # noqa: SLF001
    assert isinstance(container.m4_service._repository, SQLiteM4Repository)  # noqa: SLF001
    assert isinstance(container.m5_service._repository, SQLiteM5Repository)  # noqa: SLF001
    assert isinstance(container.m6_service._repository, SQLiteM6Repository)  # noqa: SLF001
    assert isinstance(container.m7_service._prompt_repository, SQLiteM7Repository)  # noqa: SLF001
    assert isinstance(container.m8_service._repository, SQLiteM8Repository)  # noqa: SLF001
    assert isinstance(container.m9_service._repository, SQLiteM9Repository)  # noqa: SLF001
    assert isinstance(container.outbox_worker, OutboxWorker)
    assert container.outbox_worker._repository is container.m0_service._repository  # noqa: SLF001
    assert container.outbox_worker._status_path.name == (  # noqa: SLF001
        f"{container.outbox_worker.snapshot().worker_id}.status.json"
    )


def test_factory_backed_sqlite_repository_replays_intent_after_restart(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first_container = build_application(settings)
    first_container.m0_service.initialize()
    candidate = StoredIntentDecision(
        request_key="factory-restart-key",
        resolved_task_type="practice",
        decision_status=IntentStatus.ACCEPTED,
        decision_source="active_model",
        adapter_id="factory-test-adapter",
        adapter_version="adapter-v1",
        policy_version="intent-policy-v1",
        confidence=0.91,
        margin=0.31,
        input_checksum=hashlib.sha256("原始文本".encode("utf-8")).hexdigest(),
        reason_codes=("model_accepted",),
        created_at=NOW,
        _generate_checksum=True,
    )
    first_repository = first_container.m4_service._repository  # noqa: SLF001
    first_repository.insert_or_get_intent_decision(candidate)
    first_container.close()

    restarted_container = build_application(settings)
    restarted_container.m0_service.initialize()
    restarted_repository = restarted_container.m4_service._repository  # noqa: SLF001
    replayed = restarted_repository.get_intent_decision(candidate.request_key)
    restarted_container.close()

    assert replayed == candidate
    assert replayed is not candidate


def test_container_and_coordinator_share_exact_service_instances(
    tmp_path: Path,
) -> None:
    container = build_application(_settings(tmp_path))

    for number in range(10):
        service = getattr(container, f"m{number}_service")
        assert getattr(container.coordinator, f"_m{number}") is service
    assert container.runtime_registry.m0_service is container.m0_service
    assert container.runtime_registry.m2_service is container.m2_service


def test_runtime_registry_receives_m1_m2_m3_repository_overrides(
    tmp_path: Path,
) -> None:
    m1_repository = _RuntimeM1Repository(_course_package())
    m2_repository = _RecordingM2Repository()
    m3_repository = _RecordingM3Repository()

    container = build_application(
        _settings(tmp_path),
        repositories=RepositoryOverrides(
            m1=m1_repository,  # type: ignore[arg-type]
            m2=m2_repository,  # type: ignore[arg-type]
            m3=m3_repository,  # type: ignore[arg-type]
        ),
    )

    assert container.runtime_registry.m1_repository is m1_repository
    assert container.runtime_registry.m2_repository is m2_repository
    assert container.runtime_registry.m3_repository is m3_repository


def _publish_runtime_artifact_fixture(
    tmp_path: Path,
) -> tuple[
    ApplicationContainer,
    RuntimeSnapshotRefs,
    _RuntimeM1Repository,
    _RecordingM2Repository,
    _RecordingM3Repository,
    CoursePackage,
    EvidenceIndexRef,
    KnowledgeBundle,
]:
    package = _course_package()
    m1_repository = _RuntimeM1Repository(package)
    m2_repository = _RecordingM2Repository()
    m3_repository = _RecordingM3Repository()
    container = build_application(
        _settings(tmp_path),
        repositories=RepositoryOverrides(
            m1=m1_repository,  # type: ignore[arg-type]
            m2=m2_repository,  # type: ignore[arg-type]
            m3=m3_repository,  # type: ignore[arg-type]
        ),
    )
    container.m0_service.initialize()
    index = container.m2_service.build_index(package)
    seed_paths = _m3_seed_paths(tmp_path / "teacher-seeds", package)
    bundle = container.m3_service.build_knowledge_bundle(
        package,
        seed_paths["concept"],
        seed_paths["item"],
        seed_paths["rubric"],
        seed_paths["blueprint"],
        seed_paths["prerequisite"],
        seed_paths["misconception"],
    )
    snapshot_dir = container.settings.runtime_dir / "snapshots"
    refs = RuntimeSnapshotRefs(
        course_package_ref=Path("snapshots/course-package.json"),
        evidence_index_ref=Path("snapshots/evidence-index.json"),
        knowledge_bundle_ref=Path("snapshots/knowledge-bundle.json"),
    )
    container.m0_service.save_contract_snapshot(
        package, snapshot_dir / "course-package.json"
    )
    container.m0_service.save_contract_snapshot(
        index, snapshot_dir / "evidence-index.json"
    )
    container.m0_service.save_contract_snapshot(
        bundle, snapshot_dir / "knowledge-bundle.json"
    )
    return (
        container,
        refs,
        m1_repository,
        m2_repository,
        m3_repository,
        package,
        index,
        bundle,
    )


def test_runtime_registry_restores_complete_artifacts_without_rebuilding_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        container,
        refs,
        _,
        _,
        _,
        package,
        index,
        bundle,
    ) = _publish_runtime_artifact_fixture(tmp_path)

    def fail_build(*args: object, **kwargs: object) -> EvidenceIndexRef:
        del args, kwargs
        raise AssertionError("runtime restore must not rebuild M2")

    monkeypatch.setattr(container.m2_service, "build_index", fail_build)

    context = container.runtime_registry.restore("course_1", refs)

    assert context.course_package.model_dump(mode="json") == package.model_dump(
        mode="json"
    )
    assert context.evidence_index_ref.model_dump(mode="json") == index.model_dump(
        mode="json"
    )
    assert context.knowledge_bundle.model_dump(mode="json") == bundle.model_dump(
        mode="json"
    )


def test_runtime_registry_rejects_m3_artifact_checksum_mismatch(
    tmp_path: Path,
) -> None:
    (
        container,
        refs,
        _,
        _,
        m3_repository,
        package,
        _,
        bundle,
    ) = _publish_runtime_artifact_fixture(tmp_path)
    loaded = m3_repository.load_bundle_artifact(
        bundle.knowledge_bundle_id,
        bundle.bundle_version,
    )
    assert loaded is not None
    _, _, seed_snapshot = loaded
    forged_checksum = "f" * 64
    forged_bundle = bundle.model_copy(
        update={"course_package_checksum": forged_checksum},
        deep=True,
    )
    forged_report = create_validation_report(
        course_package_id=package.course_package_id,
        course_package_checksum=forged_checksum,
        seed_snapshot=seed_snapshot,
        issues=(),
        knowledge_bundle_id=forged_bundle.knowledge_bundle_id,
        bundle_version=forged_bundle.bundle_version,
        bundle_checksum=forged_bundle.content_checksum(),
    )
    m3_repository._approved.clear()  # noqa: SLF001
    m3_repository.bundles.clear()
    m3_repository.save_bundle_artifact(
        forged_bundle,
        forged_report,
        seed_snapshot,
    )
    container.m0_service.save_contract_snapshot(
        forged_bundle,
        container.settings.runtime_dir / "snapshots/knowledge-bundle.json",
    )

    with pytest.raises(DomainError) as captured:
        container.runtime_registry.restore("course_1", refs)

    assert captured.value.code == "RUNTIME_SNAPSHOT_INVALID"
    assert captured.value.details == {"reason": "identity_mismatch"}


@pytest.mark.parametrize("artifact", ["m1", "m2", "m3"])
def test_runtime_registry_fails_closed_when_complete_artifact_is_missing(
    tmp_path: Path,
    artifact: str,
) -> None:
    (
        container,
        refs,
        m1_repository,
        m2_repository,
        m3_repository,
        _,
        _,
        _,
    ) = _publish_runtime_artifact_fixture(tmp_path)
    if artifact == "m1":
        m1_repository.package = None
    elif artifact == "m2":
        m2_repository.artifacts.clear()
    else:
        m3_repository._approved.clear()  # noqa: SLF001
        m3_repository.bundles.clear()

    with pytest.raises(DomainError) as captured:
        container.runtime_registry.restore("course_1", refs)

    assert captured.value.code == "RUNTIME_SNAPSHOT_INVALID"
    assert captured.value.details == {"reason": "artifact_unavailable"}


def test_postgresql_factory_builds_all_real_repositories_on_one_pool(
    tmp_path: Path,
) -> None:
    pool = _RepositorySentinel()

    container = build_application(
        _settings(tmp_path, backend="postgresql"),
        postgres_pool=pool,  # type: ignore[arg-type]
    )

    expected_types = {
        "m0": PostgresM0Repository,
        "m4": PostgresM4Repository,
        "m5": PostgresM5Repository,
        "m6": PostgresM6Repository,
        "m7": PostgresM7Repository,
        "m8": PostgresM8Repository,
        "m9": PostgresM9Repository,
    }
    for name, expected_type in expected_types.items():
        repository = getattr(container.m0_service, "_repository", None)
        if name == "m4":
            repository = container.m4_service._repository  # noqa: SLF001
        elif name == "m5":
            repository = container.m5_service._repository  # noqa: SLF001
        elif name == "m6":
            repository = container.m6_service._repository  # noqa: SLF001
        elif name == "m7":
            repository = container.m7_service._prompt_repository  # noqa: SLF001
        elif name == "m8":
            repository = container.m8_service._repository  # noqa: SLF001
        elif name == "m9":
            repository = container.m9_service._repository  # noqa: SLF001
        assert isinstance(repository, expected_type)
        assert repository._pool is pool  # noqa: SLF001
    assert isinstance(
        container.m1_service._repository, PostgresM1M2M3Repository  # noqa: SLF001
    )
    assert container.m1_service._repository is container.m2_service._repository  # noqa: SLF001
    assert container.m2_service._repository is container.m3_service._repository  # noqa: SLF001
    assert container.database_pool is pool
    assert isinstance(container.outbox_worker, OutboxWorker)
    assert (
        container.outbox_worker._repository  # noqa: SLF001
        is container.m0_service._repository  # noqa: SLF001
    )


def test_factory_closes_owned_postgresql_pool_when_assembly_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _OwnedPool:
        closed = False

        def close(self) -> None:
            self.closed = True

    pool = _OwnedPool()

    def fail_service_construction(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("assembly failed")

    monkeypatch.setattr(
        application_factory,
        "create_postgres_pool",
        lambda *args, **kwargs: pool,
    )
    monkeypatch.setattr(
        application_factory,
        "M0PlatformService",
        fail_service_construction,
    )

    with pytest.raises(RuntimeError, match="assembly failed"):
        build_application(_settings(tmp_path, backend="postgresql"))

    assert pool.closed is True


def test_factory_closes_owned_pool_when_repository_construction_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _OwnedPool:
        closed = False

        def close(self) -> None:
            self.closed = True

    pool = _OwnedPool()

    def fail_repository_construction(pool_value: object) -> object:
        assert pool_value is pool
        raise RuntimeError("repository assembly failed")

    monkeypatch.setattr(
        application_factory,
        "create_postgres_pool",
        lambda *args, **kwargs: pool,
    )
    monkeypatch.setattr(
        application_factory,
        "PostgresM0Repository",
        fail_repository_construction,
    )

    with pytest.raises(RuntimeError, match="repository assembly failed"):
        build_application(_settings(tmp_path, backend="postgresql"))

    assert pool.closed is True


def test_postgresql_accepts_complete_repository_overrides_by_identity(
    tmp_path: Path,
) -> None:
    repositories = {
        name: _RepositorySentinel()
        for name in ("m0", "m4", "m5", "m6", "m7", "m8", "m9")
    }
    overrides = RepositoryOverrides(**repositories)

    container = build_application(
        _settings(tmp_path, backend="postgresql"),
        repositories=overrides,
    )

    assert container.m0_service._repository is repositories["m0"]  # noqa: SLF001
    assert container.m4_service._repository is repositories["m4"]  # noqa: SLF001
    assert container.m5_service._repository is repositories["m5"]  # noqa: SLF001
    assert container.m6_service._repository is repositories["m6"]  # noqa: SLF001
    assert container.m7_service._prompt_repository is repositories["m7"]  # noqa: SLF001
    assert container.m8_service._repository is repositories["m8"]  # noqa: SLF001
    assert container.m9_service._repository is repositories["m9"]  # noqa: SLF001
    assert container.outbox_worker is None
    assert container.database_pool is None


def test_postgresql_complete_outbox_override_still_builds_worker(
    tmp_path: Path,
) -> None:
    m0_repository = SQLiteM0Repository(
        tmp_path / "runtime" / "override.sqlite3"
    )
    repositories = {
        "m0": m0_repository,
        **{
            name: _RepositorySentinel()
            for name in ("m4", "m5", "m6", "m7", "m8", "m9")
        },
    }

    container = build_application(
        _settings(tmp_path, backend="postgresql"),
        repositories=RepositoryOverrides(**repositories),
    )

    assert isinstance(container.outbox_worker, OutboxWorker)
    assert container.outbox_worker._repository is m0_repository  # noqa: SLF001


def test_service_and_worker_overrides_preserve_identity(tmp_path: Path) -> None:
    replacement_m2 = M2EvidenceRetrievalServiceStub(tmp_path / "replacement-index")
    worker = SimpleNamespace(run=lambda *, once=False: 0)

    container = build_application(
        _settings(tmp_path),
        services=ServiceOverrides(m2=replacement_m2),
        outbox_worker=worker,
    )

    assert container.m2_service is replacement_m2
    assert container.coordinator._m2 is replacement_m2  # noqa: SLF001
    assert container.runtime_registry.m2_service is replacement_m2
    assert container.outbox_worker is worker


def test_falsey_repository_override_still_preserves_identity(
    tmp_path: Path,
) -> None:
    repository = _FalseyRepositorySentinel()

    container = build_application(
        _settings(tmp_path),
        repositories=RepositoryOverrides(m4=repository),
    )

    assert container.m4_service._repository is repository  # noqa: SLF001


def test_service_override_does_not_construct_discarded_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement = _RepositorySentinel()
    overrides = ServiceOverrides(
        **{f"m{number}": replacement for number in range(10)}
    )

    def fail_if_constructed(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("discarded default M0 was constructed")

    monkeypatch.setattr(
        application_factory,
        "M0PlatformService",
        fail_if_constructed,
    )

    container = build_application(
        _settings(tmp_path, backend="postgresql"),
        services=overrides,
    )

    for number in range(10):
        assert getattr(container, f"m{number}_service") is replacement
        assert getattr(container.coordinator, f"_m{number}") is replacement


def test_runtime_registry_restores_complete_artifacts_and_retrieves_evidence(
    tmp_path: Path,
) -> None:
    container = build_application(_settings(tmp_path))
    container.m0_service.initialize()
    refs = _snapshot_refs(container)

    context = container.runtime_registry.restore("course_1", refs)
    evidence = container.m2_service.retrieve(
        EvidenceQuery(
            query_id="query_1",
            course_package_id="package_1",
            query_text="governed rule",
            concept_ids=["concept_1"],
            item_id=None,
            use_case="qa",
            top_k=1,
            min_relevance=0.1,
        ),
        context.evidence_index_ref,
    )

    assert context.course_package.course_id == "course_1"
    assert context.knowledge_bundle.course_id == "course_1"
    assert evidence.course_id == "course_1"
    assert [item.chunk_id for item in evidence.evidence_chunks] == [
        context.course_package.content_chunks[0].chunk_id
    ]
    assert container.runtime_registry.require("course_1") == context


def test_runtime_registry_restores_loaded_m2_artifact_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = build_application(_settings(tmp_path))
    container.m0_service.initialize()
    refs = _snapshot_refs(container)

    def fail_build(*args: object, **kwargs: object) -> EvidenceIndexRef:
        del args, kwargs
        raise AssertionError("runtime restore must not rebuild M2")

    monkeypatch.setattr(container.m2_service, "build_index", fail_build)

    first = container.runtime_registry.restore("course_1", refs)
    second = container.runtime_registry.restore("course_1", refs)
    artifact = container.m2_service._repository.load_index_artifact(  # noqa: SLF001
        first.evidence_index_ref.index_id,
        first.evidence_index_ref.index_version,
    )

    assert artifact is not None
    assert artifact[0] == first.evidence_index_ref == second.evidence_index_ref


@pytest.mark.parametrize(
    ("index_update", "reason"),
    [
        ({"checksum": "wrong-checksum"}, "artifact_binding_mismatch"),
        ({"course_package_id": "package_other"}, "artifact_binding_mismatch"),
    ],
)
def test_runtime_registry_rejects_index_identity_checksum_and_status_mismatch(
    tmp_path: Path,
    index_update: dict[str, object],
    reason: str,
) -> None:
    container = build_application(_settings(tmp_path))
    container.m0_service.initialize()
    refs = _snapshot_refs(container, index_update=index_update)

    with pytest.raises(DomainError) as captured:
        container.runtime_registry.restore("course_1", refs)

    assert captured.value.code == "RUNTIME_SNAPSHOT_INVALID"
    assert captured.value.details == {"reason": reason}
    assert str(tmp_path) not in str(captured.value)


def test_runtime_snapshot_status_is_not_authoritative(
    tmp_path: Path,
) -> None:
    container = build_application(_settings(tmp_path))
    container.m0_service.initialize()
    refs = _snapshot_refs(container, index_update={"status": "failed"})

    context = container.runtime_registry.restore("course_1", refs)

    assert context.evidence_index_ref.status == "ready"


def test_failed_runtime_preflight_does_not_pollute_live_m2_index(
    tmp_path: Path,
) -> None:
    container = build_application(_settings(tmp_path))
    container.m0_service.initialize()
    refs = _snapshot_refs(
        container,
        index_update={"checksum": "wrong-checksum"},
    )
    correct_index = M2EvidenceRetrievalServiceStub().build_index(
        _course_package()
    )
    query = EvidenceQuery(
        query_id="query_after_failed_restore",
        course_package_id="package_1",
        query_text="governed rule",
        concept_ids=["concept_1"],
        item_id=None,
        use_case="qa",
        top_k=1,
        min_relevance=0.1,
    )

    with pytest.raises(DomainError):
        container.runtime_registry.restore("course_1", refs)
    with pytest.raises(DomainError) as captured:
        container.m2_service.retrieve(query, correct_index)

    assert captured.value.code == "INDEX_NOT_READY"


def test_failed_m2_repository_load_does_not_pollute_live_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = build_application(_settings(tmp_path))
    container.m0_service.initialize()
    refs = _snapshot_refs(container)
    repository = container.m2_service._repository  # noqa: SLF001

    def fail_load(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OSError("repository unavailable")

    monkeypatch.setattr(repository, "load_index_artifact", fail_load)
    correct_index = M2EvidenceRetrievalServiceStub().build_index(
        _course_package()
    )
    query = EvidenceQuery(
        query_id="query_after_failed_repository_save",
        course_package_id="package_1",
        query_text="governed rule",
        concept_ids=["concept_1"],
        item_id=None,
        use_case="qa",
        top_k=1,
        min_relevance=0.1,
    )

    with pytest.raises(DomainError) as restore_error:
        container.runtime_registry.restore("course_1", refs)
    with pytest.raises(DomainError) as retrieval_error:
        container.m2_service.retrieve(query, correct_index)

    assert restore_error.value.code == "RUNTIME_SNAPSHOT_INVALID"
    assert restore_error.value.details == {"reason": "artifact_invalid"}
    assert retrieval_error.value.code == "INDEX_NOT_READY"


@pytest.mark.parametrize(
    "unsafe_ref",
    [
        Path("../outside.json"),
        Path("C:/private/course.json"),
    ],
)
def test_runtime_registry_rejects_path_escape_without_leaking_path(
    tmp_path: Path,
    unsafe_ref: Path,
) -> None:
    container = build_application(_settings(tmp_path))
    refs = RuntimeSnapshotRefs(
        course_package_ref=unsafe_ref,
        evidence_index_ref=Path("snapshots/evidence-index.json"),
        knowledge_bundle_ref=Path("snapshots/knowledge-bundle.json"),
    )

    with pytest.raises(DomainError) as captured:
        container.runtime_registry.restore("course_1", refs)

    assert captured.value.code == "RUNTIME_SNAPSHOT_INVALID"
    assert captured.value.details == {"reason": "unsafe_reference"}
    assert str(tmp_path) not in str(captured.value)


def test_runtime_registry_missing_snapshot_and_context_fail_closed(
    tmp_path: Path,
) -> None:
    container = build_application(_settings(tmp_path))
    refs = RuntimeSnapshotRefs(
        course_package_ref=Path("snapshots/missing-package.json"),
        evidence_index_ref=Path("snapshots/missing-index.json"),
        knowledge_bundle_ref=Path("snapshots/missing-bundle.json"),
    )

    with pytest.raises(DomainError) as missing_snapshot:
        container.runtime_registry.restore("course_1", refs)
    with pytest.raises(DomainError) as missing_context:
        container.runtime_registry.require("course_1")

    assert missing_snapshot.value.code == "RUNTIME_SNAPSHOT_INVALID"
    assert missing_snapshot.value.details == {"reason": "snapshot_unavailable"}
    assert missing_context.value.code == "RUNTIME_CONTEXT_UNAVAILABLE"


def test_cli_init_loads_configuration_and_uses_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".env").write_text(
        "COURSE_INSIGHT_ENVIRONMENT=production\n",
        encoding="utf-8",
    )
    runtime_dir = project_root / "runtime" / "cli"
    settings = _settings(project_root)
    calls: dict[str, object] = {}

    class _M0:
        def initialize(self) -> None:
            calls["initialized"] = True

        @staticmethod
        def health_check() -> dict[str, str]:
            return {"config": "ok", "database": "ok", "runtime": "ok"}

    def fake_loader(**kwargs: object) -> PlatformSettings:
        calls["loader"] = kwargs
        return settings

    def fake_factory(value: PlatformSettings) -> object:
        calls["settings"] = value
        return SimpleNamespace(m0_service=_M0())

    monkeypatch.setattr(cli, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(cli, "load_platform_settings", fake_loader)
    monkeypatch.setattr(cli, "build_application", fake_factory)

    exit_code = cli.main(["init", "--runtime-dir", str(runtime_dir)])

    assert exit_code == 0
    assert calls["settings"] is settings
    assert calls["initialized"] is True
    assert calls["loader"]["dotenv_path"] is None
    assert json.loads(capsys.readouterr().out) == {
        "config": "ok",
        "database": "ok",
        "runtime": "ok",
    }


def test_cli_explicit_dotenv_flag_is_passed_to_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    dotenv_path = project_root / ".env"
    dotenv_path.write_text("COURSE_INSIGHT_ENVIRONMENT=test\n", encoding="utf-8")
    calls: dict[str, object] = {}

    class _M0:
        @staticmethod
        def initialize() -> None:
            return None

        @staticmethod
        def health_check() -> dict[str, str]:
            return {"config": "ok", "database": "ok", "runtime": "ok"}

    def fake_loader(**kwargs: object) -> PlatformSettings:
        calls.update(kwargs)
        return _settings(project_root)

    monkeypatch.setattr(cli, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(cli, "load_platform_settings", fake_loader)
    monkeypatch.setattr(
        cli,
        "build_application",
        lambda _: SimpleNamespace(m0_service=_M0()),
    )

    exit_code = cli.main(["init", "--dotenv", str(dotenv_path)])

    assert exit_code == 0
    assert calls["dotenv_path"] == dotenv_path


def test_cli_startup_failure_is_safe_and_does_not_echo_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root = tmp_path / "private-project"
    project_root.mkdir()

    class _FailingM0:
        @staticmethod
        def initialize() -> None:
            raise OSError(str(project_root / "secret.sqlite3"))

    monkeypatch.setattr(cli, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(cli, "load_platform_settings", lambda **_: _settings(project_root))
    monkeypatch.setattr(
        cli,
        "build_application",
        lambda _: SimpleNamespace(m0_service=_FailingM0()),
    )

    exit_code = cli.main(["init"])
    error_text = capsys.readouterr().err

    assert exit_code == 1
    assert "command failed safely: OSError" in error_text
    assert str(project_root) not in error_text
