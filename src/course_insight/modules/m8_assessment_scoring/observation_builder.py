"""Build traceable model observations from frozen M8 evidence."""

from __future__ import annotations

from course_insight.contracts.assessment import (
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.learning_models import (
    LearningObservation,
    LearningObservationBatch,
)
from course_insight.modules.m8_assessment_scoring.paper_record import (
    DEFAULT_MODEL_OBSERVATION_POLICY,
    FrozenAssessmentRecord,
    ModelObservationPolicy,
)


def build_observation_batch(
    record: FrozenAssessmentRecord,
    bundle: ScoringResultBundle,
    *,
    observation_policy: ModelObservationPolicy = DEFAULT_MODEL_OBSERVATION_POLICY,
) -> LearningObservationBatch:
    """Convert current audit versions without guessing an item from its name."""

    if (
        bundle.paper_id != record.paper.paper_id
        or bundle.learner_id != record.paper.learner_id
    ):
        raise DomainError(
            code="OBSERVATION_SOURCE_MISMATCH",
            module="m8",
            message="scoring result does not belong to the frozen paper",
        )
    latest_by_audit: dict[str, ScoreAuditRecord] = {}
    for audit in bundle.score_audit_records:
        current = latest_by_audit.get(audit.audit_id)
        if current is None or audit.audit_version > current.audit_version:
            latest_by_audit = {**latest_by_audit, audit.audit_id: audit}
    latest_by_item: dict[str, ScoreAuditRecord] = {}
    for audit in latest_by_audit.values():
        if audit.item_instance_id in latest_by_item:
            raise DomainError(
                code="OBSERVATION_SOURCE_MISMATCH",
                module="m8",
                message="multiple current audits refer to one paper item",
                details={"item_instance_id": audit.item_instance_id},
            )
        latest_by_item = {**latest_by_item, audit.item_instance_id: audit}

    items = record.paper.all_items()
    expected_ids = {item.item_instance_id for item in items}
    if set(latest_by_item) != expected_ids:
        raise DomainError(
            code="OBSERVATION_SOURCE_MISMATCH",
            module="m8",
            message="current score audits must cover every frozen paper item",
            details={"paper_id": record.paper.paper_id},
        )

    observations = []
    for item in items:
        audit = latest_by_item[item.item_instance_id]
        item_type = "subjective" if item.is_subjective() else "objective"
        observations.append(
            LearningObservation(
                observation_id=f"obs_{audit.audit_id}_v{audit.audit_version}",
                learner_id=bundle.learner_id,
                course_id=record.course_id,
                class_id=record.class_id,
                attempt_id=bundle.attempt_id,
                item_id=item.item_id,
                item_version=item.item_version,
                concept_ids=list(item.concept_ids),
                score=audit.total_score,
                max_score=audit.max_score,
                response_outcome=observation_policy.classify(
                    score=audit.total_score,
                    max_score=audit.max_score,
                    item_type=item_type,
                ),
                outcome_policy_version=observation_policy.version,
                source_audit_id=audit.audit_id,
                source_audit_version=audit.audit_version,
                occurred_at=audit.created_at,
            )
        )
    watermark = bundle.content_checksum()
    return LearningObservationBatch(
        batch_id=f"batch_{bundle.attempt_id}_{watermark[:16]}",
        learner_id=bundle.learner_id,
        observations=observations,
        watermark=watermark,
        created_at=bundle.finalized_at,
    )


__all__ = ["build_observation_batch"]
