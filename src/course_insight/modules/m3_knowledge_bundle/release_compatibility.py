"""Compatibility projection from new releases into existing M3-M9 contracts."""

from __future__ import annotations

import re
from typing import TypedDict

from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import (
    AssessmentBlueprint,
    BlueprintSection,
    ItemCard,
    KnowledgeBundle,
    KnowledgeConcept,
    QMatrixEntry,
    ReviewPolicy,
    Rubric,
    RubricCriterion,
)
from course_insight.modules.m0_platform.django_app.models import (
    CourseKnowledgeRelease,
)


class ReleaseQualitySummary(TypedDict):
    release_id: str
    concept_count: int
    source_reference_count: int
    question_count: int
    usable_question_link_count: int
    needs_review_link_count: int
    isolated_concept_count: int


def knowledge_bundle_from_release(
    release: CourseKnowledgeRelease,
) -> KnowledgeBundle:
    """Project one frozen active or retired release into the legacy contract."""

    if release.status not in {
        CourseKnowledgeRelease.Status.ACTIVE,
        CourseKnowledgeRelease.Status.RETIRED,
    }:
        raise DomainError(
            code="KNOWLEDGE_RELEASE_NOT_PUBLISHED",
            module="m3",
            message="only active or retired releases can be consumed downstream",
        )
    concept_rows = list(
        release.concepts.prefetch_related("source_references").order_by("concept_id")
    )
    concepts = [
        KnowledgeConcept(
            concept_id=row.concept_id,
            name=row.name,
            chapter_id=f"release-{release.version_number}",
            description=row.description,
            aliases=list(row.aliases),
            status="teacher_approved",
        )
        for row in concept_rows
    ]
    evidence_by_concept = {
        row.concept_id: [
            f"evidence-{reference.pk}"
            for reference in row.source_references.all()
        ]
        for row in concept_rows
    }

    items: list[ItemCard] = []
    rubrics: list[Rubric] = []
    q_matrix: list[QMatrixEntry] = []
    question_rows = list(
        release.questions.prefetch_related(
            "concept_links__concept",
            "concept_links__concept__source_references",
        ).order_by("source_version_id", "ordinal", "question_id")
    )
    for question in question_rows:
        links = [
            link
            for link in question.concept_links.all()
            if link.status == "usable"
        ]
        if not links:
            continue
        concept_ids = sorted({link.concept.concept_id for link in links})
        source_evidence_ids = sorted(
            {
                evidence_id
                for concept_id in concept_ids
                for evidence_id in evidence_by_concept.get(concept_id, [])
            }
        )
        item_version = (
            f"release-{release.version_number}."
            f"source-{question.source_version.version_number}"
        )
        rubric_id: str | None = None
        if question.question_type == "subjective":
            rubric_id = f"rubric-{release.pk}-{question.question_id}"
            rubrics.append(
                _subjective_rubric(
                    rubric_id,
                    str(question.payload.get("rubric") or "按课程依据完整作答。"),
                    source_evidence_ids,
                )
            )
        answer_key = {
            "answers": list(question.payload.get("accepted_answers", [])),
            "options": dict(question.payload.get("options", {})),
            "explanation": str(question.payload.get("explanation") or ""),
            "max_score": 1.0,
        }
        item = ItemCard(
            item_id=question.question_id,
            version=item_version,
            stem=question.stem,
            item_type=_legacy_item_type(question.question_type),
            concept_ids=concept_ids,
            misconception_ids=[],
            difficulty_level=1,
            cognitive_level="understand",
            parameter_rules=[],
            answer_key=answer_key,
            rubric_id=rubric_id,
            source_evidence_ids=source_evidence_ids,
            status="teacher_approved",
        )
        items.append(item)
        q_matrix.extend(
            QMatrixEntry(
                item_id=item.item_id,
                item_version=item.version,
                concept_id=link.concept.concept_id,
                weight=float(link.confidence),
            )
            for link in sorted(links, key=lambda value: value.concept.concept_id)
        )

    blueprints: list[AssessmentBlueprint] = []
    if items:
        total_score = sum(item.max_score(_bundle_for_score(rubrics)) for item in items)
        blueprint_id = f"blueprint-{release.pk}"
        blueprints.append(
            AssessmentBlueprint(
                blueprint_id=blueprint_id,
                version=str(release.version_number),
                course_id=release.course_id,
                sections=[
                    BlueprintSection(
                        section_id=f"section-{release.pk}",
                        name="当前发布题目",
                        item_count=len(items),
                        score=total_score,
                        purpose="anchor",
                        item_types=sorted({item.item_type for item in items}),
                        concept_weights={},
                        difficulty_range=(0, 5),
                        anchor_item_ids=[],
                    )
                ],
                total_score=total_score,
                duration_minutes=max(10, len(items) * 5),
                status="teacher_approved",
            )
        )

    return KnowledgeBundle(
        knowledge_bundle_id=str(release.pk),
        course_package_id=f"course-release-{release.pk}",
        course_id=release.course_id,
        bundle_version=str(release.version_number),
        concepts=concepts,
        prerequisite_relations=[],
        misconception_tags=[],
        items=items,
        rubrics=rubrics,
        blueprints=blueprints,
        q_matrix=q_matrix,
        status="published",
        published_at=release.activated_at or release.created_at,
        course_package_checksum=release.content_checksum,
        concept_evidence_ids=evidence_by_concept,
    )


