from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from course_insight.application.retrieval import retrieve_for_application
from course_insight.contracts.evidence import EvidenceQuery, evidence_id_for_chunk
from course_insight.contracts.errors import DomainError
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.m1_file_repository import FileM1Repository
from course_insight.infrastructure.m2_file_repository import FileM2Repository
from course_insight.infrastructure.m3_file_repository import FileM3Repository
from course_insight.modules.m1_course_governance.parsers import parse_source
from course_insight.modules.m1_course_governance.service import (
    M1CourseGovernanceService,
)
from course_insight.modules.m2_evidence_retrieval.service import (
    M2EvidenceRetrievalService,
)
from course_insight.modules.m2_evidence_retrieval.audit import (
    InMemoryRetrievalAuditStore,
)
from course_insight.modules.m2_evidence_retrieval.embedding import (
    DeterministicEmbeddingProvider,
    EmbeddingModelIdentity,
)
from course_insight.modules.m2_evidence_retrieval.vector_store import (
    InMemoryVectorStore,
)
from course_insight.modules.m3_knowledge_bundle.service import (
    M3KnowledgeBundleService,
)
from course_insight.contracts.intelligence import RetrievalPolicy


NOW = datetime(2026, 8, 11, tzinfo=timezone.utc)


def _write_seed(path: Path, payload: object) -> Path:
    path.write_bytes(dumps_json(payload).encode("utf-8"))
    return path


