"""Live PostgreSQL+pgvector and HTTP embedding acceptance path."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Iterator

import pytest

from course_insight.contracts.course import (
    CoursePackage,
)
from course_insight.contracts.evidence import EvidenceQuery, evidence_id_for_chunk
from course_insight.contracts.intelligence import RetrievalPolicy
from course_insight.infrastructure.postgresql.m1_m2_m3_repository import (
    PostgresM1M2M3Repository,
    PostgresPgVectorStore,
)
from course_insight.infrastructure.postgresql.migration_runner import (
    destroy_schema_for_tests,
    rebuild_schema_for_tests,
    run_migrations,
)
from course_insight.infrastructure.postgresql.pool import (
    PostgresPool,
    create_postgres_pool,
)
from course_insight.modules.m1_course_governance.parsers import parse_source
from course_insight.modules.m1_course_governance.service import (
    M1CourseGovernanceService,
)
from course_insight.modules.m2_evidence_retrieval.audit import (
    RepositoryRetrievalAuditStore,
)
from course_insight.modules.m2_evidence_retrieval.embedding import (
    EmbeddingModelIdentity,
    OpenAICompatibleEmbeddingProvider,
)
from course_insight.modules.m2_evidence_retrieval.service import (
    M2EvidenceRetrievalService,
)
from course_insight.modules.m3_knowledge_bundle.service import (
    M3KnowledgeBundleService,
)
from course_insight.modules.m3_knowledge_bundle.teacher_review import (
    RepositoryTeacherReviewRepository,
    TeacherReviewWorkflow,
)
from tests.integration._postgres_live import require_live_test_database_url


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


def _embedding_for(text: str) -> list[float]:
    return {
        "alpha rule": [1.0, 0.0],
        "beta rule": [0.8, 0.6],
        "unmatched": [1.0, 0.0],
    }[text]


class _EmbeddingHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/embeddings":
            self.send_error(404)
            return
        if self.headers.get("Authorization") != "Bearer live-test-key":
            self.send_error(401)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        inputs = payload["input"]
        response = {
            "object": "list",
            "model": "live-test-model",
            "data": [
                {
                    "object": "embedding",
                    "index": index,
                    "embedding": _embedding_for(text),
                }
                for index, text in enumerate(inputs)
            ],
        }
        body = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture(scope="module")
def embedding_endpoint() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture(scope="module")
def postgres_pool() -> Iterator[PostgresPool]:
    pool = create_postgres_pool(
        require_live_test_database_url(),
        min_size=1,
        max_size=4,
        connect_timeout_seconds=5,
    )
    try:
        rebuild_schema_for_tests(pool, allow_destructive=True)
        run_migrations(pool)
        yield pool
    finally:
        destroy_schema_for_tests(pool, allow_destructive=True)
        pool.close()


def _import_package(tmp_path: Path, repository: PostgresM1M2M3Repository) -> CoursePackage:
    source = tmp_path / "course.txt"
    source_bytes = b"alpha rule\n\nbeta rule"
    source.write_bytes(source_bytes)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    metadata = tmp_path / "course.json"
    metadata.write_text(
        json.dumps(
            {
                "course_package_id": "live_package",
                "course_id": "live_course",
                "package_version": "1.0.0",
                "course_name": "Live course",
                "imported_at": NOW.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    authorization = tmp_path / "authorization.csv"
    authorization.write_text(
        "file_name,source_id,expected_sha256,authorized_by,authorized_at,license_note\n"
        f"course.txt,live_source,{source_sha256},live-test,{NOW.isoformat()},acceptance\n",
        encoding="utf-8",
    )
    return M1CourseGovernanceService(
        {".txt": parse_source},
        lambda payload: hashlib.sha256(payload).hexdigest(),
        repository,
    ).import_course([source], metadata, authorization, tmp_path / "import-output")


def _service(
    pool: PostgresPool,
    endpoint: str,
    repository: PostgresM1M2M3Repository,
) -> M2EvidenceRetrievalService:
    model = EmbeddingModelIdentity(
        provider="live-test",
        model_name="live-test-model",
        model_version="v1",
        dimension=2,
    )
    provider = OpenAICompatibleEmbeddingProvider(
        endpoint=endpoint,
        api_key="live-test-key",
        model_ref=model,
        environment="test",
        timeout_seconds=2,
        max_retries=0,
        verify_tls=False,
    )
    return M2EvidenceRetrievalService(
        Path("runtime/live-m2"),
        "live-http",
        repository,
        embedding_provider=provider,
        vector_store=PostgresPgVectorStore(pool),
        audit_store=RepositoryRetrievalAuditStore(repository),
        production=True,
    )


def _write_m3_seeds(tmp_path: Path, package: CoursePackage) -> dict[str, Path]:
    evidence_id = evidence_id_for_chunk(package.content_chunks[0].chunk_id)
    payloads: dict[str, dict[str, object]] = {
        "concept": {
            "knowledge_bundle_id": "live_bundle",
            "bundle_version": "1.0.0",
            "published_at": NOW.isoformat(),
            "course_id": package.course_id,
            "course_package_id": package.course_package_id,
            "course_package_checksum": package.checksum,
            "concepts": [
                {
                    "concept_id": "live_concept",
                    "name": "Live concept",
                    "chapter_id": "chapter_1",
                    "description": "A governed live concept.",
                    "aliases": [],
                    "status": "published",
                }
            ],
            "concept_evidence_ids": {"live_concept": [evidence_id]},
        },
        "item": {
            "items": [
                {
                    "item_id": "live_item",
                    "version": "1.0.0",
                    "stem": "Can the governed rule be retrieved?",
                    "item_type": "true_false",
                    "concept_ids": ["live_concept"],
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
            "q_matrix": [
                {
                    "item_id": "live_item",
                    "item_version": "1.0.0",
                    "concept_id": "live_concept",
                    "weight": 1.0,
                }
            ],
        },
        "rubric": {"rubrics": []},
        "blueprint": {
            "blueprints": [
                {
                    "blueprint_id": "live_blueprint",
                    "version": "1.0.0",
                    "course_id": package.course_id,
                    "sections": [
                        {
                            "section_id": "live_section",
                            "name": "Live section",
                            "item_count": 1,
                            "score": 1.0,
                            "item_types": [],
                            "concept_weights": {"live_concept": 1.0},
                            "difficulty_range": [1, 1],
                            "anchor_item_ids": ["live_item"],
                            "anchor_item_versions": {"live_item": "1.0.0"},
                        }
                    ],
                    "total_score": 1.0,
                    "duration_minutes": 15,
                    "status": "teacher_approved",
                }
            ]
        },
        "prerequisite": {"prerequisite_relations": []},
        "misconception": {"misconception_tags": []},
    }
    seed_dir = tmp_path / "teacher-seeds"
    seed_dir.mkdir()
    paths: dict[str, Path] = {}
    for role, payload in payloads.items():
        path = seed_dir / f"{role}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        paths[role] = path
    return paths


def test_real_postgres_pgvector_http_embedding_retrieval_audit_and_restore(
    postgres_pool: PostgresPool,
    embedding_endpoint: str,
    tmp_path: Path,
) -> None:
    repository = PostgresM1M2M3Repository(postgres_pool)
    package = _import_package(tmp_path, repository)
    service = _service(postgres_pool, embedding_endpoint, repository)
    index = service.build_vector_index(package)
    query = EvidenceQuery(
        query_id="live-query",
        course_package_id=package.course_package_id,
        course_package_checksum=package.checksum,
        query_text="unmatched",
        concept_ids=[],
        required_evidence_ids=["evidence_live_chunk_2"],
        item_id=None,
        use_case="qa",
        top_k=1,
        min_relevance=1.0,
    )
    policy = RetrievalPolicy(
        policy_id="live-vector-v1",
        strategy="vector",
        top_k=1,
        lexical_weight=0.0,
        vector_weight=1.0,
        rerank=False,
    )

    first = service.retrieve_with_policy(
        query, index, policy, request_id="live-request-1"
    )
    assert first.citation_ids() == [
        "evidence_live_chunk_2",
        "evidence_live_chunk_1",
    ]

    restored_service = _service(postgres_pool, embedding_endpoint)
    restored_index = restored_service.restore_vector_index(
        course_package=package,
        evidence_index_ref=index,
    )
    second = restored_service.retrieve_with_policy(
        query, restored_index, policy, request_id="live-request-2"
    )
    assert second.citation_ids() == first.citation_ids()

    seeds = _write_m3_seeds(tmp_path, package)
    review_workflow = TeacherReviewWorkflow(RepositoryTeacherReviewRepository(repository))
    m3 = M3KnowledgeBundleService(
        repository,
        None,
        review_workflow=review_workflow,
        require_teacher_approval=True,
    )
    draft = m3.create_teacher_review_draft(
        review_id="live-review",
        subject_id=package.course_package_id,
        validation_report_ref="live-validation",
        now=NOW,
        concept_seed_path=seeds["concept"],
        item_seed_path=seeds["item"],
        rubric_seed_path=seeds["rubric"],
        blueprint_seed_path=seeds["blueprint"],
        prerequisite_seed_path=seeds["prerequisite"],
        misconception_seed_path=seeds["misconception"],
    )
    submitted = m3.submit_teacher_review(
        draft.review_id,
        "live-teacher",
        "Reviewed governed live evidence.",
        draft.version,
        NOW,
    )
    approved = m3.approve_teacher_review(
        submitted.review_id,
        "live-teacher",
        "Approved governed live evidence.",
        submitted.version,
        NOW,
    )
    bundle = m3.build_knowledge_bundle_after_approval(
        review_id=approved.review_id,
        review_version=approved.version,
        course_package=package,
        concept_seed_path=seeds["concept"],
        item_seed_path=seeds["item"],
        rubric_seed_path=seeds["rubric"],
        blueprint_seed_path=seeds["blueprint"],
        prerequisite_seed_path=seeds["prerequisite"],
        misconception_seed_path=seeds["misconception"],
    )
    assert bundle.course_package_checksum == package.checksum
    assert bundle.status == "published"

    for seed in seeds.values():
        seed.unlink()
    restored_package = PostgresM1M2M3Repository(postgres_pool).get_course_package(
        package.course_package_id, package.package_version
    )
    assert restored_package is not None
    fresh_m2 = _service(postgres_pool, embedding_endpoint, repository)
    restored_index = fresh_m2.restore_vector_index(
        course_package=restored_package,
        evidence_index_ref=index,
    )
    fresh_m3 = M3KnowledgeBundleService(repository, None)
    restored_bundle = fresh_m3.restore_knowledge_bundle(
        course_package=restored_package,
        knowledge_bundle_id=bundle.knowledge_bundle_id,
        bundle_version=bundle.bundle_version,
    )
    assert restored_bundle == bundle
    assert restored_index.course_package_checksum == restored_package.checksum

    with postgres_pool.connection() as connection:
        rows = connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM m2_retrieval_audits
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
    assert {row["status"]: row["count"] for row in rows} == {"succeeded": 2}
