"""Django-side binding of the current M5 snapshot to published sources."""

from __future__ import annotations

from collections import defaultdict

from course_insight.modules.m0_platform.django_app.models import (
    ClassLearningSnapshot,
    CourseSource,
    CourseSourceVersion,
    ReleaseConceptSource,
)
from course_insight.modules.m5_learner_class_state.class_snapshot import (
    ClassLearningSnapshotView,
)
from course_insight.modules.m9_teacher_analytics.teaching_advice import (
    TeachingAdviceConcept,
    TeachingAdvicePrompt,
    TeachingAdviceSource,
    build_teaching_advice_prompt,
)


_MAX_SOURCES_PER_CONCEPT = 2
_MAX_SOURCE_EXCERPT_CHARACTERS = 1_200


def build_class_teaching_advice_prompt(
    *,
    snapshot: ClassLearningSnapshotView,
    weak_mastery_threshold: float,
) -> TeachingAdvicePrompt:
    """Bind one persistent M5 snapshot to its active course source excerpts.

    The snapshot ID prevents an old release's sources from being mixed into a
    newly synchronized class view.  Only still-published knowledge sources are
    included in the teacher-triggered external request.
    """

    persisted = (
        ClassLearningSnapshot.objects.select_related("release")
        .filter(
            snapshot_id=snapshot.snapshot_id,
            workspace__course_id=snapshot.course_id,
            workspace__class_id=snapshot.class_id,
        )
        .first()
    )
    if persisted is None:
        raise ValueError("class learning snapshot is unavailable")
    release = persisted.release
    if release is None:
        raise ValueError("class learning snapshot has no published release")

    sources_by_concept: dict[str, list[TeachingAdviceSource]] = defaultdict(list)
    references = (
        ReleaseConceptSource.objects.filter(
            concept__release=release,
            source_version__status=CourseSourceVersion.Status.ACTIVE,
            source_version__source__course_id=snapshot.course_id,
            source_version__source__class_id=snapshot.class_id,
            source_version__source__source_type=CourseSource.SourceType.KNOWLEDGE,
            source_version__source__status=CourseSource.Status.ACTIVE,
        )
        .select_related("concept", "source_version__source")
        .order_by(
            "concept__concept_id",
            "source_version__source__display_name",
            "locator",
            "pk",
        )
    )
    for reference in references:
        concept_id = reference.concept.concept_id
        if len(sources_by_concept[concept_id]) >= _MAX_SOURCES_PER_CONCEPT:
            continue
        text = reference.chunk_text.strip()
        if not text:
            continue
        sources_by_concept[concept_id].append(
            TeachingAdviceSource(
                source_id=f"source_{reference.pk}",
                locator=reference.locator.strip(),
                text=text[:_MAX_SOURCE_EXCERPT_CHARACTERS],
            )
        )

    return build_teaching_advice_prompt(
        concepts=tuple(
            TeachingAdviceConcept(
                concept_id=row.concept_id,
                name=row.name,
                average_mastery=row.average_mastery,
                attempted_student_count=row.attempted_student_count,
                unattempted_student_count=row.unattempted_student_count,
                priority_support_rate=row.priority_support_rate,
                sources=tuple(sources_by_concept[row.concept_id]),
            )
            for row in snapshot.concepts
        ),
        weak_mastery_threshold=weak_mastery_threshold,
    )


__all__ = ["build_class_teaching_advice_prompt"]
