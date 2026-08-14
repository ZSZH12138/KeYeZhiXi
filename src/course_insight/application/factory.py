"""Single application composition root for CLI, Web, and workers."""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Protocol, TypeVar, cast

from course_insight.application.coordinator import AppCoordinator
from course_insight.application.runtime_context import CourseRuntimeRegistry
from course_insight.contracts.course import CoursePackage
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceIndexRef
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.intelligence import RetrievalPolicy
from course_insight.infrastructure.config import PlatformSettings
from course_insight.infrastructure.m1_file_repository import FileM1Repository
from course_insight.infrastructure.m2_file_repository import FileM2Repository
from course_insight.infrastructure.m3_file_repository import FileM3Repository
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
from course_insight.infrastructure.postgresql.pool import (
    PostgresPool,
    create_postgres_pool,
)
from course_insight.infrastructure.sqlite.m0_repository import SQLiteM0Repository
from course_insight.infrastructure.sqlite.m4_repository import SQLiteM4Repository
from course_insight.infrastructure.sqlite.m5_repository import SQLiteM5Repository
from course_insight.infrastructure.sqlite.m6_repository import SQLiteM6Repository
from course_insight.infrastructure.sqlite.m7_repository import SQLiteM7Repository
from course_insight.infrastructure.sqlite.m8_repository import SQLiteM8Repository
from course_insight.infrastructure.sqlite.m9_repository import SQLiteM9Repository
from course_insight.modules.m0_platform.repository import M0Repository
from course_insight.modules.m0_platform.service import M0PlatformService
from course_insight.modules.m0_platform.outbox import IdempotentJsonlSink
from course_insight.modules.m0_platform.outbox_worker import OutboxWorker
from course_insight.modules.m1_course_governance.repository import M1Repository
from course_insight.modules.m1_course_governance.parsers import parse_source
from course_insight.modules.m1_course_governance.service import (
    M1CourseGovernanceService,
)
from course_insight.modules.m2_evidence_retrieval.repository import M2Repository
from course_insight.modules.m2_evidence_retrieval.lexical import (
    LexicalIndexSnapshot,
)
from course_insight.modules.m2_evidence_retrieval.service import (
    M2EvidenceRetrievalService,
)
from course_insight.modules.m2_evidence_retrieval.audit import (
    RepositoryRetrievalAuditStore,
)
from course_insight.modules.m2_evidence_retrieval.embedding import (
    EmbeddingModelIdentity,
    OpenAICompatibleEmbeddingProvider,
)
from course_insight.infrastructure.postgresql.m1_m2_m3_repository import (
    PostgresPgVectorStore,
)
from course_insight.modules.m3_knowledge_bundle.repository import M3Repository
from course_insight.modules.m3_knowledge_bundle.service import (
    M3KnowledgeBundleService,
)
from course_insight.modules.m3_knowledge_bundle.teacher_review import (
    RepositoryTeacherReviewRepository,
    TeacherReviewWorkflow,
)
from course_insight.modules.m4_task_orchestration import sklearn_adapter
from course_insight.modules.m4_task_orchestration.identity import (
    canonical_idempotency_key,
)
from course_insight.modules.m4_task_orchestration.intent import IntentAdapter
from course_insight.modules.m4_task_orchestration.intent_policy import (
    IntentPolicy,
)
from course_insight.modules.m4_task_orchestration.intent_service import (
    M4IntentService,
)
from course_insight.modules.m4_task_orchestration.repository import M4Repository
from course_insight.modules.m4_task_orchestration.service import (
    M4TaskOrchestrationService,
)
from course_insight.modules.m5_learner_class_state.aggregation import (
    DeterministicClassAggregationPolicy,
)
from course_insight.modules.m5_learner_class_state.repository import M5Repository
from course_insight.modules.m5_learner_class_state.service import M5StateService
from course_insight.modules.m5_learner_class_state.update_policy import (
    DeterministicStateUpdatePolicy,
)
from course_insight.modules.m6_tutoring_fsm.repository import M6Repository
from course_insight.modules.m6_tutoring_fsm.linucb import LinUCBPolicyAdapter
from course_insight.modules.m6_tutoring_fsm.policy_artifacts import (
    LoadedPolicyArtifact,
    load_policy_artifact_for_manifest,
)
from course_insight.modules.m6_tutoring_fsm.policy_gate import (
    ActivePolicyGate,
    PolicyGateConfig,
)
from course_insight.modules.m6_tutoring_fsm.policy_runtime import (
    PolicyRuntime,
    PolicyRuntimeGateInputs,
)
from course_insight.modules.m6_tutoring_fsm.policy_types import (
    EVALUATION_METRICS,
    PolicyArtifactManifest,
    PolicyEvaluationRecord,
    PolicyExecutionRef,
)
from course_insight.modules.m6_tutoring_fsm.safety_envelope import SafetyEnvelope
from course_insight.modules.m6_tutoring_fsm.service import (
    M6TutoringControlService,
)
from course_insight.modules.m6_tutoring_fsm.state_machine import (
    DEFAULT_STATE_MACHINE,
)
from course_insight.modules.m7_local_model.adapter import PlaceholderRubricAdapter
from course_insight.modules.m7_local_model.repository import M7Repository
from course_insight.modules.m7_local_model.service import M7LocalModelService
from course_insight.modules.m8_assessment_scoring.paper_generator import (
    PaperGenerator,
)
from course_insight.modules.m8_assessment_scoring.repository import M8Repository
from course_insight.modules.m8_assessment_scoring.rule_scorer import RuleScorer
from course_insight.modules.m8_assessment_scoring.service import (
    M8AssessmentService,
)
from course_insight.modules.m9_teacher_analytics.repository import M9Repository
from course_insight.modules.m9_teacher_analytics.service import (
    M9TeacherAnalyticsService,
)


