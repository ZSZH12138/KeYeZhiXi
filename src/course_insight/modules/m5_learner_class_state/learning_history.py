"""Deterministic complete-history assembly for M5 model inference."""

from __future__ import annotations

import hashlib
import json

from course_insight.contracts.errors import DomainError
from course_insight.contracts.learning_models import LearningObservationBatch
from course_insight.modules.m5_learner_class_state.learning_observation_evidence import (
    select_current_learning_observations,
)
from course_insight.modules.m5_learner_class_state.repository import M5Repository


def build_complete_history_batch(
    repository: M5Repository,
    current_batch: LearningObservationBatch,
    *,
    course_id: str,
    class_id: str,
) -> LearningObservationBatch:
    """Load and freeze one learner's full governed observation history."""

    reader = getattr(repository, "list_learning_observations", None)
    stored = (
        reader(course_id=course_id, class_id=class_id)
        if callable(reader)
        else current_batch.observations
    )
    if any(
        observation.course_id != course_id or observation.class_id != class_id
        for observation in stored
    ):
        raise DomainError(
            code="MODEL_SCOPE_MISMATCH",
            module="m5",
            message="persisted learning observations do not match model scope",
            recoverable=True,
        )
    learner_history = select_current_learning_observations(
        observation.model_copy(deep=True)
        for observation in stored
        if observation.learner_id == current_batch.learner_id
    )
    learner_history = sorted(
        learner_history,
        key=lambda item: (item.occurred_at, item.attempt_id, item.observation_id),
    )
    if not learner_history:
        raise DomainError(
            code="INSUFFICIENT_MODEL_DATA",
            module="m5",
            message="persisted learner history is empty after observation storage",
            recoverable=True,
        )
    history_payload = [
        observation.model_dump(mode="json") for observation in learner_history
    ]
    digest = hashlib.sha256(
        json.dumps(history_payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    created_at = max(
        current_batch.created_at,
        *(observation.occurred_at for observation in learner_history),
    )
    return LearningObservationBatch(
        batch_id=f"history_batch_{digest[:24]}",
        learner_id=current_batch.learner_id,
        observations=learner_history,
        watermark=f"history-{digest[:20]}",
        created_at=created_at,
    )


__all__ = ["build_complete_history_batch"]
