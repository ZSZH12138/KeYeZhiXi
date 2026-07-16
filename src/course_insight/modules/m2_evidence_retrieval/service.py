"""Formal M2 evidence-retrieval service boundary."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from course_insight.contracts.course import CoursePackage
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import (
    EvidenceBundle,
    EvidenceChunk,
    EvidenceIndexRef,
    EvidenceQuery,
)
from course_insight.contracts.intelligence import RetrievalAudit
from course_insight.modules.m2_evidence_retrieval.repository import M2Repository


class M2EvidenceRetrievalService:
    """Build and query course-bound local evidence indexes."""

    def __init__(
        self,
        index_dir: Path,
        tokenizer_or_embedding_adapter: Any,
        repository: M2Repository,
    ) -> None:
        self._index_dir = index_dir
        self._tokenizer_or_embedding_adapter = tokenizer_or_embedding_adapter
        self._repository = repository
        self._indexed_packages: dict[str, CoursePackage] = {}
        self._index_refs: dict[str, EvidenceIndexRef] = {}

    def build_index(self, course_package: CoursePackage) -> EvidenceIndexRef:
        """Build a versioned index for one governed course package.

        原始输入：M1 输出的 CoursePackage。
        契约来源：M1CourseGovernanceService.import_course。
        返回消费者：后续 M2.retrieve 调用。
        业务校验：来源、切片数量、版本、路径和校验和必须一致。
        错误码：INDEX_NOT_READY。
        """

        if course_package.status != "ready":
            raise DomainError(
                code="INDEX_NOT_READY",
                module="m2",
                message="only ready course packages can be indexed",
                details={"course_package_id": course_package.course_package_id},
                recoverable=True,
            )
        index_id = f"{course_package.course_package_id}_lexical_index"
        checksum_payload = "\x00".join(
            [
                course_package.course_package_id,
                *(chunk.sha256 for chunk in course_package.content_chunks),
            ]
        )
        index_ref = EvidenceIndexRef(
            index_id=index_id,
            course_package_id=course_package.course_package_id,
            index_version=course_package.package_version,
            storage_ref=f"lexical:{index_id}",
            backend="lexical",
            embedding_model_id=None,
            source_count=len(course_package.source_documents),
            chunk_count=len(course_package.content_chunks),
            built_at=course_package.imported_at,
            checksum=hashlib.sha256(checksum_payload.encode("utf-8")).hexdigest(),
            status="ready",
        )
        self._indexed_packages[index_id] = course_package
        self._index_refs[index_id] = index_ref
        save = getattr(self._repository, "save_index", None)
        if callable(save):
            save(index_ref)
        return index_ref

    def initialize_vector_store(
        self,
        course_package_id: str,
        requested_at: datetime,
    ) -> EvidenceIndexRef:
        """Return a path-free empty pgvector index declaration.

        原始输入：M1 课程包标识和架构检查请求时间。
        契约来源：EvidenceIndexRef 的 pgvector 空状态约定。
        返回消费者：M2.empty_retrieval_audit 与 AppCoordinator。
        业务校验：不连接数据库，不创建表，不生成虚构向量。
        错误码：无；当前空实现固定返回 empty。
        """

        index_id = f"pgvector_empty_{course_package_id}"
        checksum = hashlib.sha256(
            f"pgvector:empty:{course_package_id}".encode("utf-8")
        ).hexdigest()
        return EvidenceIndexRef(
            index_id=index_id,
            course_package_id=course_package_id,
            index_version="unconfigured",
            storage_ref=f"pgvector:unconfigured:{course_package_id}",
            backend="pgvector",
            embedding_model_id=None,
            source_count=0,
            chunk_count=0,
            built_at=requested_at,
            checksum=checksum,
            status="empty",
        )

    def empty_retrieval_audit(
        self,
        index_ref: EvidenceIndexRef,
        requested_at: datetime,
    ) -> RetrievalAudit:
        """Record that the unconfigured RAG path retrieved no evidence.

        原始输入：M2 空向量索引引用和架构检查请求时间。
        契约来源：EvidenceIndexRef 与 intelligence.RetrievalAudit。
        返回消费者：AppCoordinator 与 M7/M9 调用审计。
        业务校验：空审计不得携带证据标识或执行检索。
        错误码：无；当前空实现固定返回 empty。
        """

        return RetrievalAudit(
            audit_id=f"retrieval_empty_{index_ref.index_id}",
            query_id=f"query_empty_{index_ref.course_package_id}",
            index_id=index_ref.index_id,
            policy_id="retrieval_policy_unconfigured",
            retrieved_evidence_ids=[],
            status="empty",
            created_at=requested_at,
        )

    def retrieve(
        self,
        evidence_query: EvidenceQuery,
        evidence_index_ref: EvidenceIndexRef,
    ) -> EvidenceBundle:
        """Retrieve traceable chunks from one ready evidence index.

        原始输入：M6/M8 查询和 M2 自身输出的索引引用。
        契约来源：EvidenceQuery 与 build_index 返回值。
        返回消费者：M7 评分/反馈和课程问答流程。
        业务校验：索引就绪、课程身份一致且每条证据可定位。
        错误码：INDEX_NOT_READY。
        """

        evidence_index_ref.assert_ready()
        package = self._indexed_packages.get(evidence_index_ref.index_id)
        stored_ref = self._index_refs.get(evidence_index_ref.index_id)
        if (
            package is None
            or stored_ref is None
            or evidence_query.course_package_id != package.course_package_id
            or evidence_index_ref.course_package_id != package.course_package_id
            or evidence_index_ref.checksum != stored_ref.checksum
        ):
            raise DomainError(
                code="INDEX_NOT_READY",
                module="m2",
                message="query and index must reference one in-memory course package",
                details={
                    "query_id": evidence_query.query_id,
                    "index_id": evidence_index_ref.index_id,
                },
                recoverable=True,
            )
        query_terms = self._lexical_terms(evidence_query.normalized_text())
        requested_concepts = set(evidence_query.concept_ids)
        ranked: list[tuple[float, str, EvidenceChunk]] = []
        for chunk in package.content_chunks:
            normalized_chunk = " ".join(chunk.text.split()).casefold()
            lexical_hits = sum(term in normalized_chunk for term in query_terms)
            lexical_score = lexical_hits / len(query_terms) if query_terms else 0.0
            concept_hit = bool(requested_concepts.intersection(chunk.concept_hints))
            relevance = min(1.0, lexical_score + (0.5 if concept_hit else 0.0))
            if relevance < evidence_query.min_relevance or relevance <= 0.0:
                continue
            evidence_chunk = EvidenceChunk(
                evidence_id=f"evidence_{chunk.chunk_id}",
                source_id=chunk.source_id,
                chunk_id=chunk.chunk_id,
                text=chunk.text,
                locator=chunk.locator,
                concept_ids=list(chunk.concept_hints),
                relevance=relevance,
                checksum=chunk.sha256,
            )
            ranked.append((relevance, chunk.chunk_id, evidence_chunk))
        ranked.sort(key=lambda entry: (-entry[0], entry[1]))
        return EvidenceBundle(
            query_id=evidence_query.query_id,
            index_id=evidence_index_ref.index_id,
            course_id=package.course_id,
            evidence_chunks=[
                entry[2] for entry in ranked[: evidence_query.top_k]
            ],
            retrieved_at=evidence_index_ref.built_at,
        )

    @staticmethod
    def _lexical_terms(text: str) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                re.findall(r"[0-9a-z_:]+|[\u4e00-\u9fff]+", text.casefold())
            )
        )