T = TypeVar("T")


class OutboxWorkerLike(Protocol):
    """Minimal future worker surface accepted by the composition root."""

    def run(self, *, once: bool = False) -> object:
        """Run continuously or perform one bounded delivery cycle."""


@dataclass(frozen=True, slots=True)
class RepositoryOverrides:
    """Typed test/deployment repository injection points."""

    m0: M0Repository | None = None
    m1: M1Repository | None = None
    m2: M2Repository | None = None
    m3: M3Repository | None = None
    m4: M4Repository | None = None
    m5: M5Repository | None = None
    m6: M6Repository | None = None
    m7: M7Repository | None = None
    m8: M8Repository | None = None
    m9: M9Repository | None = None


@dataclass(frozen=True, slots=True)
class ServiceOverrides:
    """Typed service injection points for isolated application tests."""

    m0: M0PlatformService | None = None
    m1: M1CourseGovernanceService | None = None
    m2: M2EvidenceRetrievalService | None = None
    m3: M3KnowledgeBundleService | None = None
    m4: M4TaskOrchestrationService | None = None
    m5: M5StateService | None = None
    m6: M6TutoringControlService | None = None
    m7: M7LocalModelService | None = None
    m8: M8AssessmentService | None = None
    m9: M9TeacherAnalyticsService | None = None


@dataclass(frozen=True, slots=True)
class ApplicationContainer:
    """Internal immutable graph shared by every process entry point."""

    settings: PlatformSettings
    m0_service: M0PlatformService
    m1_service: M1CourseGovernanceService
    m2_service: M2EvidenceRetrievalService
    m3_service: M3KnowledgeBundleService
    m4_service: M4TaskOrchestrationService
    m5_service: M5StateService
    m6_service: M6TutoringControlService
    m7_service: M7LocalModelService
    m8_service: M8AssessmentService
    m9_service: M9TeacherAnalyticsService
    coordinator: AppCoordinator
    runtime_registry: CourseRuntimeRegistry
    outbox_worker: OutboxWorkerLike | None
    persistence_backend: object
    database_pool: PostgresPool | None = None
    _owns_database_pool: bool = field(
        default=False,
        repr=False,
        compare=False,
    )

    def close(self) -> None:
        """Release resources created by this composition root."""

        if self._owns_database_pool and self.database_pool is not None:
            self.database_pool.close()


@dataclass(frozen=True, slots=True)
class _DurableGraph:
    repositories: Mapping[str, object]
    database_pool: PostgresPool | None
    owns_database_pool: bool