def summarize_release_quality(
    release: CourseKnowledgeRelease,
) -> ReleaseQualitySummary:
    """Return M9-ready quality counts without resurrecting approval state."""

    concept_rows = list(release.concepts.prefetch_related("question_links"))
    return {
        "release_id": str(release.pk),
        "concept_count": len(concept_rows),
        "source_reference_count": sum(
            concept.source_references.count() for concept in concept_rows
        ),
        "question_count": release.questions.count(),
        "usable_question_link_count": sum(
            concept.question_links.filter(status="usable").count()
            for concept in concept_rows
        ),
        "needs_review_link_count": sum(
            concept.question_links.filter(status="needs_review").count()
            for concept in concept_rows
        ),
        "isolated_concept_count": sum(
            not concept.question_links.filter(status="usable").exists()
            for concept in concept_rows
        ),
    }


def _legacy_item_type(question_type: str) -> str:
    return {
        "choice": "multiple_choice",
        "fill_blank": "fill_blank",
        "subjective": "subjective",
    }[question_type]


def _subjective_rubric(
    rubric_id: str,
    rubric_text: str,
    evidence_ids: list[str],
) -> Rubric:
    match = re.search(r"(\d+(?:\.\d+)?)\s*分", rubric_text)
    total = float(match.group(1)) if match else 10.0
    return Rubric(
        rubric_id=rubric_id,
        version="1",
        total_score=total,
        criteria=[
            RubricCriterion(
                criterion_id=f"criterion-{rubric_id}",
                description=rubric_text,
                max_score=total,
                expected_student_evidence=rubric_text,
                course_evidence_ids=evidence_ids,
            )
        ],
        review_policy=ReviewPolicy(
            low_confidence_threshold=0.7,
            double_score_disagreement_threshold=1.0,
            require_evidence_for_positive_score=True,
        ),
        status="teacher_approved",
    )


def _bundle_for_score(rubrics: list[Rubric]) -> KnowledgeBundle:
    """Minimal private resolver used only by ItemCard.max_score."""

    return KnowledgeBundle(
        knowledge_bundle_id="score-helper",
        course_package_id="score-helper",
        course_id="score-helper",
        bundle_version="1",
        concepts=[],
        prerequisite_relations=[],
        misconception_tags=[],
        items=[],
        rubrics=rubrics,
        blueprints=[],
        q_matrix=[],
        status="draft",
        published_at=None,
    )