def test_m1_m2_m3_chain_preserves_identity_and_restores_without_inputs(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    source = tmp_path / "lesson.txt"
    source_bytes = "Linear equations\n\nA governed rule can be retrieved.".encode("utf-8")
    source.write_bytes(source_bytes)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()

    metadata = tmp_path / "course.json"
    metadata.write_text(
        json.dumps(
            {
                "course_package_id": "package_chain",
                "course_id": "course_chain",
                "package_version": "v1",
                "course_name": "Chain Course",
                "imported_at": NOW.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    authorization = tmp_path / "authorization.csv"
    authorization.write_text(
        "file_name,source_id,expected_sha256,authorized_by,authorized_at,license_note\n"
        f"lesson.txt,source_chain,{source_sha256},teacher,{NOW.isoformat()},course-use\n",
        encoding="utf-8",
    )

    m1 = M1CourseGovernanceService(
        {".txt": parse_source},
        lambda payload: hashlib.sha256(payload).hexdigest(),
        FileM1Repository(runtime),
    )
    package = m1.import_course(
        [source], metadata, authorization, tmp_path / "import-output"
    )
    assert package.status == "ready"

    embedding_provider = DeterministicEmbeddingProvider.for_tests(
        model_ref=EmbeddingModelIdentity(
            provider="test",
            model_name="chain-embedding",
            model_version="v1",
            dimension=8,
        )
    )
    vector_store = InMemoryVectorStore()
    audit_store = InMemoryRetrievalAuditStore()
    m2 = M2EvidenceRetrievalService(
        runtime / "indexes",
        "lexical",
        FileM2Repository(runtime),
        embedding_provider=embedding_provider,
        vector_store=vector_store,
        audit_store=audit_store,
        production=True,
    )
    index_ref = m2.build_index(package)
    vector_ref = m2.build_vector_index(package)

    chunk = package.content_chunks[0]
    evidence_id = evidence_id_for_chunk(chunk.chunk_id)
    query = EvidenceQuery(
        query_id="chain_query",
        course_package_id=package.course_package_id,
        course_package_checksum=package.checksum,
        query_text="Linear equations",
        concept_ids=[],
        item_id=None,
        use_case="qa",
        top_k=1,
        min_relevance=0.1,
    )
    hybrid_policy = RetrievalPolicy(
        policy_id="chain-hybrid",
        strategy="hybrid",
        top_k=1,
        lexical_weight=0.5,
        vector_weight=0.5,
        rerank=False,
    )
    vector_bundle = retrieve_for_application(
        m2,
        query,
        vector_ref,
        policy=RetrievalPolicy(
            policy_id="chain-vector",
            strategy="vector",
            top_k=1,
            lexical_weight=0.0,
            vector_weight=1.0,
            rerank=False,
        ),
    )
    hybrid_bundle = retrieve_for_application(
        m2,
        query,
        vector_ref,
        policy=hybrid_policy,
        request_id="chain-hybrid-request",
    )
    assert vector_bundle.evidence_chunks
    assert hybrid_bundle.evidence_chunks
    assert len(audit_store._audits) == 2  # noqa: SLF001
    seeds = {
        "concept": {
            "knowledge_bundle_id": "bundle_chain",
            "bundle_version": "v1",
            "published_at": NOW.isoformat(),
            "course_id": package.course_id,
            "course_package_id": package.course_package_id,
            "course_package_checksum": package.checksum,
            "concepts": [
                {
                    "concept_id": "concept_chain",
                    "name": "Linear equations",
                    "chapter_id": "chapter_1",
                    "description": "A governed concept.",
                    "aliases": [],
                    "status": "published",
                }
            ],
            "concept_evidence_ids": {"concept_chain": [evidence_id]},
        },
        "item": {
            "items": [
                {
                    "item_id": "item_chain",
                    "version": "v1",
                    "stem": "Is the governed rule retrievable?",
                    "item_type": "true_false",
                    "concept_ids": ["concept_chain"],
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
                    "item_id": "item_chain",
                    "item_version": "v1",
                    "concept_id": "concept_chain",
                    "weight": 1.0,
                }
            ],
        },
        "rubric": {"rubrics": []},
        "blueprint": {
            "blueprints": [
                {
                    "blueprint_id": "blueprint_chain",
                    "version": "v1",
                    "course_id": package.course_id,
                    "sections": [
                        {
                            "section_id": "section_chain",
                            "name": "Chain section",
                            "item_count": 1,
                            "score": 1.0,
                            "item_types": [],
                            "concept_weights": {"concept_chain": 1.0},
                            "difficulty_range": [1, 1],
                            "anchor_item_ids": ["item_chain"],
                            "anchor_item_versions": {"item_chain": "v1"},
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
    seed_paths = {
        role: _write_seed(seed_dir / f"{role}.json", payload)
        for role, payload in seeds.items()
    }
    m3 = M3KnowledgeBundleService(FileM3Repository(runtime), None)
    bundle = m3.build_knowledge_bundle(
        package,
        seed_paths["concept"],
        seed_paths["item"],
        seed_paths["rubric"],
        seed_paths["blueprint"],
        seed_paths["prerequisite"],
        seed_paths["misconception"],
    )

    source.unlink()
    metadata.unlink()
    authorization.unlink()
    shutil.rmtree(seed_dir)

    fresh_m1 = FileM1Repository(runtime)
    fresh_m2 = M2EvidenceRetrievalService(
        runtime / "indexes",
        "lexical",
        FileM2Repository(runtime),
        embedding_provider=embedding_provider,
        vector_store=vector_store,
        audit_store=audit_store,
        production=True,
    )
    fresh_m3 = M3KnowledgeBundleService(FileM3Repository(runtime), None)
    restored_package = fresh_m1.get_course_package(
        package.course_package_id, package.package_version
    )
    assert restored_package is not None
    restored_index = fresh_m2.restore_index(
        course_package=restored_package, evidence_index_ref=index_ref
    )
    restored_vector_index = fresh_m2.restore_vector_index(
        course_package=restored_package, evidence_index_ref=vector_ref
    )
    discovered_vector_index = fresh_m2.restore_vector_index_from_store(
        course_package=restored_package,
        index_id=vector_ref.index_id,
        index_version=vector_ref.index_version,
    )
    restored_bundle = fresh_m3.restore_knowledge_bundle(
        course_package=restored_package,
        knowledge_bundle_id=bundle.knowledge_bundle_id,
        bundle_version=bundle.bundle_version,
    )
    evidence = fresh_m2.retrieve(
        query,
        restored_index,
    )
    restored_vector_evidence = fresh_m2.retrieve_with_policy(
        query,
        restored_vector_index,
        RetrievalPolicy(
            policy_id="chain-vector-restored",
            strategy="vector",
            top_k=1,
            lexical_weight=0.0,
            vector_weight=1.0,
            rerank=False,
        ),
    )

    assert restored_package.checksum == package.checksum
    assert restored_index.course_package_checksum == package.checksum
    assert restored_vector_index.course_package_checksum == package.checksum
    assert discovered_vector_index == restored_vector_index
    assert restored_vector_evidence.evidence_chunks
    assert restored_bundle.course_package_checksum == package.checksum
    assert restored_bundle.course_package_id == package.course_package_id
    assert [row.evidence_id for row in evidence.evidence_chunks] == [evidence_id]