@dataclass(frozen=True, slots=True)
class _MemoryM1Repository:
    packages: Mapping[tuple[str, str], CoursePackage] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def save_course_package(self, package: CoursePackage) -> None:
        key = (package.course_package_id, package.package_version)
        updated = {**self.packages, key: package.model_copy(deep=True)}
        object.__setattr__(self, "packages", MappingProxyType(updated))

    def get_course_package(
        self,
        course_package_id: str,
        package_version: str,
    ) -> CoursePackage | None:
        package = self.packages.get((course_package_id, package_version))
        return None if package is None else package.model_copy(deep=True)


@dataclass(frozen=True, slots=True)
class _MemoryM2Repository:
    indexes: Mapping[tuple[str, str], EvidenceIndexRef] = field(
        default_factory=lambda: MappingProxyType({})
    )
    snapshots: Mapping[tuple[str, str], LexicalIndexSnapshot] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def save_index(self, index: EvidenceIndexRef) -> None:
        key = (index.index_id, index.index_version)
        updated = {**self.indexes, key: index.model_copy(deep=True)}
        object.__setattr__(self, "indexes", MappingProxyType(updated))

    def get_index(
        self,
        index_id: str,
        index_version: str,
    ) -> EvidenceIndexRef | None:
        index = self.indexes.get((index_id, index_version))
        return None if index is None else index.model_copy(deep=True)

    def save_index_artifact(
        self,
        index: EvidenceIndexRef,
        snapshot: LexicalIndexSnapshot,
    ) -> None:
        key = (index.index_id, index.index_version)
        updated_indexes = {**self.indexes, key: index.model_copy(deep=True)}
        updated_snapshots = {**self.snapshots, key: snapshot}
        object.__setattr__(self, "indexes", MappingProxyType(updated_indexes))
        object.__setattr__(self, "snapshots", MappingProxyType(updated_snapshots))

    def load_index_artifact(
        self,
        index_id: str,
        index_version: str,
    ) -> tuple[EvidenceIndexRef, LexicalIndexSnapshot] | None:
        key = (index_id, index_version)
        index = self.indexes.get(key)
        snapshot = self.snapshots.get(key)
        if index is None or snapshot is None:
            return None
        return index.model_copy(deep=True), snapshot


@dataclass(frozen=True, slots=True)
class _MemoryM3Repository:
    bundles: Mapping[tuple[str, str], KnowledgeBundle] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def save_knowledge_bundle(self, bundle: KnowledgeBundle) -> None:
        key = (bundle.knowledge_bundle_id, bundle.bundle_version)
        updated = {**self.bundles, key: bundle.model_copy(deep=True)}
        object.__setattr__(self, "bundles", MappingProxyType(updated))

    def get_knowledge_bundle(
        self,
        knowledge_bundle_id: str,
        bundle_version: str,
    ) -> KnowledgeBundle | None:
        bundle = self.bundles.get((knowledge_bundle_id, bundle_version))
        return None if bundle is None else bundle.model_copy(deep=True)


@dataclass(frozen=True, slots=True)
class _DeterministicDependency:
    name: str


def build_application(
    settings: PlatformSettings,
    *,
    repositories: RepositoryOverrides | None = None,
    services: ServiceOverrides | None = None,
    outbox_worker: OutboxWorkerLike | None = None,
    postgres_pool: PostgresPool | None = None,
) -> ApplicationContainer:
    """Assemble exactly one dependency graph from validated settings."""

    repository_overrides = (
        RepositoryOverrides() if repositories is None else repositories
    )
    service_overrides = ServiceOverrides() if services is None else services
    durable_graph = _durable_repositories(
        settings,
        repository_overrides,
        service_overrides,
        postgres_pool=postgres_pool,
    )
    try:
        return _assemble_application(
            settings,
            repository_overrides=repository_overrides,
            service_overrides=service_overrides,
            durable_graph=durable_graph,
            outbox_worker=outbox_worker,
        )
    except BaseException:
        if (
            durable_graph.owns_database_pool
            and durable_graph.database_pool is not None
        ):
            _close_pool_safely(durable_graph.database_pool)
        raise


