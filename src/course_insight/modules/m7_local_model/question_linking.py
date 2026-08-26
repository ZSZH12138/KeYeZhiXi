"""Governed closed-vocabulary DeepSeek question-to-concept linking."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from course_insight.contracts.errors import DomainError
from course_insight.contracts.intelligence import LLMGenerationRequest, LLMModelRef
from course_insight.contracts.knowledge_ingestion import (
    MergedKnowledgeConcept,
    QuestionConceptLinkCandidate,
)
from course_insight.infrastructure.deepseek import DeepSeekClient
from course_insight.modules.m3_knowledge_bundle.question_files import ParsedQuestion
from course_insight.modules.m7_local_model.policy import (
    DEFAULT_M7_EXECUTION_POLICY,
    M7ExecutionPolicy,
)
from course_insight.modules.m7_local_model.prompts import question_linking_prompt


class DeepSeekQuestionLinkingAdapter:
    """Accept only active concept IDs and attach their complete provenance."""

    def __init__(
        self,
        client: DeepSeekClient,
        policy: M7ExecutionPolicy = DEFAULT_M7_EXECUTION_POLICY,
        usable_threshold: float = 0.7,
    ) -> None:
        if not 0.0 <= usable_threshold <= 1.0:
            raise ValueError("usable_threshold must be between zero and one")
        self._client = client
        self._policy = policy
        self._usable_threshold = usable_threshold

    def link(
        self,
        question: ParsedQuestion,
        concepts: list[MergedKnowledgeConcept],
        *,
        model_ref: LLMModelRef,
        created_at: datetime,
    ) -> tuple[QuestionConceptLinkCandidate, ...]:
        prompt = question_linking_prompt(question, concepts, self._policy)
        request = LLMGenerationRequest(
            request_id=f"m7_link_{question.question_id}_{prompt.input_checksum[:12]}",
            use_case="question_concept_linking",
            model_ref=model_ref,
            prompt_template_id=prompt.prompt_id,
            prompt_template_version=prompt.prompt_version,
            evidence_ids=list(prompt.evidence_ids),
            input_checksum=prompt.input_checksum,
            created_at=created_at,
        )
        invocation = self._client.invoke_json(request=request, messages=prompt.messages)
        if invocation.result.status != "succeeded":
            raise DomainError(
                code="QUESTION_CONCEPT_LINKING_FAILED",
                module="m7",
                message="DeepSeek question concept linking did not complete successfully",
                details={"question_id": question.question_id},
                recoverable=True,
            )
        try:
            return self._parse(
                question,
                concepts,
                invocation.result.structured_output,
            )
        except (DomainError, ValidationError, TypeError, ValueError, KeyError) as error:
            raise DomainError(
                code="QUESTION_CONCEPT_LINKING_OUTPUT_INVALID",
                module="m7",
                message="DeepSeek question concept linking output is invalid",
                details={"question_id": question.question_id, "reason": type(error).__name__},
                recoverable=True,
            ) from error

    def _parse(
        self,
        question: ParsedQuestion,
        concepts: list[MergedKnowledgeConcept],
        payload: dict[str, Any],
    ) -> tuple[QuestionConceptLinkCandidate, ...]:
        if set(payload) != {"links", "citation_ids"}:
            raise ValueError("question linking fields are invalid")
        raw_links = payload["links"]
        citations = payload["citation_ids"]
        if type(raw_links) is not list or type(citations) is not list:
            raise ValueError("question linking collections are invalid")
        by_id = {concept.concept_id: concept for concept in concepts}
        links: list[QuestionConceptLinkCandidate] = []
        seen: set[str] = set()
        for raw_link in raw_links:
            if type(raw_link) is not dict or set(raw_link) != {"concept_id", "confidence"}:
                raise ValueError("question link fields are invalid")
            concept_id = raw_link["concept_id"]
            confidence = raw_link["confidence"]
            if (
                type(concept_id) is not str
                or concept_id not in by_id
                or concept_id in seen
                or type(confidence) not in {int, float}
                or not math.isfinite(float(confidence))
                or not 0.0 <= float(confidence) <= 1.0
            ):
                raise ValueError("question link value is invalid")
            seen.add(concept_id)
            concept = by_id[concept_id]
            links.append(
                QuestionConceptLinkCandidate(
                    question_id=question.question_id,
                    concept_id=concept_id,
                    confidence=float(confidence),
                    status=(
                        "usable"
                        if float(confidence) >= self._usable_threshold
                        else "needs_review"
                    ),
                    evidence=list(concept.evidence),
                )
            )
        if citations != [link.concept_id for link in links]:
            raise ValueError("question link citations do not match links")
        return tuple(links)
