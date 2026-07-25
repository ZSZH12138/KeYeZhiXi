from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

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
from course_insight.application.runtime_context import (
    CourseRuntimeRegistry,
    RuntimeSnapshotRefs,
)
from course_insight.contracts.course import (
    ContentChunk,
    CoursePackage,
    SourceDocument,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceIndexRef, EvidenceQuery
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.platform import ActorContext
from course_insight.infrastructure.config import (
    DatabaseSettings,
    LoggingSettings,
    PlatformSettings,
)
from course_insight.infrastructure.postgresql.m0_repository import (
    PostgresM0Repository,
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


NOW = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)


def _settings(tmp_path: Path, *, backend: str = "sqlite") -> PlatformSettings:
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
    )


def _course_package() -> CoursePackage:
    chunk = ContentChunk(
        chunk_id="chunk_1",
        source_id="source_1",
        text="A governed rule explains the target concept.",
        locator="section:1",
        concept_hints=["concept_1"],
        sha256=hashlib.sha256(b"governed chunk").hexdigest(),
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
        source_authorizations=[],
        imported_at=NOW,
        status="ready",
        checksum="pending",
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
    package = _course_package()
    expected_index = M2EvidenceRetrievalServiceStub().build_index(package)
    if index_update:
        expected_index = expected_index.model_copy(
            update=index_update,
            deep=True,
        )
    bundle = _knowledge_bundle()
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
        expected_index,
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


class _FalseyRepositorySentinel:
    def __bool__(self) -> bool:
        return False


class _FailingM2Repository:
    @staticmethod
    def save_index(index: EvidenceIndexRef) -> None:
        del index
        raise OSError("repository unavailable")

    @staticmethod
    def get_index(
        index_id: str,
        index_version: str,
    ) -> EvidenceIndexRef | None:
        del index_id, index_version
        return None


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


def test_container_and_coordinator_share_exact_service_instances(
    tmp_path: Path,
) -> None:
    container = build_application(_settings(tmp_path))

    for number in range(10):
        service = getattr(container, f"m{number}_service")
        assert getattr(container.coordinator, f"_m{number}") is service
    assert container.runtime_registry.m0_service is container.m0_service
    assert container.runtime_registry.m2_service is container.m2_service


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


def test_runtime_registry_restores_snapshots_and_rebuilds_fresh_m2_index(
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
    assert [item.chunk_id for item in evidence.evidence_chunks] == ["chunk_1"]
    assert container.runtime_registry.require("course_1") == context


@pytest.mark.parametrize(
    ("index_update", "reason"),
    [
        ({"checksum": "wrong-checksum"}, "index_mismatch"),
        ({"course_package_id": "package_other"}, "identity_mismatch"),
        ({"status": "failed"}, "index_not_ready"),
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


def test_failed_m2_repository_save_does_not_pollute_live_index(
    tmp_path: Path,
) -> None:
    container = build_application(
        _settings(tmp_path),
        repositories=RepositoryOverrides(m2=_FailingM2Repository()),
    )
    container.m0_service.initialize()
    refs = _snapshot_refs(container)
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
    assert restore_error.value.details == {"reason": "index_rebuild_failed"}
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