def _assemble_application(
    settings: PlatformSettings,
    *,
    repository_overrides: RepositoryOverrides,
    service_overrides: ServiceOverrides,
    durable_graph: _DurableGraph,
    outbox_worker: OutboxWorkerLike | None,
) -> ApplicationContainer:
    """Finish assembly after backend resources have been acquired."""

    durable = durable_graph.repositories

    m1_repository = _runtime_repository(
        repository_override=repository_overrides.m1,
        service_override=service_overrides.m1,
        default_factory=lambda: durable["m1"]
        if "m1" in durable
        else FileM1Repository(settings.runtime_dir),
    )
    m2_repository = _runtime_repository(
        repository_override=repository_overrides.m2,
        service_override=service_overrides.m2,
        default_factory=lambda: durable["m2"]
        if "m2" in durable
        else FileM2Repository(settings.runtime_dir),
    )
    m3_repository = _runtime_repository(
        repository_override=repository_overrides.m3,
        service_override=service_overrides.m3,
        default_factory=lambda: durable["m3"]
        if "m3" in durable
        else FileM3Repository(settings.runtime_dir),
    )

    m0 = (
        M0PlatformService(
            settings.database.sqlite_path,
            settings.runtime_dir,
            settings.config_dir,
            repository=durable["m0"],
        )
        if service_overrides.m0 is None
        else service_overrides.m0
    )
    m1 = (
        M1CourseGovernanceService(
            {
                ".md": parse_source,
                ".txt": parse_source,
                ".pdf": parse_source,
                ".docx": parse_source,
                ".pptx": parse_source,
            },
            _sha256,
            cast(M1Repository, m1_repository),
        )
        if service_overrides.m1 is None
        else service_overrides.m1
    )
    embedding_provider = _build_embedding_provider(settings)
    vector_store = (
        PostgresPgVectorStore(durable_graph.database_pool)
        if settings.database.backend == "postgresql"
        and durable_graph.database_pool is not None
        else None
    )
    audit_store = (
        RepositoryRetrievalAuditStore(m2_repository)
        if callable(getattr(m2_repository, "save_retrieval_audit", None))
        and callable(getattr(m2_repository, "load_retrieval_audit", None))
        else None
    )
    m2 = (
        M2EvidenceRetrievalService(
            settings.runtime_dir / "indexes",
            _DeterministicDependency("lexical"),
            cast(M2Repository, m2_repository),
            embedding_provider=embedding_provider,
            vector_store=vector_store,
            audit_store=audit_store,
            production=settings.environment == "production",
        )
        if service_overrides.m2 is None
        else service_overrides.m2
    )
    retrieval_policy = RetrievalPolicy(
        policy_id=settings.retrieval.policy_id,
        strategy=settings.retrieval.strategy,
        top_k=settings.retrieval.top_k,
        lexical_weight=settings.retrieval.lexical_weight,
        vector_weight=settings.retrieval.vector_weight,
        rerank=settings.retrieval.rerank,
    )
    teacher_review_workflow = _build_teacher_review_workflow(m3_repository)
    m3 = (
        M3KnowledgeBundleService(
            cast(M3Repository, m3_repository),
            _accept_knowledge_bundle,
            review_workflow=teacher_review_workflow,
            require_teacher_approval=settings.environment == "production",
        )
        if service_overrides.m3 is None
        else service_overrides.m3
    )
    m4 = (
        _build_m4_service(settings, cast(M4Repository, durable["m4"]))
        if service_overrides.m4 is None
        else service_overrides.m4
    )
    m5 = (
        M5StateService(
            durable["m5"],
            DeterministicStateUpdatePolicy(),
            DeterministicClassAggregationPolicy(),
        )
        if service_overrides.m5 is None
        else service_overrides.m5
    )
    m6 = (
        M6TutoringControlService(
            DEFAULT_STATE_MACHINE,
            durable["m6"],
            policy_runtime=_build_m6_policy_runtime(
                settings,
                durable["m6"],
            ),
        )
        if service_overrides.m6 is None
        else service_overrides.m6
    )
    m7 = (
        M7LocalModelService(
            PlaceholderRubricAdapter(),
            durable["m7"],
            _accept_model_output,
        )
        if service_overrides.m7 is None
        else service_overrides.m7
    )
    m8 = (
        M8AssessmentService(
            durable["m8"],
            RuleScorer(),
            PaperGenerator(),
        )
        if service_overrides.m8 is None
        else service_overrides.m8
    )
    m9 = (
        M9TeacherAnalyticsService(
            durable["m9"],
            _DeterministicDependency("statistics"),
            _DeterministicDependency("suggestions"),
        )
        if service_overrides.m9 is None
        else service_overrides.m9
    )
    coordinator = AppCoordinator(
        m0,
        m1,
        m2,
        m3,
        m4,
        m5,
        m6,
        m7,
        m8,
        m9,
        retrieval_policy=retrieval_policy,
    )
    runtime_registry = CourseRuntimeRegistry(
        m0,
        m2,
        settings.runtime_dir,
        m1_repository=cast(M1Repository | None, m1_repository),
        m2_repository=cast(M2Repository | None, m2_repository),
        m3_repository=cast(M3Repository | None, m3_repository),
    )
    resolved_outbox_worker = outbox_worker
    if (
        resolved_outbox_worker is None
        and service_overrides.m0 is None
        and _supports_outbox(durable["m0"])
    ):
        m0_repository = cast(M0Repository, durable["m0"])
        worker_id = f"outbox-{os.getpid()}-{uuid.uuid4().hex}"
        resolved_outbox_worker = OutboxWorker(
            repository=m0_repository,
            sink=IdempotentJsonlSink(
                settings.runtime_dir / "audit" / "learning_events.jsonl"
            ),
            settings=settings.outbox,
            status_path=(
                settings.runtime_dir
                / "outbox_worker"
                / f"{worker_id}.status.json"
            ),
            worker_id=worker_id,
            )
    persistence_backend = durable.get("m1")
    if persistence_backend is None:
        persistence_backend = durable.get("m0")
    if persistence_backend is None:
        persistence_backend = getattr(m0, "_repository", m0)
    return ApplicationContainer(
        settings=settings,
        m0_service=m0,
        m1_service=m1,
        m2_service=m2,
        m3_service=m3,
        m4_service=m4,
        m5_service=m5,
        m6_service=m6,
        m7_service=m7,
        m8_service=m8,
        m9_service=m9,
        coordinator=coordinator,
        runtime_registry=runtime_registry,
        outbox_worker=resolved_outbox_worker,
        persistence_backend=persistence_backend,
        database_pool=durable_graph.database_pool,
        _owns_database_pool=durable_graph.owns_database_pool,
    )


