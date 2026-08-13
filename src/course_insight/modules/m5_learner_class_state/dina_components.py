"""Connected-component helpers for bounded DINA fitting and inference."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Set
from datetime import datetime
from typing import Literal, TypeAlias

from course_insight.contracts.errors import DomainError
from course_insight.contracts.learning_models import (
    DinaModelArtifact,
    LearningObservationBatch,
)


ItemKey: TypeAlias = tuple[str, str]
PreparedData: TypeAlias = tuple[
    str,
    str,
    datetime,
    list[str],
    dict[ItemKey, frozenset[str]],
    dict[str, list[tuple[ItemKey, bool]]],
    dict[ItemKey, int],
]
InferenceMode: TypeAlias = Literal["exact", "variational"]


def validate_inference_scope(
    model: DinaModelArtifact,
    batch: LearningObservationBatch,
) -> None:
    """Require every inference observation to match model and learner scope."""

    if any(
        observation.course_id != model.course_id
        or observation.class_id != model.class_id
        or observation.learner_id != batch.learner_id
        for observation in batch.observations
    ):
        raise DomainError(
            code="MODEL_SCOPE_MISMATCH",
            module="m5",
            message="DINA inference batch does not match the model scope",
            recoverable=True,
        )


def connected_components(
    requirements: Mapping[ItemKey, Set[str]],
) -> list[frozenset[str]]:
    """Return stable concept components connected by common items."""

    adjacency: dict[str, set[str]] = defaultdict(set)
    for required in requirements.values():
        for concept_id in required:
            adjacency[concept_id].update(required - {concept_id})
    remaining = set(adjacency)
    components: list[frozenset[str]] = []
    while remaining:
        seed = min(remaining)
        pending = [seed]
        component: set[str] = set()
        while pending:
            concept_id = pending.pop()
            if concept_id in component:
                continue
            component.add(concept_id)
            pending.extend(sorted(adjacency[concept_id] - component, reverse=True))
        remaining -= component
        components.append(frozenset(component))
    return sorted(components, key=lambda item: tuple(sorted(item)))


def component_data(
    prepared: PreparedData,
    component: frozenset[str],
) -> PreparedData:
    """Project prepared training evidence onto one connected component."""

    (
        course_id,
        class_id,
        created_at,
        _concept_ids,
        requirements,
        responses_by_learner,
        item_counts,
    ) = prepared
    component_requirements = {
        item_key: required
        for item_key, required in requirements.items()
        if required <= component
    }
    component_items = set(component_requirements)
    component_responses = {
        learner_id: filtered
        for learner_id, responses in responses_by_learner.items()
        if (
            filtered := [
                response
                for response in responses
                if response[0] in component_items
            ]
        )
    }
    return (
        course_id,
        class_id,
        created_at,
        sorted(component),
        component_requirements,
        component_responses,
        {
            item_key: item_counts[item_key]
            for item_key in sorted(component_requirements)
        },
    )


def merge_component_models(
    prepared: PreparedData,
    component_models: list[DinaModelArtifact],
) -> DinaModelArtifact:
    """Merge independently fitted components into one immutable artifact."""

    course_id, class_id, created_at, concept_ids, _, responses, item_counts = prepared
    variational_models = [
        model
        for model in component_models
        if model.inference_mode == "variational"
    ]
    objective_history = _combined_objective_history(variational_models)
    item_parameters = sorted(
        (
            item
            for model in component_models
            for item in model.item_parameters
        ),
        key=lambda item: (item.item_id, item.item_version),
    )
    attribute_priors = {
        concept_id: model.attribute_priors[concept_id]
        for model in component_models
        for concept_id in model.concept_ids
    }
    model_payload = {
        "course_id": course_id,
        "class_id": class_id,
        "concept_ids": concept_ids,
        "item_parameters": [item.model_dump(mode="json") for item in item_parameters],
        "attribute_priors": {
            concept_id: attribute_priors[concept_id] for concept_id in concept_ids
        },
        "learner_count": len(responses),
        "observation_count": sum(item_counts.values()),
        "inference_mode": "variational" if variational_models else "exact",
        "log_likelihood": round(
            math.fsum(model.log_likelihood for model in component_models),
            12,
        ),
        "elbo": (
            round(
                math.fsum(
                    model.elbo
                    for model in variational_models
                    if model.elbo is not None
                ),
                12,
            )
            if variational_models
            else None
        ),
        "objective_history": objective_history,
        "iteration_count": max(model.iteration_count for model in component_models),
        "converged": all(model.converged for model in component_models),
    }
    digest = hashlib.sha256(
        json.dumps(model_payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return DinaModelArtifact(
        model_id=f"dina_{digest[:24]}",
        model_version=f"dina-{digest[:16]}",
        created_at=created_at,
        **model_payload,
    )


def component_model(
    model: DinaModelArtifact,
    component: frozenset[str],
    *,
    inference_mode: InferenceMode,
) -> DinaModelArtifact:
    """Project a merged model artifact onto one inference component."""

    item_parameters = [
        item
        for item in model.item_parameters
        if set(item.concept_ids) <= component
    ]
    return DinaModelArtifact(
        model_id=model.model_id,
        course_id=model.course_id,
        class_id=model.class_id,
        model_version=model.model_version,
        concept_ids=sorted(component),
        item_parameters=item_parameters,
        attribute_priors={
            concept_id: model.attribute_priors[concept_id]
            for concept_id in sorted(component)
        },
        learner_count=model.learner_count,
        observation_count=sum(item.sample_size for item in item_parameters),
        inference_mode=inference_mode,
        log_likelihood=model.log_likelihood,
        elbo=model.elbo if inference_mode == "variational" else None,
        objective_history=(
            model.objective_history if inference_mode == "variational" else []
        ),
        iteration_count=model.iteration_count,
        converged=model.converged,
        created_at=model.created_at,
    )


def _combined_objective_history(
    variational_models: list[DinaModelArtifact],
) -> list[float]:
    if not variational_models:
        return []
    history_length = max(len(model.objective_history) for model in variational_models)
    return [
        round(
            math.fsum(
                model.objective_history[min(index, len(model.objective_history) - 1)]
                for model in variational_models
            ),
            12,
        )
        for index in range(history_length)
    ]
