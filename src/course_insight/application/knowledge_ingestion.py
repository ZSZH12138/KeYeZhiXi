"""End-to-end candidate build and atomic publication for course knowledge."""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty, Queue
from typing import Callable, Protocol
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max

from course_insight.application.knowledge_ingestion_extraction import (
    ExtractionAdapter,
    _bisect_extraction_batch,
    _bounded_phase_progress,
    _parse_chunks,
    _recoverable_extraction_retry_reason,
    _safe_ingestion_error_code,
    _safe_source_processing_error_code,
)
from course_insight.contracts.course import ContentChunk
from course_insight.contracts.errors import DomainError
from course_insight.contracts.intelligence import LLMModelRef
from course_insight.contracts.knowledge_ingestion import (
    KnowledgeCandidate,
    KnowledgeEvidenceRef,
    KnowledgeExtractionBatch,
    KnowledgeExtractionResult,
    MergedKnowledgeConcept,
    QuestionConceptLinkCandidate,
)
from course_insight.infrastructure.logging import log_event
from course_insight.modules.m0_platform.django_app.models import (
    CourseClassWorkspace,
    CourseKnowledgeRelease,
    CourseSource,
    CourseSourceVersion,
    KnowledgeChangeOperation,
    KnowledgeExtractionBatchCheckpoint,
    KnowledgeIngestionJob,
    ReleaseConcept,
    ReleaseConceptSource,
    ReleaseQuestion,
    ReleaseQuestionConceptLink,
)
from course_insight.modules.m1_course_governance.legacy_powerpoint import (
    LegacyPowerPointConverter,
)
from course_insight.modules.m3_knowledge_bundle.concept_merge import (
    merge_knowledge_candidates,
)
from course_insight.modules.m3_knowledge_bundle.question_files import (
    ParsedQuestion,
    parse_question_file,
)
from course_insight.modules.m7_local_model.extraction_batches import (
    build_extraction_batches,
)
from course_insight.modules.m7_local_model.prompts import (
    KNOWLEDGE_EXTRACTION_PROMPT_ID,
    KNOWLEDGE_EXTRACTION_PROMPT_VERSION,
)


_LOGGER = logging.getLogger(__name__)


class QuestionLinkingAdapter(Protocol):
    def link(
        self,
        question: ParsedQuestion,
        concepts: list[MergedKnowledgeConcept],
        *,
        model_ref: LLMModelRef,
        created_at: datetime,
    ) -> tuple[QuestionConceptLinkCandidate, ...]: ...


@dataclass(frozen=True, slots=True)
class KnowledgeIngestionOutcome:
    job_id: str
    release_id: str
    status: str
    successful_source_ids: tuple[str, ...]
    failed_source_ids: tuple[str, ...]
    issue_count: int


@dataclass(frozen=True, slots=True)
class _QuestionBuild:
    question: ParsedQuestion
    source_version: CourseSourceVersion
    links: tuple[QuestionConceptLinkCandidate, ...]


@dataclass(frozen=True, slots=True)
class _ExtractionProgressEvent:
    batch_index: int
    event_type: str
    character_count: int = 0
    retry_reason: str | None = None
    retry_depth: int = 0
    retry_attempt: int = 0
    retry_limit: int = 0
    validation_code: str | None = None


@dataclass(frozen=True, slots=True)
class _ExtractionBatchOutcome:
    candidates: tuple[KnowledgeCandidate, ...]
    evidence_chunks: tuple[ContentChunk, ...]


def _merge_extraction_outcomes(
    outcomes: list[_ExtractionBatchOutcome],
) -> _ExtractionBatchOutcome:
    return _ExtractionBatchOutcome(
        candidates=tuple(
            candidate
            for outcome in outcomes
            for candidate in outcome.candidates
        ),
        evidence_chunks=tuple(
            chunk
            for outcome in outcomes
            for chunk in outcome.evidence_chunks
        ),
    )