def _build_m4_service(
    settings: PlatformSettings,
    repository: M4Repository,
) -> M4TaskOrchestrationService:
    intent_settings = settings.intent
    adapter: IntentAdapter | None = None
    if intent_settings.mode in {"shadow", "active"}:
        model_ref = intent_settings.model_ref
        model_id = intent_settings.model_id
        model_version = intent_settings.model_version
        model_sha256 = intent_settings.model_sha256
        if (
            model_ref is None
            or model_id is None
            or model_version is None
            or model_sha256 is None
        ):
            raise RuntimeError("validated intent artifact is unavailable")
        try:
            runtime_dir = settings.runtime_dir.resolve()
            model_dir = (runtime_dir / model_ref).resolve()
        except (OSError, RuntimeError):
            raise RuntimeError(
                "validated intent artifact is unavailable"
            ) from None
        if (
            model_dir == runtime_dir
            or not model_dir.is_relative_to(runtime_dir)
        ):
            raise RuntimeError("validated intent artifact is unavailable")
        adapter = sklearn_adapter.load_sklearn_intent_adapter(
            model_dir,
            runtime_dir=runtime_dir,
            expected_model_id=model_id,
            expected_model_version=model_version,
            expected_model_sha256=model_sha256,
        )
    intent_service = M4IntentService(
        repository,
        canonical_idempotency_key,
        mode=intent_settings.mode,
        adapter=adapter,
        policy=IntentPolicy(
            intent_settings.min_confidence,
            intent_settings.min_margin,
            intent_settings.fallback_to_rules,
            intent_settings.fail_closed,
        ),
        policy_version=intent_settings.policy_version,
    )
    return M4TaskOrchestrationService(
        repository,
        canonical_idempotency_key,
        intent_service=intent_service,
    )


def _runtime_repository(
    *,
    repository_override: object | None,
    service_override: object | None,
    default_factory: Callable[[], object],
) -> object | None:
    """Resolve the repository used by both a module service and runtime restore."""

    if service_override is not None:
        # A service override owns its private persistence boundary.  Do not
        # construct an unused default repository merely for the registry.
        return getattr(service_override, "_repository", None)
    if repository_override is not None:
        return repository_override
    return default_factory()


