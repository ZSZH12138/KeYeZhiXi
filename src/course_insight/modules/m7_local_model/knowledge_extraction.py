"""Governed DeepSeek adapter for exhaustive, source-bound concept extraction."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Callable

from pydantic import ValidationError

from course_insight.contracts.errors import DomainError
from course_insight.contracts.intelligence import LLMGenerationRequest, LLMModelRef
from course_insight.contracts.knowledge_ingestion import (
    KnowledgeCandidate,
    KnowledgeEvidenceRef,
    KnowledgeExtractionBatch,
    KnowledgeExtractionResult,
)
from course_insight.infrastructure.deepseek import DeepSeekClient
from course_insight.modules.m7_local_model.policy import (
    DEFAULT_M7_EXECUTION_POLICY,
    M7ExecutionPolicy,
)
from course_insight.modules.m7_local_model.prompts import knowledge_extraction_prompt


_CONCEPT_FIELDS = frozenset({"name", "description", "aliases", "evidence"})
_EVIDENCE_FIELDS = frozenset({"chunk_id", "quote", "relation_type"})
_FALLBACK_ERROR_CODES = frozenset(
    {
        "DEEPSEEK_INCOMPLETE_RESPONSE",
        "DEEPSEEK_NETWORK_ERROR",
        "DEEPSEEK_RESPONSE_INVALID",
        "DEEPSEEK_SERVICE_UNAVAILABLE",
    }
)
_OUTPUT_VALIDATION_RETRY_LIMIT = 5


class _KnowledgeOutputValidationError(ValueError):
    def __init__(self, validation_code: str) -> None:
        super().__init__(validation_code)
        self.validation_code = validation_code


class DeepSeekKnowledgeExtractionAdapter:
    """Convert governed DeepSeek JSON into locally grounded candidates."""

    def __init__(
        self,
        client: DeepSeekClient,
        policy: M7ExecutionPolicy = DEFAULT_M7_EXECUTION_POLICY,
        *,
        fallback_client: DeepSeekClient | None = None,
    ) -> None:
        self._client = client
        self._policy = policy
        self._fallback_client = fallback_client

    def extract(
        self,
        batch: KnowledgeExtractionBatch,
        *,
        model_ref: LLMModelRef,
        created_at: datetime,
        on_retry: Callable[[int, int, str], None] | None = None,
    ) -> KnowledgeExtractionResult:
        validation_code: str | None = None
        for attempt in range(_OUTPUT_VALIDATION_RETRY_LIMIT + 1):
            prompt = knowledge_extraction_prompt(
                batch,
                self._policy,
                retry_attempt=attempt,
                validation_code=validation_code,
            )
            request = LLMGenerationRequest(
                request_id=(
                    f"m7_extract_{batch.batch_id.removeprefix('batch_')}_"
                    f"{prompt.input_checksum[:12]}"
                ),
                use_case="knowledge_extraction",
                model_ref=model_ref,
                prompt_template_id=prompt.prompt_id,
                prompt_template_version=prompt.prompt_version,
                evidence_ids=list(prompt.evidence_ids),
                input_checksum=prompt.input_checksum,
                created_at=created_at,
            )
            invocation = self._client.invoke_json(
                request=request,
                messages=prompt.messages,
            )
            if (
                invocation.result.status != "succeeded"
                and self._fallback_client is not None
                and invocation.audit.error_code in _FALLBACK_ERROR_CODES
            ):
                invocation = self._fallback_client.invoke_json(
                    request=request,
                    messages=prompt.messages,
                )
            if invocation.result.status != "succeeded":
                raise DomainError(
                    code="KNOWLEDGE_EXTRACTION_FAILED",
                    module="m7",
                    message="DeepSeek knowledge extraction did not complete successfully",
                    details={
                        "batch_id": batch.batch_id,
                        "error_code": invocation.audit.error_code or "unknown",
                    },
                    recoverable=True,
                )

            try:
                candidates, citation_ids = _parse_candidates(
                    invocation.result.structured_output,
                    batch,
                )
                expected_citations = list(
                    dict.fromkeys(
                        reference.chunk_id
                        for candidate in candidates
                        for reference in candidate.evidence
                    )
                )
                if citation_ids != expected_citations:
                    raise _KnowledgeOutputValidationError(
                        "citation_ids_mismatch"
                    )
                return KnowledgeExtractionResult(
                    result_id=f"result_{hashlib.sha256(request.request_id.encode('utf-8')).hexdigest()}",
                    batch=batch,
                    candidates=candidates,
                    status="succeeded",
                    possibly_truncated=len(candidates) == 100,
                    created_at=invocation.result.generated_at,
                )
            except (
                DomainError,
                ValidationError,
                KeyError,
                TypeError,
                ValueError,
            ) as error:
                validation_code = _validation_code(error)
                if attempt < _OUTPUT_VALIDATION_RETRY_LIMIT:
                    if on_retry is not None:
                        on_retry(
                            attempt + 1,
                            _OUTPUT_VALIDATION_RETRY_LIMIT,
                            validation_code,
                        )
                    continue
                raise DomainError(
                    code="KNOWLEDGE_EXTRACTION_OUTPUT_INVALID",
                    module="m7",
                    message="DeepSeek knowledge extraction output is invalid or ungrounded",
                    details={
                        "batch_id": batch.batch_id,
                        "reason": type(error).__name__,
                        "validation_code": validation_code,
                        "retry_count": _OUTPUT_VALIDATION_RETRY_LIMIT,
                        "attempt_count": _OUTPUT_VALIDATION_RETRY_LIMIT + 1,
                    },
                    recoverable=True,
                ) from error

        raise RuntimeError("knowledge extraction retry loop ended unexpectedly")


def _parse_candidates(
    payload: dict[str, Any],
    batch: KnowledgeExtractionBatch,
) -> tuple[list[KnowledgeCandidate], list[str]]:
    if set(payload) != {"concepts", "citation_ids"}:
        raise _KnowledgeOutputValidationError("top_level_fields_invalid")
    raw_concepts = payload["concepts"]
    citation_ids = payload["citation_ids"]
    if type(raw_concepts) is not list or len(raw_concepts) > 100:
        raise _KnowledgeOutputValidationError("concept_count_invalid")
    if (
        type(citation_ids) is not list
        or any(type(identifier) is not str or not identifier for identifier in citation_ids)
        or len(citation_ids) != len(set(citation_ids))
    ):
        raise _KnowledgeOutputValidationError("citation_ids_invalid")

    chunks = {chunk.chunk_id: chunk for chunk in batch.chunks}
    candidates: list[KnowledgeCandidate] = []
    for ordinal, raw_concept in enumerate(raw_concepts, start=1):
        if type(raw_concept) is not dict or set(raw_concept) != _CONCEPT_FIELDS:
            raise _KnowledgeOutputValidationError("concept_fields_invalid")
        name = raw_concept["name"]
        description = raw_concept["description"]
        aliases = raw_concept["aliases"]
        raw_evidence = raw_concept["evidence"]
        if (
            type(name) is not str
            or not name.strip()
            or type(description) is not str
            or not description.strip()
            or type(aliases) is not list
            or any(type(alias) is not str for alias in aliases)
            or type(raw_evidence) is not list
            or not raw_evidence
        ):
            raise _KnowledgeOutputValidationError("concept_content_invalid")

        evidence: list[KnowledgeEvidenceRef] = []
        for raw_reference in raw_evidence:
            if type(raw_reference) is not dict or set(raw_reference) != _EVIDENCE_FIELDS:
                raise _KnowledgeOutputValidationError("evidence_fields_invalid")
            chunk_id = raw_reference["chunk_id"]
            quote = raw_reference["quote"]
            if (
                type(chunk_id) is not str
                or chunk_id not in chunks
                or type(quote) is not str
                or not quote
            ):
                raise _KnowledgeOutputValidationError("evidence_identity_invalid")
            chunk = chunks[chunk_id]
            span_start = chunk.text.find(quote)
            if span_start < 0:
                raise _KnowledgeOutputValidationError(
                    "evidence_quote_not_found"
                )
            span_end = span_start + len(quote)
            evidence.append(
                KnowledgeEvidenceRef(
                    source_id=chunk.source_id,
                    source_version_id=chunk.source_id,
                    chunk_id=chunk.chunk_id,
                    locator=chunk.locator,
                    span_start=span_start,
                    span_end=span_end,
                    relation_type=raw_reference["relation_type"],
                )
            )

        candidate_seed = f"{batch.batch_id}\0{ordinal}\0{name.strip()}"
        candidates.append(
            KnowledgeCandidate(
                candidate_id=(
                    "candidate_"
                    + hashlib.sha256(candidate_seed.encode("utf-8")).hexdigest()
                ),
                name=name.strip(),
                description=description.strip(),
                aliases=list(aliases),
                evidence=evidence,
            )
        )
    return candidates, list(citation_ids)


def _validation_code(error: Exception) -> str:
    if isinstance(error, _KnowledgeOutputValidationError):
        return error.validation_code
    if isinstance(error, (DomainError, ValidationError)):
        return "contract_validation_failed"
    return "output_structure_invalid"