class KnowledgeIngestionProcessor:
    """Build off-line, then switch one active-release pointer transactionally."""

    def __init__(
        self,
        *,
        storage_root: Path,
        extraction_adapter: ExtractionAdapter,
        question_linking_adapter: QuestionLinkingAdapter,
        model_ref: LLMModelRef,
        clock: Callable[[], datetime] | None = None,
        heartbeat: Callable[[], None] | None = None,
        legacy_powerpoint_converter: LegacyPowerPointConverter | None = None,
        extraction_concurrency: int = 3,
    ) -> None:
        if (
            isinstance(extraction_concurrency, bool)
            or not isinstance(extraction_concurrency, int)
            or not 1 <= extraction_concurrency <= 4
        ):
            raise ValueError("extraction_concurrency must be between 1 and 4")
        self._storage_root = Path(storage_root).resolve()
        self._extractor = extraction_adapter
        self._linker = question_linking_adapter
        self._model_ref = model_ref
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._heartbeat_callback = heartbeat or (lambda: None)
        self._legacy_powerpoint_converter = legacy_powerpoint_converter
        self._extraction_concurrency = extraction_concurrency

    def process(self, job_id: UUID) -> KnowledgeIngestionOutcome:
        """Process one restartable job without exposing a partial release."""

        job = KnowledgeIngestionJob.objects.get(pk=job_id)
        existing = CourseKnowledgeRelease.objects.filter(job=job).first()
        if existing is not None and existing.status in {
            CourseKnowledgeRelease.Status.ACTIVE,
            CourseKnowledgeRelease.Status.RETIRED,
        }:
            return self._replayed_outcome(job, existing)
        if job.status == KnowledgeIngestionJob.Status.QUEUED:
            job.transition_to(KnowledgeIngestionJob.Status.RUNNING)
        elif job.status != KnowledgeIngestionJob.Status.RUNNING:
            raise DomainError(
                code="KNOWLEDGE_JOB_NOT_RUNNABLE",
                module="m0",
                message="knowledge ingestion job is not runnable",
            )
        operations = list(
            job.operations.select_related("source").order_by("sequence")
        )
        active_release = CourseKnowledgeRelease.objects.filter(
            course_id=job.course_id,
            class_id=job.class_id,
            status=CourseKnowledgeRelease.Status.ACTIVE,
        ).first()
        if active_release is not None and _is_question_only_change(operations):
            try:
                return self._process_question_only_change(
                    job,
                    active_release,
                    operations,
                )
            except Exception as error:
                self._fail_job(job, error)
                raise
        if active_release is not None and _is_knowledge_only_change(operations):
            try:
                return self._process_incremental_knowledge_change(
                    job,
                    active_release,
                    operations,
                )
            except Exception as error:
                self._fail_job(job, error)
                raise
        failed_sources: list[str] = []
        failed_source_errors: dict[str, str] = {}
        successful_sources: list[str] = []
        issue_count = 0
        selected_versions: list[CourseSourceVersion] = []
        chunks: list[ContentChunk] = []
        question_inputs: list[tuple[CourseSourceVersion, str]] = []
        sources = list(
            CourseSource.objects.filter(
                course_id=job.course_id,
                class_id=job.class_id,
                status__in=(CourseSource.Status.STAGED, CourseSource.Status.ACTIVE),
            )
            .prefetch_related("versions")
            .order_by("source_id")
        )

        total_sources = len(sources)
        for source_index, source in enumerate(sources, start=1):
            completed_sources = source_index - 1
            self._record_stage(
                job,
                "reading_sources",
                _bounded_phase_progress(5, 24, completed_sources, total_sources),
                detail={
                    "completed_units": completed_sources,
                    "total_units": total_sources,
                    "unit_kind": "files",
                },
            )
            version = _preferred_version(source)
            if version is None:
                continue
            try:
                payload = self._read_version(version)
                if source.source_type == CourseSource.SourceType.KNOWLEDGE:
                    chunks.extend(
                        _parse_chunks(
                            source,
                            version,
                            payload,
                            legacy_powerpoint_converter=(
                                self._legacy_powerpoint_converter
                            ),
                        )
                    )
                else:
                    question_inputs.append((version, payload.decode("utf-8-sig")))
            except (DomainError, UnicodeError, ValueError, OSError) as error:
                source_id = str(source.pk)
                failed_sources.append(source_id)
                failed_source_errors = {
                    **failed_source_errors,
                    source_id: _safe_source_processing_error_code(error),
                }
                issue_count += 1
                if version.status == CourseSourceVersion.Status.STAGED:
                    CourseSourceVersion.objects.filter(pk=version.pk).update(
                        status=CourseSourceVersion.Status.FAILED
                    )
                fallback = _active_version(source, excluding=version.pk)
                if fallback is None:
                    continue
                try:
                    payload = self._read_version(fallback)
                    if source.source_type == CourseSource.SourceType.KNOWLEDGE:
                        chunks.extend(
                            _parse_chunks(
                                source,
                                fallback,
                                payload,
                                legacy_powerpoint_converter=(
                                    self._legacy_powerpoint_converter
                                ),
                            )
                        )
                    else:
                        question_inputs.append((fallback, payload.decode("utf-8-sig")))
                    selected_versions.append(fallback)
                except (DomainError, UnicodeError, ValueError, OSError):
                    continue
            else:
                selected_versions.append(version)
                successful_sources.append(str(source.pk))

        self._record_stage(
            job,
            "reading_sources",
            24,
            detail={
                "completed_units": total_sources,
                "total_units": total_sources,
                "unit_kind": "files",
            },
        )

        try:
            self._record_stage(job, "sources_parsed", 25)
            candidates, extracted_chunk_texts = self._extract_all(
                job,
                job.course_id,
                chunks,
            )
            self._record_stage(job, "concepts_extracted", 55)
            concepts = merge_knowledge_candidates(job.course_id, candidates)
            if chunks and not concepts:
                raise DomainError(
                    code="KNOWLEDGE_EXTRACTION_EMPTY",
                    module="m3",
                    message="knowledge files produced no grounded concepts",
                )
            self._record_stage(job, "concepts_merged", 65)
            pending_questions: list[tuple[CourseSourceVersion, ParsedQuestion]] = []
            for version, text in question_inputs:
                parsed = parse_question_file(str(version.source_id), text)
                issue_count += len(parsed.issues)
                for question in parsed.questions:
                    pending_questions.append((version, question))
            questions: list[_QuestionBuild] = []
            total_questions = len(pending_questions)
            self._record_stage(
                job,
                "linking_questions",
                65,
                detail={
                    "completed_units": 0,
                    "total_units": total_questions,
                    "unit_kind": "questions",
                },
            )
            for question_index, (version, question) in enumerate(
                pending_questions,
                start=1,
            ):
                try:
                    links = self._linker.link(
                        question,
                        list(concepts),
                        model_ref=self._model_ref,
                        created_at=self._aware_now(),
                    )
                except DomainError:
                    links = ()
                    issue_count += 1
                questions.append(_QuestionBuild(question, version, links))
                self._record_stage(
                    job,
                    "linking_questions",
                    _bounded_phase_progress(
                        65,
                        84,
                        question_index,
                        total_questions,
                    ),
                    detail={
                        "completed_units": question_index,
                        "total_units": total_questions,
                        "unit_kind": "questions",
                    },
                )
            self._record_stage(job, "questions_linked", 85)
            self._record_stage(job, "publishing", 90)
            release = self._publish(
                job=job,
                selected_versions=selected_versions,
                concepts=concepts,
                questions=questions,
                partial=bool(failed_sources or issue_count),
                chunk_text_by_id=extracted_chunk_texts,
                successful_source_ids=tuple(sorted(set(successful_sources))),
                failed_source_ids=tuple(sorted(set(failed_sources))),
                failed_source_errors=failed_source_errors,
                issue_count=issue_count,
            )
        except Exception as error:
            self._fail_job(job, error)
            raise

        status = (
            KnowledgeIngestionJob.Status.PARTIAL
            if failed_sources or issue_count
            else KnowledgeIngestionJob.Status.SUCCEEDED
        )
        return KnowledgeIngestionOutcome(
            job_id=str(job.pk),
            release_id=str(release.pk),
            status=status,
            successful_source_ids=tuple(sorted(set(successful_sources))),
            failed_source_ids=tuple(sorted(set(failed_sources))),
            issue_count=issue_count,
        )

    def _process_question_only_change(
        self,
        job: KnowledgeIngestionJob,
        active_release: CourseKnowledgeRelease,
        operations: list[KnowledgeChangeOperation],
    ) -> KnowledgeIngestionOutcome:
        changed_source_ids = {
            operation.source_id
            for operation in operations
            if operation.source_id is not None
        }
        concepts, chunk_text_by_id, concept_versions = (
            _snapshot_release_concepts(active_release, job.course_id)
        )
        questions = list(
            _snapshot_release_questions(
                active_release,
                excluded_source_ids=changed_source_ids,
            )
        )
        selected_versions = [
            *concept_versions,
            *(build.source_version for build in questions),
        ]
        changed_sources = _changed_question_sources(operations)
        question_inputs: list[tuple[CourseSourceVersion, str]] = []
        successful_sources: list[str] = []
        failed_source_errors: dict[str, str] = {}
        issue_count = 0

        for source_index, source in enumerate(changed_sources, start=1):
            self._record_stage(
                job,
                "reading_sources",
                _bounded_phase_progress(
                    5,
                    24,
                    source_index - 1,
                    len(changed_sources),
                ),
                detail={
                    "completed_units": source_index - 1,
                    "total_units": len(changed_sources),
                    "unit_kind": "files",
                },
            )
            version = _preferred_version(source)
            if version is None:
                continue
            try:
                text = self._read_version(version).decode("utf-8-sig")
            except (DomainError, UnicodeError, ValueError, OSError) as error:
                source_id = str(source.pk)
                failed_source_errors[source_id] = (
                    _safe_source_processing_error_code(error)
                )
                issue_count += 1
                if version.status == CourseSourceVersion.Status.STAGED:
                    CourseSourceVersion.objects.filter(pk=version.pk).update(
                        status=CourseSourceVersion.Status.FAILED
                    )
                fallback = _active_version(source, excluding=version.pk)
                if fallback is None:
                    continue
                try:
                    text = self._read_version(fallback).decode("utf-8-sig")
                except (DomainError, UnicodeError, ValueError, OSError):
                    continue
                version = fallback
            else:
                successful_sources.append(str(source.pk))
            selected_versions.append(version)
            question_inputs.append((version, text))

        self._record_stage(
            job,
            "reading_sources",
            24,
            detail={
                "completed_units": len(changed_sources),
                "total_units": len(changed_sources),
                "unit_kind": "files",
            },
        )
        self._record_stage(job, "sources_parsed", 25)
        self._record_stage(job, "concepts_merged", 65)
        changed_questions, changed_issue_count = self._parse_and_link_questions(
            job,
            concepts,
            question_inputs,
        )
        questions.extend(changed_questions)
        issue_count += changed_issue_count
        failed_sources = sorted(failed_source_errors)
        self._record_stage(job, "questions_linked", 85)
        self._record_stage(job, "publishing", 90)
        release = self._publish(
            job=job,
            selected_versions=_unique_versions(selected_versions),
            concepts=concepts,
            questions=questions,
            partial=bool(failed_sources or issue_count),
            chunk_text_by_id=chunk_text_by_id,
            successful_source_ids=tuple(sorted(set(successful_sources))),
            failed_source_ids=tuple(failed_sources),
            failed_source_errors=failed_source_errors,
            issue_count=issue_count,
        )
        status = (
            KnowledgeIngestionJob.Status.PARTIAL
            if failed_sources or issue_count
            else KnowledgeIngestionJob.Status.SUCCEEDED
        )
        return KnowledgeIngestionOutcome(
            job_id=str(job.pk),
            release_id=str(release.pk),
            status=status,
            successful_source_ids=tuple(sorted(set(successful_sources))),
            failed_source_ids=tuple(failed_sources),
            issue_count=issue_count,
        )

    def _process_incremental_knowledge_change(
        self,
        job: KnowledgeIngestionJob,
        active_release: CourseKnowledgeRelease,
        operations: list[KnowledgeChangeOperation],
    ) -> KnowledgeIngestionOutcome:
        active_concepts, chunk_text_by_id, concept_versions = (
            _snapshot_release_concepts(active_release, job.course_id)
        )
        active_concept_ids = {concept.concept_id for concept in active_concepts}
        deleted_source_ids = {
            operation.source_id
            for operation in operations
            if operation.operation == KnowledgeChangeOperation.Operation.DELETE
            and operation.source_id is not None
        }
        deleted_version_ids = {
            str(version_id)
            for version_id in CourseSourceVersion.objects.filter(
                source_id__in=deleted_source_ids
            ).values_list("pk", flat=True)
        }
        retained_concepts = _filter_concept_evidence(
            active_concepts,
            excluded_version_ids=deleted_version_ids,
        )
        questions = list(
            _snapshot_release_questions(
                active_release,
                excluded_source_ids=set(),
            )
        )
        selected_versions = [
            *(
                version
                for version in concept_versions
                if version.source_id not in deleted_source_ids
            ),
            *(build.source_version for build in questions),
        ]
        changed_sources = _changed_knowledge_sources(operations)
        chunks: list[ContentChunk] = []
        successful_sources: list[str] = [
            str(source_id) for source_id in deleted_source_ids
        ]
        failed_source_errors: dict[str, str] = {}

        for source_index, source in enumerate(changed_sources, start=1):
            self._record_stage(
                job,
                "reading_sources",
                _bounded_phase_progress(
                    5,
                    24,
                    source_index - 1,
                    len(changed_sources),
                ),
                detail={
                    "completed_units": source_index - 1,
                    "total_units": len(changed_sources),
                    "unit_kind": "files",
                },
            )
            version = _preferred_version(source)
            if version is None:
                continue
            try:
                payload = self._read_version(version)
                chunks.extend(
                    _parse_chunks(
                        source,
                        version,
                        payload,
                        legacy_powerpoint_converter=self._legacy_powerpoint_converter,
                    )
                )
            except (DomainError, UnicodeError, ValueError, OSError) as error:
                source_id = str(source.pk)
                failed_source_errors[source_id] = _safe_source_processing_error_code(
                    error
                )
                CourseSourceVersion.objects.filter(pk=version.pk).update(
                    status=CourseSourceVersion.Status.FAILED
                )
                continue
            selected_versions.append(version)
            successful_sources.append(str(source.pk))

        self._record_stage(
            job,
            "reading_sources",
            24,
            detail={
                "completed_units": len(changed_sources),
                "total_units": len(changed_sources),
                "unit_kind": "files",
            },
        )
        self._record_stage(job, "sources_parsed", 25)
        candidates, added_chunk_texts = self._extract_all(
            job,
            job.course_id,
            chunks,
        )
        if chunks and not candidates:
            raise DomainError(
                code="KNOWLEDGE_EXTRACTION_EMPTY",
                module="m3",
                message="knowledge files produced no grounded concepts",
            )
        chunk_text_by_id.update(added_chunk_texts)
        concepts = merge_knowledge_candidates(
            job.course_id,
            [
                *(_concept_as_candidate(concept) for concept in retained_concepts),
                *candidates,
            ],
        )
        current_ids = {concept.concept_id for concept in concepts}
        issue_count = len(failed_source_errors)
        if current_ids - active_concept_ids:
            questions, linking_issues = self._relink_stored_questions(
                job,
                concepts,
                questions,
            )
            issue_count += linking_issues
        else:
            questions = _refresh_question_link_evidence(questions, concepts)
        self._record_stage(job, "concepts_extracted", 55)
        self._record_stage(job, "concepts_merged", 65)
        self._record_stage(job, "questions_linked", 85)
        self._record_stage(job, "publishing", 90)
        failed_sources = sorted(failed_source_errors)
        release = self._publish(
            job=job,
            selected_versions=_unique_versions(selected_versions),
            concepts=concepts,
            questions=questions,
            partial=bool(failed_sources or issue_count),
            chunk_text_by_id=chunk_text_by_id,
            successful_source_ids=tuple(sorted(set(successful_sources))),
            failed_source_ids=tuple(failed_sources),
            failed_source_errors=failed_source_errors,
            issue_count=issue_count,
        )
        status = (
            KnowledgeIngestionJob.Status.PARTIAL
            if failed_sources or issue_count
            else KnowledgeIngestionJob.Status.SUCCEEDED
        )
        return KnowledgeIngestionOutcome(
            job_id=str(job.pk),
            release_id=str(release.pk),
            status=status,
            successful_source_ids=tuple(sorted(set(successful_sources))),
            failed_source_ids=tuple(failed_sources),
            issue_count=issue_count,
        )

    def _relink_stored_questions(
        self,
        job: KnowledgeIngestionJob,
        concepts: tuple[MergedKnowledgeConcept, ...],
        questions: list[_QuestionBuild],
    ) -> tuple[list[_QuestionBuild], int]:
        relinked: list[_QuestionBuild] = []
        issue_count = 0
        total_questions = len(questions)
        self._record_stage(
            job,
            "linking_questions",
            65,
            detail={
                "completed_units": 0,
                "total_units": total_questions,
                "unit_kind": "questions",
            },
        )
        for question_index, build in enumerate(questions, start=1):
            try:
                links = self._linker.link(
                    build.question,
                    list(concepts),
                    model_ref=self._model_ref,
                    created_at=self._aware_now(),
                )
            except DomainError:
                links = ()
                issue_count += 1
            relinked.append(_QuestionBuild(build.question, build.source_version, links))
            self._record_stage(
                job,
                "linking_questions",
                _bounded_phase_progress(
                    65,
                    84,
                    question_index,
                    total_questions,
                ),
                detail={
                    "completed_units": question_index,
                    "total_units": total_questions,
                    "unit_kind": "questions",
                },
            )
        return relinked, issue_count

    def _parse_and_link_questions(
        self,
        job: KnowledgeIngestionJob,
        concepts: tuple[MergedKnowledgeConcept, ...],
        question_inputs: list[tuple[CourseSourceVersion, str]],
    ) -> tuple[list[_QuestionBuild], int]:
        pending_questions: list[tuple[CourseSourceVersion, ParsedQuestion]] = []
        issue_count = 0
        for version, text in question_inputs:
            parsed = parse_question_file(str(version.source_id), text)
            issue_count += len(parsed.issues)
            pending_questions.extend(
                (version, question) for question in parsed.questions
            )
        questions: list[_QuestionBuild] = []
        total_questions = len(pending_questions)
        self._record_stage(
            job,
            "linking_questions",
            65,
            detail={
                "completed_units": 0,
                "total_units": total_questions,
                "unit_kind": "questions",
            },
        )
        for question_index, (version, question) in enumerate(
            pending_questions,
            start=1,
        ):
            try:
                links = self._linker.link(
                    question,
                    list(concepts),
                    model_ref=self._model_ref,
                    created_at=self._aware_now(),
                )
            except DomainError:
                links = ()
                issue_count += 1
            questions.append(_QuestionBuild(question, version, links))
            self._record_stage(
                job,
                "linking_questions",
                _bounded_phase_progress(
                    65,
                    84,
                    question_index,
                    total_questions,
                ),
                detail={
                    "completed_units": question_index,
                    "total_units": total_questions,
                    "unit_kind": "questions",
                },
            )
        return questions, issue_count

    def _extract_all(
        self,
        job: KnowledgeIngestionJob,
        course_id: str,
        chunks: list[ContentChunk],
    ) -> tuple[list[KnowledgeCandidate], dict[str, str]]:
        batches = build_extraction_batches(chunks, course_id=course_id)
        chunk_text_by_id = {
            chunk.chunk_id: chunk.text
            for batch in batches
            for chunk in batch.chunks
        }
        if not batches:
            return [], chunk_text_by_id
        total_characters = sum(batch.character_count for batch in batches)
        completed_characters = 0
        completed_indices: set[int] = set()
        retry_characters: dict[int, int] = {}
        retry_counts: dict[int, int] = {}
        candidates_by_index: dict[int, list[KnowledgeCandidate]] = {}
        events: Queue[_ExtractionProgressEvent] = Queue()

        for batch_index, batch in enumerate(batches, start=1):
            checkpoint_outcome = self._restore_extraction_checkpoint(
                job,
                batch_index,
                batch,
            )
            if checkpoint_outcome is None:
                continue
            candidates_by_index[batch_index] = list(checkpoint_outcome.candidates)
            chunk_text_by_id.update(
                {
                    chunk.chunk_id: chunk.text
                    for chunk in checkpoint_outcome.evidence_chunks
                }
            )
            completed_indices.add(batch_index)
            completed_characters += batch.character_count

        def record_progress(event: _ExtractionProgressEvent) -> None:
            nonlocal completed_characters
            if event.batch_index in completed_indices:
                return
            batch = batches[event.batch_index - 1]
            retry_counts[event.batch_index] = (
                retry_counts.get(event.batch_index, 0) + 1
            )
            if event.event_type == "retry_progress":
                retry_characters[event.batch_index] = min(
                    batch.character_count,
                    retry_characters.get(event.batch_index, 0)
                    + event.character_count,
                )
            current_characters = min(
                total_characters,
                completed_characters + sum(retry_characters.values()),
            )
            detail: dict[str, object] = {
                "current_batch": event.batch_index,
                "total_batches": len(batches),
                "completed_characters": current_characters,
                "total_characters": total_characters,
            }
            if event.retry_reason is not None:
                detail.update(
                    {
                        "retry_reason": event.retry_reason,
                        "retry_depth": event.retry_depth,
                    }
                )
            if event.event_type == "model_retry":
                detail.update(
                    {
                        "retry_attempt": event.retry_attempt,
                        "retry_limit": event.retry_limit,
                        "validation_code": event.validation_code or "unknown",
                    }
                )
            self._record_stage(
                job,
                "extracting_concepts",
                _bounded_phase_progress(
                    25,
                    54,
                    current_characters,
                    total_characters,
                ),
                detail=detail,
            )

        def drain_progress_events() -> None:
            while True:
                try:
                    event = events.get_nowait()
                except Empty:
                    return
                record_progress(event)

        def submit_batch(
            executor: ThreadPoolExecutor,
            batch_index: int,
            batch: KnowledgeExtractionBatch,
        ) -> Future[_ExtractionBatchOutcome]:
            return executor.submit(
                self._extract_batch_safely,
                batch,
                depth=0,
                retry_reason=None,
                on_retry_progress=lambda count, reason, depth: events.put(
                    _ExtractionProgressEvent(
                        batch_index=batch_index,
                        event_type="retry_progress",
                        character_count=count,
                        retry_reason=reason,
                        retry_depth=depth,
                    )
                ),
                on_model_retry=lambda attempt, limit, code, depth: events.put(
                    _ExtractionProgressEvent(
                        batch_index=batch_index,
                        event_type="model_retry",
                        retry_reason="KNOWLEDGE_EXTRACTION_OUTPUT_INVALID",
                        retry_depth=depth,
                        retry_attempt=attempt,
                        retry_limit=limit,
                        validation_code=code,
                    )
                ),
            )

        self._record_stage(
            job,
            "extracting_concepts",
            _bounded_phase_progress(
                25,
                54,
                completed_characters,
                total_characters,
            ),
            detail={
                "current_batch": next(
                    (
                        index
                        for index in range(1, len(batches) + 1)
                        if index not in completed_indices
                    ),
                    len(batches),
                ),
                "total_batches": len(batches),
                "completed_characters": completed_characters,
                "total_characters": total_characters,
            },
        )
        remaining_indices = [
            index
            for index in range(1, len(batches) + 1)
            if index not in completed_indices
        ]
        if not remaining_indices:
            return (
                [
                    candidate
                    for batch_index in range(1, len(batches) + 1)
                    for candidate in candidates_by_index[batch_index]
                ],
                chunk_text_by_id,
            )
        worker_count = min(self._extraction_concurrency, len(remaining_indices))
        executor = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="knowledge-extraction",
        )
        future_to_index: dict[Future[_ExtractionBatchOutcome], int] = {}
        next_remaining_position = 0

        def fill_available_workers() -> None:
            nonlocal next_remaining_position
            while (
                len(future_to_index) < worker_count
                and next_remaining_position < len(remaining_indices)
            ):
                batch_index = remaining_indices[next_remaining_position]
                next_remaining_position += 1
                future = submit_batch(
                    executor,
                    batch_index,
                    batches[batch_index - 1],
                )
                future_to_index[future] = batch_index

        fill_available_workers()
        try:
            while future_to_index:
                done, _ = wait(
                    set(future_to_index),
                    timeout=1.0,
                    return_when=FIRST_COMPLETED,
                )
                drain_progress_events()
                if not done:
                    self._renew_lease(job)
                    continue
                first_error: Exception | None = None
                for future in sorted(done, key=future_to_index.__getitem__):
                    batch_index = future_to_index.pop(future)
                    batch = batches[batch_index - 1]
                    try:
                        batch_outcome = future.result()
                    except Exception as error:
                        log_event(
                            _LOGGER,
                            "knowledge_ingestion.extraction_batch_failed",
                            level=logging.ERROR,
                            error_code=_safe_ingestion_error_code(error),
                            error=error,
                            failure_stage="extracting_concepts",
                        )
                        self._persist_extraction_checkpoint(
                            job,
                            batch_index,
                            batch,
                            candidates=[],
                            evidence_chunks=[],
                            retry_count=retry_counts.get(batch_index, 0),
                            error=error,
                        )
                        if first_error is None:
                            first_error = error
                        continue
                    batch_candidates = list(batch_outcome.candidates)
                    candidates_by_index[batch_index] = batch_candidates
                    chunk_text_by_id.update(
                        {
                            chunk.chunk_id: chunk.text
                            for chunk in batch_outcome.evidence_chunks
                        }
                    )
                    self._persist_extraction_checkpoint(
                        job,
                        batch_index,
                        batch,
                        candidates=batch_candidates,
                        evidence_chunks=list(batch_outcome.evidence_chunks),
                        retry_count=retry_counts.get(batch_index, 0),
                    )
                    retry_characters.pop(batch_index, None)
                    completed_indices.add(batch_index)
                    completed_characters += batch.character_count
                    self._record_stage(
                        job,
                        "extracting_concepts",
                        _bounded_phase_progress(
                            25,
                            54,
                            completed_characters,
                            total_characters,
                        ),
                        detail={
                            "current_batch": batch_index,
                            "total_batches": len(batches),
                            "completed_characters": completed_characters,
                            "total_characters": total_characters,
                        },
                    )
                if first_error is not None:
                    for future in future_to_index:
                        future.cancel()
                    raise first_error
                fill_available_workers()
        except Exception:
            for future in future_to_index:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        drain_progress_events()
        return (
            [
                candidate
                for batch_index in range(1, len(batches) + 1)
                for candidate in candidates_by_index[batch_index]
            ],
            chunk_text_by_id,
        )

    def _restore_extraction_checkpoint(
        self,
        job: KnowledgeIngestionJob,
        batch_index: int,
        batch: KnowledgeExtractionBatch,
    ) -> _ExtractionBatchOutcome | None:
        checkpoint = KnowledgeExtractionBatchCheckpoint.objects.filter(
            job=job,
            batch_id=batch.batch_id,
            status=KnowledgeExtractionBatchCheckpoint.Status.SUCCEEDED,
            execution_checksum=self._extraction_execution_checksum(batch),
        ).first()
        if checkpoint is None:
            return None
        try:
            if not isinstance(checkpoint.candidates, list):
                raise ValueError("checkpoint candidates must be a list")
            if not isinstance(checkpoint.evidence_chunks, list):
                raise ValueError("checkpoint evidence chunks must be a list")
            candidates = [
                KnowledgeCandidate.model_validate(candidate)
                for candidate in checkpoint.candidates
            ]
            evidence_chunks = [
                ContentChunk.model_validate(chunk)
                for chunk in checkpoint.evidence_chunks
            ]
            KnowledgeExtractionResult(
                result_id=f"checkpoint-{batch.batch_id}",
                batch=KnowledgeExtractionBatch(
                    batch_id=f"checkpoint-validation-{batch.batch_id}",
                    course_id=batch.course_id,
                    chunks=evidence_chunks,
                    max_chars=batch.max_chars,
                ),
                candidates=candidates,
                status="succeeded",
                possibly_truncated=False,
                created_at=checkpoint.completed_at,
            )
        except (DomainError, TypeError, ValueError):
            KnowledgeExtractionBatchCheckpoint.objects.filter(
                pk=checkpoint.pk
            ).update(
                status=KnowledgeExtractionBatchCheckpoint.Status.FAILED,
                candidates=[],
                evidence_chunks=[],
                error_code="KNOWLEDGE_EXTRACTION_CHECKPOINT_INVALID",
                exception_type="CheckpointValidationError",
                completed_at=self._aware_now(),
            )
            return None
        if (
            checkpoint.batch_index != batch_index
            or checkpoint.character_count != batch.character_count
        ):
            return None
        return _ExtractionBatchOutcome(
            candidates=tuple(candidates),
            evidence_chunks=tuple(evidence_chunks),
        )

    def _persist_extraction_checkpoint(
        self,
        job: KnowledgeIngestionJob,
        batch_index: int,
        batch: KnowledgeExtractionBatch,
        *,
        candidates: list[KnowledgeCandidate],
        evidence_chunks: list[ContentChunk],
        retry_count: int,
        error: Exception | None = None,
    ) -> None:
        succeeded = error is None
        KnowledgeExtractionBatchCheckpoint.objects.update_or_create(
            job=job,
            batch_id=batch.batch_id,
            defaults={
                "batch_index": batch_index,
                "character_count": batch.character_count,
                "execution_checksum": self._extraction_execution_checksum(batch),
                "status": (
                    KnowledgeExtractionBatchCheckpoint.Status.SUCCEEDED
                    if succeeded
                    else KnowledgeExtractionBatchCheckpoint.Status.FAILED
                ),
                "candidates": (
                    [candidate.model_dump(mode="json") for candidate in candidates]
                    if succeeded
                    else []
                ),
                "evidence_chunks": (
                    [chunk.model_dump(mode="json") for chunk in evidence_chunks]
                    if succeeded
                    else []
                ),
                "retry_count": retry_count,
                "error_code": (
                    None if succeeded else _safe_ingestion_error_code(error)
                ),
                "exception_type": None if succeeded else type(error).__name__,
                "completed_at": self._aware_now(),
            },
        )

    def _extraction_execution_checksum(
        self,
        batch: KnowledgeExtractionBatch,
    ) -> str:
        payload = json.dumps(
            {
                "batch_id": batch.batch_id,
                "model_ref": self._model_ref.model_dump(mode="json"),
                "prompt_id": KNOWLEDGE_EXTRACTION_PROMPT_ID,
                "prompt_version": KNOWLEDGE_EXTRACTION_PROMPT_VERSION,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _extract_batch_safely(
        self,
        batch: KnowledgeExtractionBatch,
        *,
        depth: int,
        retry_reason: str | None,
        on_retry_progress: Callable[[int, str, int], None],
        on_model_retry: Callable[[int, int, str, int], None],
    ) -> _ExtractionBatchOutcome:
        try:
            result = self._extractor.extract(
                batch,
                model_ref=self._model_ref,
                created_at=self._aware_now(),
                on_retry=lambda attempt, limit, validation_code: on_model_retry(
                    attempt,
                    limit,
                    validation_code,
                    depth,
                ),
            )
        except DomainError as error:
            next_retry_reason = _recoverable_extraction_retry_reason(error)
            if next_retry_reason is None or depth >= 8:
                raise
            try:
                children = _bisect_extraction_batch(batch)
            except DomainError as split_error:
                if split_error.code == "KNOWLEDGE_EXTRACTION_OVERFLOW":
                    raise error from split_error
                raise
            child_outcomes = [
                self._extract_batch_safely(
                    child,
                    depth=depth + 1,
                    retry_reason=next_retry_reason,
                    on_retry_progress=on_retry_progress,
                    on_model_retry=on_model_retry,
                )
                for child in children
            ]
            return _merge_extraction_outcomes(child_outcomes)
        if not result.possibly_truncated:
            if retry_reason is not None:
                on_retry_progress(batch.character_count, retry_reason, depth)
            return _ExtractionBatchOutcome(
                candidates=tuple(result.candidates),
                evidence_chunks=tuple(batch.chunks),
            )
        if depth >= 8:
            raise DomainError(
                code="KNOWLEDGE_EXTRACTION_OVERFLOW",
                module="m7",
                message="knowledge extraction remains truncated after safe bisection",
            )
        children = _bisect_extraction_batch(batch)
        child_outcomes = [
            self._extract_batch_safely(
                child,
                depth=depth + 1,
                retry_reason="KNOWLEDGE_EXTRACTION_OVERFLOW",
                on_retry_progress=on_retry_progress,
                on_model_retry=on_model_retry,
            )
            for child in children
        ]
        return _merge_extraction_outcomes(child_outcomes)

    def _read_version(self, version: CourseSourceVersion) -> bytes:
        target = (self._storage_root / version.storage_key).resolve()
        if self._storage_root not in target.parents or not target.is_file():
            raise DomainError(
                code="KNOWLEDGE_SOURCE_MISSING",
                module="m1",
                message="knowledge source bytes are unavailable",
            )
        payload = target.read_bytes()
        if len(payload) != version.size_bytes or not hashlib.sha256(payload).hexdigest() == version.sha256:
            raise DomainError(
                code="KNOWLEDGE_SOURCE_HASH_MISMATCH",
                module="m1",
                message="knowledge source bytes do not match their immutable version",
            )
        return payload

    def _publish(
        self,
        *,
        job: KnowledgeIngestionJob,
        selected_versions: list[CourseSourceVersion],
        concepts: tuple[MergedKnowledgeConcept, ...],
        questions: list[_QuestionBuild],
        partial: bool,
        chunk_text_by_id: dict[str, str],
        successful_source_ids: tuple[str, ...],
        failed_source_ids: tuple[str, ...],
        failed_source_errors: dict[str, str],
        issue_count: int,
    ) -> CourseKnowledgeRelease:
        checksum = _release_checksum(concepts, questions)
        with transaction.atomic():
            locked_job = KnowledgeIngestionJob.objects.select_for_update().get(pk=job.pk)
            existing = CourseKnowledgeRelease.objects.filter(job=locked_job).first()
            if existing is not None:
                return existing
            active = list(
                CourseKnowledgeRelease.objects.select_for_update().filter(
                    course_id=job.course_id,
                    class_id=job.class_id,
                    status=CourseKnowledgeRelease.Status.ACTIVE,
                )
            )
            maximum = CourseKnowledgeRelease.objects.filter(
                course_id=job.course_id,
                class_id=job.class_id,
            ).aggregate(value=Max("version_number"))["value"] or 0
            release = CourseKnowledgeRelease.objects.create(
                course_id=job.course_id,
                class_id=job.class_id,
                version_number=maximum + 1,
                status=CourseKnowledgeRelease.Status.BUILDING,
                job=locked_job,
                content_checksum=checksum,
            )
            concept_rows: dict[str, ReleaseConcept] = {}
            versions = {str(version.pk): version for version in selected_versions}
            for concept in concepts:
                row = ReleaseConcept.objects.create(
                    release=release,
                    concept_id=concept.concept_id,
                    name=concept.name,
                    description=concept.description,
                    aliases=concept.aliases,
                )
                concept_rows[concept.concept_id] = row
                ReleaseConceptSource.objects.bulk_create(
                    [
                        ReleaseConceptSource(
                            concept=row,
                            source_version=versions[reference.source_version_id],
                            chunk_id=reference.chunk_id,
                            locator=reference.locator,
                            chunk_text=chunk_text_by_id[reference.chunk_id],
                            span_start=reference.span_start,
                            span_end=reference.span_end,
                            relation_type=reference.relation_type,
                        )
                        for reference in concept.evidence
                    ]
                )
            for build in questions:
                question = build.question
                question_row = ReleaseQuestion.objects.create(
                    release=release,
                    question_id=question.question_id,
                    source_version=build.source_version,
                    question_type=question.question_type,
                    ordinal=question.ordinal,
                    locator=question.locator,
                    stem=question.stem,
                    payload={
                        "options": question.options,
                        "accepted_answers": list(question.accepted_answers),
                        "rubric": question.rubric,
                        "explanation": question.explanation,
                    },
                )
                ReleaseQuestionConceptLink.objects.bulk_create(
                    [
                        ReleaseQuestionConceptLink(
                            question=question_row,
                            concept=concept_rows[link.concept_id],
                            confidence=link.confidence,
                            status=link.status,
                            evidence=[
                                reference.model_dump(mode="json")
                                for reference in link.evidence
                            ],
                        )
                        for link in build.links
                        if link.concept_id in concept_rows
                    ]
                )
            for previous in active:
                previous.status = CourseKnowledgeRelease.Status.RETIRED
                previous.save(update_fields=("status",))
            release.status = CourseKnowledgeRelease.Status.ACTIVE
            release.activated_at = self._aware_now()
            release.save(update_fields=("status", "activated_at"))
            workspace, _ = CourseClassWorkspace.objects.get_or_create(
                course_id=job.course_id,
                class_id=job.class_id,
            )
            workspace = CourseClassWorkspace.objects.select_for_update().get(
                pk=workspace.pk
            )
            workspace.active_release = release
            workspace.content_revision += 1
            workspace.save(
                update_fields=(
                    "active_release",
                    "content_revision",
                    "updated_at",
                )
            )
            _activate_source_versions(selected_versions)
            _finalize_deleted_sources(locked_job)
            locked_job.checkpoint = {
                "stage": "published",
                "release_id": str(release.pk),
                "successful_source_ids": list(successful_source_ids),
                "failed_source_ids": list(failed_source_ids),
                "failed_source_errors": failed_source_errors,
                "issue_count": issue_count,
            }
            locked_job.lease_until = None
            locked_job.save(update_fields=("checkpoint", "lease_until", "updated_at"))
            KnowledgeChangeOperation.objects.filter(
                job=locked_job,
                status=KnowledgeChangeOperation.Status.PENDING,
            ).update(
                status=KnowledgeChangeOperation.Status.SUCCEEDED,
                error_code=None,
            )
            for source_id, error_code in failed_source_errors.items():
                KnowledgeChangeOperation.objects.filter(
                    job=locked_job,
                    source_id=source_id,
                ).update(
                    status=KnowledgeChangeOperation.Status.FAILED,
                    error_code=error_code,
                )
            locked_job.transition_to(
                KnowledgeIngestionJob.Status.PARTIAL
                if partial
                else KnowledgeIngestionJob.Status.SUCCEEDED
            )
        job.refresh_from_db()
        return release

    def _fail_job(self, job: KnowledgeIngestionJob, error: Exception) -> None:
        job.refresh_from_db()
        if job.status == KnowledgeIngestionJob.Status.RUNNING:
            code = _safe_ingestion_error_code(error)
            try:
                KnowledgeChangeOperation.objects.filter(
                    job=job,
                    status=KnowledgeChangeOperation.Status.PENDING,
                ).update(
                    status=KnowledgeChangeOperation.Status.FAILED,
                    error_code=code,
                )
                job.lease_until = None
                job.save(update_fields=("lease_until", "updated_at"))
                job.transition_to(KnowledgeIngestionJob.Status.FAILED, error_code=code)
            except ValidationError:
                return

    def _replayed_outcome(
        self,
        job: KnowledgeIngestionJob,
        release: CourseKnowledgeRelease,
    ) -> KnowledgeIngestionOutcome:
        checkpoint = job.checkpoint if isinstance(job.checkpoint, dict) else {}
        return KnowledgeIngestionOutcome(
            job_id=str(job.pk),
            release_id=str(release.pk),
            status=job.status,
            successful_source_ids=tuple(checkpoint.get("successful_source_ids", ())),
            failed_source_ids=tuple(checkpoint.get("failed_source_ids", ())),
            issue_count=int(checkpoint.get("issue_count", 0)),
        )

    def _record_stage(
        self,
        job: KnowledgeIngestionJob,
        stage: str,
        progress: int,
        *,
        detail: dict[str, object] | None = None,
    ) -> None:
        checkpoint = {"stage": stage, **(detail or {})}
        current_progress = max(job.progress, progress)
        now = self._aware_now()
        updates: dict[str, object] = {
            "checkpoint": checkpoint,
            "progress": current_progress,
            "updated_at": now,
        }
        if job.worker_id:
            updates["lease_until"] = now + timedelta(minutes=5)
        KnowledgeIngestionJob.objects.filter(pk=job.pk).update(**updates)
        job.checkpoint = checkpoint
        job.progress = current_progress
        job.updated_at = now
        if "lease_until" in updates:
            job.lease_until = updates["lease_until"]
        self._heartbeat_callback()

    def _renew_lease(self, job: KnowledgeIngestionJob) -> None:
        if job.worker_id:
            now = self._aware_now()
            lease_until = now + timedelta(minutes=5)
            KnowledgeIngestionJob.objects.filter(
                pk=job.pk,
                status=KnowledgeIngestionJob.Status.RUNNING,
                worker_id=job.worker_id,
            ).update(lease_until=lease_until, updated_at=now)
            job.lease_until = lease_until
            job.updated_at = now
        self._heartbeat_callback()

    def _aware_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("knowledge ingestion clock must return an aware datetime")
        return value


def _preferred_version(source: CourseSource) -> CourseSourceVersion | None:
    versions = list(source.versions.all())
    staged = [version for version in versions if version.status == CourseSourceVersion.Status.STAGED]
    active = [version for version in versions if version.status == CourseSourceVersion.Status.ACTIVE]
    candidates = staged or active
    return max(candidates, key=lambda version: version.version_number, default=None)


def _is_question_only_change(
    operations: list[KnowledgeChangeOperation],
) -> bool:
    return bool(operations) and all(
        operation.source is not None
        and operation.source.source_type == CourseSource.SourceType.QUESTION
        for operation in operations
    )


def _is_knowledge_only_change(
    operations: list[KnowledgeChangeOperation],
) -> bool:
    return bool(operations) and all(
        operation.source is not None
        and operation.source.source_type == CourseSource.SourceType.KNOWLEDGE
        and operation.operation
        in {
            KnowledgeChangeOperation.Operation.UPSERT,
            KnowledgeChangeOperation.Operation.DELETE,
        }
        for operation in operations
    )


def _changed_knowledge_sources(
    operations: list[KnowledgeChangeOperation],
) -> list[CourseSource]:
    sources: dict[UUID, CourseSource] = {}
    for operation in operations:
        if (
            operation.operation == KnowledgeChangeOperation.Operation.UPSERT
            and operation.source is not None
        ):
            sources[operation.source.pk] = operation.source
    return list(sources.values())


def _changed_question_sources(
    operations: list[KnowledgeChangeOperation],
) -> list[CourseSource]:
    sources: dict[UUID, CourseSource] = {}
    for operation in operations:
        if (
            operation.operation == KnowledgeChangeOperation.Operation.DELETE
            or operation.source is None
        ):
            continue
        sources[operation.source.pk] = operation.source
    return list(sources.values())


def _snapshot_release_concepts(
    release: CourseKnowledgeRelease,
    course_id: str,
) -> tuple[
    tuple[MergedKnowledgeConcept, ...],
    dict[str, str],
    list[CourseSourceVersion],
]:
    concepts: list[MergedKnowledgeConcept] = []
    chunk_text_by_id: dict[str, str] = {}
    versions: list[CourseSourceVersion] = []
    rows = release.concepts.prefetch_related(
        "source_references__source_version"
    ).order_by("concept_id")
    for row in rows:
        references = sorted(
            row.source_references.all(),
            key=lambda reference: (
                str(reference.source_version_id),
                reference.chunk_id,
                reference.span_start,
                reference.span_end,
                reference.relation_type,
            ),
        )
        evidence = [
            KnowledgeEvidenceRef(
                source_id=str(reference.source_version_id),
                source_version_id=str(reference.source_version_id),
                chunk_id=reference.chunk_id,
                locator=reference.locator,
                span_start=reference.span_start,
                span_end=reference.span_end,
                relation_type=reference.relation_type,
            )
            for reference in references
        ]
        concepts.append(
            MergedKnowledgeConcept(
                concept_id=row.concept_id,
                course_id=course_id,
                name=row.name,
                description=row.description,
                aliases=list(row.aliases),
                evidence=evidence,
            )
        )
        for reference in references:
            chunk_text_by_id[reference.chunk_id] = reference.chunk_text
            versions.append(reference.source_version)
    return tuple(concepts), chunk_text_by_id, _unique_versions(versions)


def _snapshot_release_questions(
    release: CourseKnowledgeRelease,
    *,
    excluded_source_ids: set[UUID],
) -> tuple[_QuestionBuild, ...]:
    rows = (
        release.questions.select_related("source_version")
        .prefetch_related("concept_links__concept")
        .exclude(source_version__source_id__in=excluded_source_ids)
        .order_by("source_version_id", "ordinal", "question_id")
    )
    builds: list[_QuestionBuild] = []
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        question = ParsedQuestion(
            source_id=str(row.source_version.source_id),
            question_id=row.question_id,
            question_type=row.question_type,
            stem=row.stem,
            options=dict(payload.get("options", {})),
            accepted_answers=tuple(payload.get("accepted_answers", ())),
            rubric=payload.get("rubric"),
            explanation=str(payload.get("explanation", "")),
            ordinal=row.ordinal,
            locator=row.locator,
        )
        links = tuple(
            QuestionConceptLinkCandidate(
                question_id=row.question_id,
                concept_id=link.concept.concept_id,
                confidence=float(link.confidence),
                status=link.status,
                evidence=[
                    KnowledgeEvidenceRef.model_validate(reference)
                    for reference in link.evidence
                ],
            )
            for link in sorted(
                row.concept_links.all(),
                key=lambda candidate: candidate.concept.concept_id,
            )
        )
        builds.append(_QuestionBuild(question, row.source_version, links))
    return tuple(builds)


def _concept_as_candidate(concept: MergedKnowledgeConcept) -> KnowledgeCandidate:
    return KnowledgeCandidate(
        candidate_id=f"retained-{concept.concept_id}",
        name=concept.name,
        description=concept.description,
        aliases=list(concept.aliases),
        evidence=list(concept.evidence),
    )


def _filter_concept_evidence(
    concepts: tuple[MergedKnowledgeConcept, ...],
    *,
    excluded_version_ids: set[str],
) -> tuple[MergedKnowledgeConcept, ...]:
    retained: list[MergedKnowledgeConcept] = []
    for concept in concepts:
        evidence = [
            reference
            for reference in concept.evidence
            if reference.source_version_id not in excluded_version_ids
        ]
        if not evidence:
            continue
        retained.append(
            MergedKnowledgeConcept(
                concept_id=concept.concept_id,
                course_id=concept.course_id,
                name=concept.name,
                description=concept.description,
                aliases=list(concept.aliases),
                evidence=evidence,
            )
        )
    return tuple(retained)


def _refresh_question_link_evidence(
    questions: list[_QuestionBuild],
    concepts: tuple[MergedKnowledgeConcept, ...],
) -> list[_QuestionBuild]:
    concepts_by_id = {concept.concept_id: concept for concept in concepts}
    refreshed: list[_QuestionBuild] = []
    for build in questions:
        links = tuple(
            QuestionConceptLinkCandidate(
                question_id=link.question_id,
                concept_id=link.concept_id,
                confidence=link.confidence,
                status=link.status,
                evidence=list(concepts_by_id[link.concept_id].evidence),
            )
            for link in build.links
            if link.concept_id in concepts_by_id
        )
        refreshed.append(_QuestionBuild(build.question, build.source_version, links))
    return refreshed


def _unique_versions(
    versions: list[CourseSourceVersion],
) -> list[CourseSourceVersion]:
    unique: dict[UUID, CourseSourceVersion] = {}
    for version in versions:
        unique[version.pk] = version
    return list(unique.values())


def _active_version(source: CourseSource, *, excluding: UUID) -> CourseSourceVersion | None:
    return max(
        (
            version
            for version in source.versions.all()
            if version.status == CourseSourceVersion.Status.ACTIVE and version.pk != excluding
        ),
        key=lambda version: version.version_number,
        default=None,
    )


def _activate_source_versions(versions: list[CourseSourceVersion]) -> None:
    for version in versions:
        CourseSourceVersion.objects.filter(
            source_id=version.source_id,
            status=CourseSourceVersion.Status.ACTIVE,
        ).exclude(pk=version.pk).update(status=CourseSourceVersion.Status.RETIRED)
        CourseSourceVersion.objects.filter(pk=version.pk).update(
            status=CourseSourceVersion.Status.ACTIVE
        )
        CourseSource.objects.filter(pk=version.source_id).update(
            status=CourseSource.Status.ACTIVE
        )


def _finalize_deleted_sources(job: KnowledgeIngestionJob) -> None:
    source_ids = tuple(
        job.operations.filter(
            operation=KnowledgeChangeOperation.Operation.DELETE,
        ).values_list("source_id", flat=True)
    )
    if not source_ids:
        return
    CourseSourceVersion.objects.filter(source_id__in=source_ids).update(
        status=CourseSourceVersion.Status.DELETED
    )
    CourseSource.objects.filter(
        pk__in=source_ids,
        status=CourseSource.Status.PENDING_DELETE,
    ).update(status=CourseSource.Status.DELETED)


def _release_checksum(
    concepts: tuple[MergedKnowledgeConcept, ...],
    questions: list[_QuestionBuild],
) -> str:
    payload = {
        "concepts": [concept.model_dump(mode="json") for concept in concepts],
        "questions": [
            {
                "source_version_id": str(build.source_version.pk),
                "question": {
                    "question_id": build.question.question_id,
                    "source_id": build.question.source_id,
                    "question_type": build.question.question_type,
                    "stem": build.question.stem,
                    "options": build.question.options,
                    "accepted_answers": list(build.question.accepted_answers),
                    "rubric": build.question.rubric,
                    "explanation": build.question.explanation,
                    "ordinal": build.question.ordinal,
                    "locator": build.question.locator,
                },
                "links": [link.model_dump(mode="json") for link in build.links],
            }
            for build in questions
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