def _durable_repositories(
    settings: PlatformSettings,
    overrides: RepositoryOverrides,
    services: ServiceOverrides,
    *,
    postgres_pool: PostgresPool | None,
) -> _DurableGraph:
    names = ("m0", "m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8", "m9")
    if settings.database.backend == "postgresql":
        names = ("m0", "m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8", "m9")
        legacy_defaults_required = [
            name
            for name in ("m0", "m4", "m5", "m6", "m7", "m8", "m9")
            if getattr(overrides, name) is None
            and getattr(services, name) is None
        ]
        s1_s6_defaults_required = [
            name
            for name in ("m1", "m2", "m3")
            if getattr(overrides, name) is None
            and getattr(services, name) is None
        ]
        # Existing test-only complete legacy overrides intentionally do not
        # open an unused PostgreSQL connection.  Production and all normal
        # partial graphs still require the one S1-S6 authority.
        defaults_required = legacy_defaults_required + (
            s1_s6_defaults_required
            if settings.environment == "production" or legacy_defaults_required
            else []
        )
        pool = postgres_pool
        owns_pool = False
        if defaults_required and pool is None:
            if settings.database.url is None:
                raise DomainError(
                    code="DATABASE_BACKEND_UNAVAILABLE",
                    module="application",
                    message="configured database adapters are unavailable",
                    details={"backend": "postgresql"},
                    recoverable=True,
                )
            pool = create_postgres_pool(
                settings.database.url.get_secret_value(),
                min_size=settings.database.pool_min_size,
                max_size=settings.database.pool_max_size,
                connect_timeout_seconds=(
                    settings.database.connect_timeout_seconds
                ),
            )
            owns_pool = True
        defaults: dict[str, object] = {}
        try:
            if pool is not None:
                shared_s1_s6 = PostgresM1M2M3Repository(pool)
                defaults = {
                    "m0": PostgresM0Repository(pool),
                    "m1": shared_s1_s6,
                    "m2": shared_s1_s6,
                    "m3": shared_s1_s6,
                    "m4": PostgresM4Repository(pool),
                    "m5": PostgresM5Repository(pool),
                    "m6": PostgresM6Repository(pool),
                    "m7": PostgresM7Repository(pool),
                    "m8": PostgresM8Repository(pool),
                    "m9": PostgresM9Repository(pool),
                }
        except BaseException:
            if owns_pool and pool is not None:
                _close_pool_safely(pool)
            raise
        selected = {}
        for name in names:
            override = getattr(overrides, name)
            if override is not None:
                selected[name] = override
            elif name in defaults:
                selected[name] = defaults[name]
        return _DurableGraph(
            repositories=MappingProxyType(selected),
            database_pool=pool,
            owns_database_pool=owns_pool,
        )

    if postgres_pool is not None:
        raise ValueError("PostgreSQL pool requires the PostgreSQL backend")

    database_path = settings.database.sqlite_path
    defaults: dict[str, object] = {
        "m0": SQLiteM0Repository(database_path),
        "m4": SQLiteM4Repository(database_path),
        "m5": SQLiteM5Repository(database_path),
        "m6": SQLiteM6Repository(database_path),
        "m7": SQLiteM7Repository(database_path),
        "m8": SQLiteM8Repository(database_path),
        "m9": SQLiteM9Repository(database_path),
    }
    s1_s6_defaults_required = [
        name
        for name in ("m1", "m2", "m3")
        if getattr(overrides, name) is None
        and getattr(services, name) is None
    ]
    if s1_s6_defaults_required:
        from course_insight.infrastructure.sqlite.m1_m2_m3_repository import (
            SQLiteM1M2M3Repository,
        )

        shared_s1_s6 = SQLiteM1M2M3Repository(database_path)
        shared_s1_s6.initialize()
        defaults.update(
            {
                "m1": shared_s1_s6,
                "m2": shared_s1_s6,
                "m3": shared_s1_s6,
            }
        )
    selected = {}
    for name in names:
        override = getattr(overrides, name)
        if override is not None:
            selected[name] = override
        elif name in defaults:
            selected[name] = defaults[name]
    return _DurableGraph(
        repositories=MappingProxyType(selected),
        database_pool=None,
        owns_database_pool=False,
    )


def _supports_outbox(repository: object) -> bool:
    methods = (
        "claim_outbox_batch",
        "mark_outbox_delivered",
        "mark_outbox_failed",
        "renew_outbox_leases",
    )
    return all(callable(getattr(repository, name, None)) for name in methods)


def _build_m6_policy_runtime(
    settings: PlatformSettings,
    repository: object,
) -> PolicyRuntime:
    configured = settings.m6_policy
    execution_loader = _frozen_execution_loader(settings, repository)
    if configured.mode == "rules":
        return PolicyRuntime(
            mode="rules",
            execution_loader=execution_loader,
            gate_policy_version=configured.gate_policy_version,
            emergency_kill_switch=configured.global_kill_switch,
        )
    manifest = _load_policy_manifest(repository, configured.policy_id)
    if manifest is None:
        return PolicyRuntime(
            mode=configured.mode,
            execution_loader=execution_loader,
            gate_policy_version=configured.gate_policy_version,
            emergency_kill_switch=configured.global_kill_switch,
        )
    try:
        loaded = load_policy_artifact_for_manifest(
            configured.runtime_directory,
            manifest,
            expected_action_ids=SafetyEnvelope.all_candidate_ids(),
        )
        adapter = LinUCBPolicyAdapter(
            loaded,
            exploration_rate=configured.exploration_rate,
        )
    except (OSError, TypeError, ValueError):
        return PolicyRuntime(
            mode=configured.mode,
            execution_loader=execution_loader,
            gate_policy_version=configured.gate_policy_version,
            emergency_kill_switch=configured.global_kill_switch,
        )
    evaluation = _load_policy_evaluation(
        repository,
        policy_id=manifest.policy_id,
        dataset_identity=configured.evaluation_dataset_identity,
    )
    usable_evaluation = _usable_active_evaluation(
        evaluation,
        minimum_support=configured.minimum_support,
    )
    gate_inputs = PolicyRuntimeGateInputs(
        support=(
            None if evaluation is None else evaluation.observation_count
        ),
        offline_evaluation_approved=usable_evaluation,
        allowed_course_ids=configured.allowed_course_ids,
        allowed_class_ids=configured.allowed_class_ids,
    )
    gate = ActivePolicyGate(
        PolicyGateConfig(
            gate_policy_version=configured.gate_policy_version,
            minimum_support=configured.minimum_support,
            maximum_uncertainty=configured.maximum_uncertainty,
            rollout_percentage=configured.rollout_percentage,
            kill_switch=configured.global_kill_switch,
        )
    )
    return PolicyRuntime(
        mode=configured.mode,
        learned_adapter=adapter,
        artifact_loader=_fixed_artifact_loader(loaded),
        execution_loader=execution_loader,
        active_gate=gate,
        gate_inputs=gate_inputs,
        gate_policy_version=configured.gate_policy_version,
        emergency_kill_switch=configured.global_kill_switch,
    )


def _frozen_execution_loader(
    settings: PlatformSettings,
    repository: object,
) -> Callable[
    [PolicyExecutionRef, tuple[str, ...]],
    tuple[LoadedPolicyArtifact, LinUCBPolicyAdapter],
]:
    configured = settings.m6_policy

    def load(
        execution: PolicyExecutionRef,
        candidate_ids: tuple[str, ...],
    ) -> tuple[LoadedPolicyArtifact, LinUCBPolicyAdapter]:
        if not candidate_ids:
            raise ValueError("frozen execution requires safe candidates")
        if execution.exploration_rate is None:
            raise ValueError(
                "frozen learned execution has no exploration rate"
            )
        manifest = _load_policy_manifest(repository, execution.policy_id)
        if manifest is None:
            raise ValueError("frozen policy manifest is unavailable")
        loaded = load_policy_artifact_for_manifest(
            configured.runtime_directory,
            manifest,
            expected_action_ids=SafetyEnvelope.all_candidate_ids(),
        )
        return (
            loaded,
            LinUCBPolicyAdapter(
                loaded,
                exploration_rate=execution.exploration_rate,
            ),
        )

    return load


def _load_policy_manifest(
    repository: object,
    policy_id: str | None,
) -> PolicyArtifactManifest | None:
    getter = getattr(repository, "get_policy_artifact", None)
    if policy_id is None or not callable(getter):
        return None
    try:
        value = getter(policy_id)
    except Exception:
        return None
    return (
        value
        if isinstance(value, PolicyArtifactManifest)
        and value.policy_id == policy_id
        else None
    )


def _load_policy_evaluation(
    repository: object,
    *,
    policy_id: str,
    dataset_identity: str | None,
) -> PolicyEvaluationRecord | None:
    getter = getattr(repository, "get_policy_evaluation", None)
    if dataset_identity is None or not callable(getter):
        return None
    try:
        value = getter(policy_id, dataset_identity)
    except Exception:
        return None
    return (
        value
        if isinstance(value, PolicyEvaluationRecord)
        and value.policy_id == policy_id
        and value.dataset_identity == dataset_identity
        else None
    )


def _usable_active_evaluation(
    evaluation: PolicyEvaluationRecord | None,
    *,
    minimum_support: int,
) -> bool:
    if evaluation is None:
        return False
    metric_names = {name for name, _ in evaluation.metrics}
    interval_names = {
        name for name, _, _ in evaluation.confidence_intervals
    }
    return (
        evaluation.status == "sufficient_data"
        and evaluation.approved
        and evaluation.observation_count >= minimum_support
        and metric_names == set(EVALUATION_METRICS)
        and interval_names == set(EVALUATION_METRICS)
    )


def _fixed_artifact_loader(
    loaded: LoadedPolicyArtifact,
) -> Callable[[tuple[str, ...]], LoadedPolicyArtifact]:
    action_ids = frozenset(loaded.payload["actions"])

    def load(candidate_ids: tuple[str, ...]) -> LoadedPolicyArtifact:
        if not candidate_ids or not set(candidate_ids).issubset(action_ids):
            raise ValueError("safe candidates are missing from artifact")
        return loaded

    return load


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _build_embedding_provider(
    settings: PlatformSettings,
) -> object | None:
    """Construct the configured provider without making test doubles implicit."""

    configured = settings.embedding
    if configured.backend == "disabled":
        return None
    if (
        configured.endpoint is None
        or configured.model_name is None
        or configured.model_version is None
        or configured.dimension is None
    ):
        raise DomainError(
            code="EMBEDDING_CONFIGURATION_INVALID",
            module="application",
            message="embedding provider configuration is incomplete",
            recoverable=False,
        )
    try:
        model_ref = EmbeddingModelIdentity(
            provider="openai_compatible",
            model_name=configured.model_name,
            model_version=configured.model_version,
            dimension=configured.dimension,
        )
        return OpenAICompatibleEmbeddingProvider.from_environment(
            endpoint=configured.endpoint,
            api_key_env=configured.api_key_env,
            model_ref=model_ref,
            environment=settings.environment,
            timeout_seconds=configured.timeout_seconds,
            max_retries=configured.max_retries,
            verify_tls=configured.verify_tls,
        )
    except DomainError:
        raise
    except Exception:
        raise DomainError(
            code="EMBEDDING_CONFIGURATION_INVALID",
            module="application",
            message="embedding provider configuration is invalid",
            recoverable=False,
        ) from None


def _build_teacher_review_workflow(
    repository: object | None,
) -> TeacherReviewWorkflow | None:
    """Bind S4 only when the selected durable backend exposes CAS storage."""

    if repository is None or not all(
        callable(getattr(repository, name, None))
        for name in (
            "get_teacher_review",
            "save_teacher_review",
            "compare_and_swap_teacher_review",
        )
    ):
        return None
    return TeacherReviewWorkflow(RepositoryTeacherReviewRepository(repository))


def _accept_knowledge_bundle(bundle: KnowledgeBundle) -> bool:
    return isinstance(bundle, KnowledgeBundle)


def _accept_model_output(value: object) -> bool:
    return value is not None


def _selected(override: T | None, default: T) -> T:
    return default if override is None else override


def _close_pool_safely(pool: PostgresPool) -> None:
    """Best-effort cleanup without masking the assembly failure."""

    try:
        pool.close()
    except Exception:
        pass
