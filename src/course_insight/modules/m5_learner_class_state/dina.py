"""Deterministic-input DINA cognitive-diagnosis primitives and fitting."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Set
from typing import TypeAlias

from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import QMatrixEntry
from course_insight.contracts.learning_models import (
    CognitiveDiagnosisResult,
    DinaItemParameters,
    DinaModelArtifact,
    LearningObservation,
    LearningObservationBatch,
)
from course_insight.modules.m5_learner_class_state.dina_components import (
    InferenceMode,
    PreparedData,
    component_data,
    component_model,
    connected_components,
    merge_component_models,
    validate_inference_scope,
)
from course_insight.modules.m5_learner_class_state.learning_observation_evidence import (
    select_current_learning_observations,
)


ItemKey: TypeAlias = tuple[str, str]
Profile: TypeAlias = tuple[bool, ...]

def _clip(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


def _logsumexp(values: list[float]) -> float:
    maximum = max(values)
    return maximum + math.log(math.fsum(math.exp(value - maximum) for value in values))


class DinaEngine:
    """Fit and apply the deterministic-input noisy-AND-gate model."""

    def __init__(
        self,
        *,
        min_students: int = 200,
        min_responses_per_item: int = 50,
        max_iterations: int = 200,
        tolerance: float = 1e-6,
        max_exact_concepts: int = 12,
    ) -> None:
        if (
            min_students < 1
            or min_responses_per_item < 1
            or max_iterations < 1
            or max_exact_concepts < 1
        ):
            raise ValueError("DINA fitting thresholds must be positive")
        self._min_students = min_students
        self._min_responses_per_item = min_responses_per_item
        self._max_iterations = max_iterations
        self._tolerance = tolerance
        self._max_exact_concepts = max_exact_concepts

    @staticmethod
    def require_converged(model: DinaModelArtifact) -> None:
        """Reject model artifacts that are not safe to publish or apply."""

        if not model.converged:
            raise DomainError(
                code="MODEL_NOT_CONVERGED",
                module="m5",
                message="DINA inference requires a converged model",
                details={
                    "model_type": "DINA",
                    "model_version": model.model_version,
                },
                recoverable=True,
            )

    @staticmethod
    def _current_inference_batch(
        batch: LearningObservationBatch,
    ) -> LearningObservationBatch:
        """Apply the same immutable latest-audit rule used during training."""

        observations = select_current_learning_observations(batch.observations)
        return batch.model_copy(
            update={
                "observations": [
                    observation.model_copy(deep=True)
                    for observation in observations
                ]
            },
            deep=True,
        )

    @staticmethod
    def response_probability(
        *,
        capable: bool,
        slip: float,
        guess: float,
    ) -> float:
        """Return the DINA probability of a correct response."""

        return 1.0 - slip if capable else guess

    @staticmethod
    def is_capable(
        *,
        profile: Mapping[str, bool],
        required: Set[str],
    ) -> bool:
        """Apply DINA's conjunctive mastery gate."""

        return all(profile.get(concept_id, False) for concept_id in required)

    def fit(
        self,
        cohort: list[LearningObservationBatch],
        q_matrix: list[QMatrixEntry],
    ) -> DinaModelArtifact:
        """Fit DINA independently for each connected Q-matrix component."""

        prepared = self._prepare_training_data(cohort, q_matrix)
        requirements = prepared[4]
        components = connected_components(requirements)
        component_models = [
            self._fit_prepared(component_data(prepared, component))
            for component in components
        ]
        if len(component_models) == 1:
            return component_models[0]
        return merge_component_models(prepared, component_models)

    def _fit_prepared(self, prepared: PreparedData) -> DinaModelArtifact:
        if len(prepared[3]) > self._max_exact_concepts:
            return self._fit_variational(prepared)
        return self._fit_exact(prepared)

    def _fit_exact(self, prepared: PreparedData) -> DinaModelArtifact:
        (
            course_id,
            class_id,
            created_at,
            concept_ids,
            requirements,
            responses_by_learner,
            item_counts,
        ) = prepared
        profiles = list(itertools.product((False, True), repeat=len(concept_ids)))
        priors = {concept_id: 0.5 for concept_id in concept_ids}
        item_estimates = {
            item_key: [0.15, 0.20] for item_key in sorted(requirements)
        }
        previous_likelihood: float | None = None
        converged = False
        posterior_by_learner: dict[str, dict[Profile, float]] = {}
        log_likelihood = float("-inf")

        for iteration in range(1, self._max_iterations + 1):
            posterior_by_learner, log_likelihood = self._exact_expectation(
                concept_ids=concept_ids,
                profiles=profiles,
                priors=priors,
                item_estimates=item_estimates,
                requirements=requirements,
                responses_by_learner=responses_by_learner,
            )
            priors, item_estimates = self._maximization(
                concept_ids=concept_ids,
                posterior_by_learner=posterior_by_learner,
                item_estimates=item_estimates,
                requirements=requirements,
                responses_by_learner=responses_by_learner,
            )
            if (
                previous_likelihood is not None
                and abs(log_likelihood - previous_likelihood) < self._tolerance
            ):
                converged = True
                break
            previous_likelihood = log_likelihood

        model_payload = {
            "course_id": course_id,
            "class_id": class_id,
            "concept_ids": concept_ids,
            "item_parameters": [
                {
                    "item_id": item_key[0],
                    "item_version": item_key[1],
                    "concept_ids": sorted(requirements[item_key]),
                    "slip": round(item_estimates[item_key][0], 12),
                    "guess": round(item_estimates[item_key][1], 12),
                    "sample_size": item_counts[item_key],
                }
                for item_key in sorted(requirements)
            ],
            "attribute_priors": {
                concept_id: round(priors[concept_id], 12)
                for concept_id in concept_ids
            },
            "learner_count": len(responses_by_learner),
            "observation_count": sum(item_counts.values()),
            "inference_mode": "exact",
            "log_likelihood": round(log_likelihood, 12),
            "objective_history": [],
            "iteration_count": iteration,
            "converged": converged,
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
            elbo=None,
            **model_payload,
        )

    def profile_posterior(
        self,
        model: DinaModelArtifact,
        batch: LearningObservationBatch,
    ) -> dict[Profile, float]:
        """Return normalized exact mastery-profile probabilities."""

        self.require_converged(model)
        batch = self._current_inference_batch(batch)
        validate_inference_scope(model, batch)
        if model.inference_mode != "exact":
            raise DomainError(
                code="DINA_INFERENCE_MODE_MISMATCH",
                module="m5",
                message="exact profile posterior requires an exact DINA model",
            )
        concept_ids = list(model.concept_ids)
        requirements = {
            (item.item_id, item.item_version): frozenset(item.concept_ids)
            for item in model.item_parameters
        }
        estimates = {
            (item.item_id, item.item_version): [item.slip, item.guess]
            for item in model.item_parameters
        }
        responses = [
            (key, observation.response_outcome == "correct")
            for observation in sorted(
                batch.observations,
                key=lambda item: (item.occurred_at, item.attempt_id, item.observation_id),
            )
            if (key := (observation.item_id, observation.item_version)) in requirements
        ]
        if not responses:
            raise DomainError(
                code="INSUFFICIENT_MODEL_DATA",
                module="m5",
                message="DINA inference requires observations covered by the model",
                recoverable=True,
            )
        posterior, _ = self._exact_expectation(
            concept_ids=concept_ids,
            profiles=list(
                itertools.product((False, True), repeat=len(concept_ids))
            ),
            priors=model.attribute_priors,
            item_estimates=estimates,
            requirements=requirements,
            responses_by_learner={batch.learner_id: responses},
        )
        return posterior[batch.learner_id]

    def infer(
        self,
        model: DinaModelArtifact,
        batch: LearningObservationBatch,
    ) -> CognitiveDiagnosisResult:
        """Infer marginals independently for each connected model component."""

        self.require_converged(model)
        batch = self._current_inference_batch(batch)
        validate_inference_scope(model, batch)
        requirements = {
            (item.item_id, item.item_version): frozenset(item.concept_ids)
            for item in model.item_parameters
        }
        components = connected_components(requirements)
        observed_items = {
            (observation.item_id, observation.item_version)
            for observation in batch.observations
        }
        mastery: dict[str, float] = {}
        for component in components:
            component_items = {
                item_key
                for item_key, required in requirements.items()
                if required <= component
            }
            if not component_items & observed_items:
                mastery.update(
                    {
                        concept_id: model.attribute_priors[concept_id]
                        for concept_id in sorted(component)
                    }
                )
                continue
            inference_mode: InferenceMode = (
                "exact"
                if len(component) <= self._max_exact_concepts
                else "variational"
            )
            projected_model = component_model(
                model,
                component,
                inference_mode=inference_mode,
            )
            if inference_mode == "exact":
                posterior = self.profile_posterior(projected_model, batch)
                mastery.update(
                    {
                        concept_id: math.fsum(
                            probability
                            for profile, probability in posterior.items()
                            if profile[index]
                        )
                        for index, concept_id in enumerate(
                            projected_model.concept_ids
                        )
                    }
                )
            else:
                mastery.update(self._variational_inference(projected_model, batch))
        digest = hashlib.sha256(
            f"{model.model_version}:{batch.content_checksum()}".encode("utf-8")
        ).hexdigest()
        return CognitiveDiagnosisResult(
            run_id=f"dina_inference_{digest[:20]}",
            learner_id=batch.learner_id,
            model_type="DINA",
            model_version=model.model_version,
            concept_mastery=mastery,
            observation_count=len(batch.observations),
            status="estimated",
            generated_at=batch.created_at,
        )

    def _prepare_training_data(
        self,
        cohort: list[LearningObservationBatch],
        q_matrix: list[QMatrixEntry],
    ) -> PreparedData:
        active_entries = [entry for entry in q_matrix if entry.is_active()]
        requirements: dict[ItemKey, set[str]] = defaultdict(set)
        for entry in active_entries:
            requirements[(entry.item_id, entry.item_version)].add(entry.concept_id)
        if not cohort or not requirements:
            self._raise_insufficient("cohort and active Q-matrix are required")

        observations = select_current_learning_observations(
            sorted(
                (observation for batch in cohort for observation in batch.observations),
                key=lambda item: (
                    item.learner_id,
                    item.occurred_at,
                    item.attempt_id,
                    item.observation_id,
                ),
            )
        )
        course_ids = {item.course_id for item in observations}
        class_ids = {item.class_id for item in observations}
        if len(course_ids) != 1 or len(class_ids) != 1:
            raise DomainError(
                code="MODEL_SCOPE_MISMATCH",
                module="m5",
                message="DINA training observations must share one course and class",
                recoverable=True,
            )
        responses_by_learner: dict[str, list[tuple[ItemKey, bool]]] = defaultdict(list)
        outcomes_by_item: dict[ItemKey, set[bool]] = defaultdict(set)
        item_counts: dict[ItemKey, int] = defaultdict(int)
        for observation in observations:
            item_key = (observation.item_id, observation.item_version)
            if item_key not in requirements:
                continue
            outcome = observation.response_outcome == "correct"
            responses_by_learner[observation.learner_id].append((item_key, outcome))
            outcomes_by_item[item_key].add(outcome)
            item_counts[item_key] += 1
        if len(responses_by_learner) < self._min_students:
            self._raise_insufficient("too few distinct learners for DINA fitting")
        if any(
            item_counts[item_key] < self._min_responses_per_item
            or outcomes_by_item[item_key] != {False, True}
            for item_key in requirements
        ):
            self._raise_insufficient(
                "each DINA item requires enough correct and incorrect responses"
            )
        concepts = sorted(
            {concept_id for values in requirements.values() for concept_id in values}
        )
        frozen_requirements = {
            key: frozenset(value) for key, value in requirements.items()
        }
        created_at = max(observation.occurred_at for observation in observations)
        return (
            next(iter(course_ids)),
            next(iter(class_ids)),
            created_at,
            concepts,
            frozen_requirements,
            dict(responses_by_learner),
            dict(item_counts),
        )

    def _fit_variational(
        self,
        prepared: PreparedData,
    ) -> DinaModelArtifact:
        (
            course_id,
            class_id,
            created_at,
            concept_ids,
            requirements,
            responses_by_learner,
            item_counts,
        ) = prepared
        priors = {concept_id: 0.5 for concept_id in concept_ids}
        item_estimates = {
            item_key: [0.15, 0.20] for item_key in sorted(requirements)
        }
        mastery = {
            learner_id: dict(priors) for learner_id in sorted(responses_by_learner)
        }
        objective_history = [
            self._variational_elbo(
                mastery,
                priors,
                item_estimates,
                requirements,
                responses_by_learner,
            )
        ]
        converged = False
        iteration = 1
        for iteration in range(1, self._max_iterations + 1):
            previous_mastery = {
                learner_id: dict(values) for learner_id, values in mastery.items()
            }
            previous_priors = dict(priors)
            previous_estimates = {
                item_key: list(values) for item_key, values in item_estimates.items()
            }
            for learner_id in sorted(responses_by_learner):
                learner_mastery = mastery[learner_id]
                for concept_id in concept_ids:
                    log_odds = math.log(priors[concept_id]) - math.log1p(
                        -priors[concept_id]
                    )
                    for item_key, correct in responses_by_learner[learner_id]:
                        required = requirements[item_key]
                        if concept_id not in required:
                            continue
                        slip, guess = item_estimates[item_key]
                        other_capability = math.prod(
                            learner_mastery[other]
                            for other in required
                            if other != concept_id
                        )
                        log_if_mastered = (
                            other_capability
                            * math.log((1.0 - slip) if correct else slip)
                            + (1.0 - other_capability)
                            * math.log(guess if correct else 1.0 - guess)
                        )
                        log_if_unmastered = math.log(
                            guess if correct else 1.0 - guess
                        )
                        log_odds += log_if_mastered - log_if_unmastered
                    learner_mastery[concept_id] = _clip(
                        1.0 / (1.0 + math.exp(-_clip(log_odds, -40.0, 40.0))),
                        1e-6,
                        1.0 - 1e-6,
                    )

            priors = {
                concept_id: _clip(
                    math.fsum(values[concept_id] for values in mastery.values())
                    / len(mastery),
                    0.01,
                    0.99,
                )
                for concept_id in concept_ids
            }
            sufficient = {
                item_key: [0.0, 0.0, 0.0, 0.0] for item_key in requirements
            }
            for learner_id in sorted(responses_by_learner):
                for item_key, correct in responses_by_learner[learner_id]:
                    capable_mass = math.prod(
                        mastery[learner_id][concept_id]
                        for concept_id in requirements[item_key]
                    )
                    incapable_mass = 1.0 - capable_mass
                    sufficient[item_key][0] += capable_mass
                    sufficient[item_key][1] += capable_mass * (not correct)
                    sufficient[item_key][2] += incapable_mass
                    sufficient[item_key][3] += incapable_mass * correct
            item_estimates = {
                item_key: [
                    _clip(
                        values[1] / values[0]
                        if values[0] > 1e-12
                        else previous_estimates[item_key][0],
                        0.01,
                        0.40,
                    ),
                    _clip(
                        values[3] / values[2]
                        if values[2] > 1e-12
                        else previous_estimates[item_key][1],
                        0.01,
                        0.40,
                    ),
                ]
                for item_key, values in sufficient.items()
            }
            objective = self._variational_elbo(
                mastery,
                priors,
                item_estimates,
                requirements,
                responses_by_learner,
            )
            if objective + 1e-9 < objective_history[-1]:
                mastery = previous_mastery
                priors = previous_priors
                item_estimates = previous_estimates
                converged = True
                break
            objective_history.append(objective)
            if abs(objective_history[-1] - objective_history[-2]) < self._tolerance:
                converged = True
                break

        log_likelihood = self._variational_log_likelihood(
            mastery,
            item_estimates,
            requirements,
            responses_by_learner,
        )
        model_payload = {
            "course_id": course_id,
            "class_id": class_id,
            "concept_ids": concept_ids,
            "item_parameters": [
                {
                    "item_id": item_key[0],
                    "item_version": item_key[1],
                    "concept_ids": sorted(requirements[item_key]),
                    "slip": round(item_estimates[item_key][0], 12),
                    "guess": round(item_estimates[item_key][1], 12),
                    "sample_size": item_counts[item_key],
                }
                for item_key in sorted(requirements)
            ],
            "attribute_priors": {
                concept_id: round(priors[concept_id], 12)
                for concept_id in concept_ids
            },
            "learner_count": len(responses_by_learner),
            "observation_count": sum(item_counts.values()),
            "inference_mode": "variational",
            "log_likelihood": round(log_likelihood, 12),
            "elbo": round(objective_history[-1], 12),
            "objective_history": [round(value, 12) for value in objective_history],
            "iteration_count": iteration,
            "converged": converged,
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

    def _variational_inference(
        self,
        model: DinaModelArtifact,
        batch: LearningObservationBatch,
    ) -> dict[str, float]:
        parameters = {
            (item.item_id, item.item_version): item
            for item in model.item_parameters
        }
        responses = [
            (parameters[key], observation.response_outcome == "correct")
            for observation in sorted(
                batch.observations,
                key=lambda item: (item.occurred_at, item.attempt_id, item.observation_id),
            )
            if (key := (observation.item_id, observation.item_version)) in parameters
        ]
        if not responses:
            self._raise_insufficient(
                "DINA inference requires observations covered by the model"
            )
        mastery = dict(model.attribute_priors)
        for _ in range(min(self._max_iterations, 100)):
            maximum_change = 0.0
            for concept_id in model.concept_ids:
                prior = _clip(model.attribute_priors[concept_id], 1e-9, 1.0 - 1e-9)
                log_odds = math.log(prior) - math.log1p(-prior)
                for item, correct in responses:
                    if concept_id not in item.concept_ids:
                        continue
                    other_capability = math.prod(
                        mastery[other]
                        for other in item.concept_ids
                        if other != concept_id
                    )
                    log_if_mastered = (
                        other_capability
                        * math.log((1.0 - item.slip) if correct else item.slip)
                        + (1.0 - other_capability)
                        * math.log(item.guess if correct else 1.0 - item.guess)
                    )
                    log_if_unmastered = math.log(
                        item.guess if correct else 1.0 - item.guess
                    )
                    log_odds += log_if_mastered - log_if_unmastered
                updated = _clip(
                    1.0 / (1.0 + math.exp(-_clip(log_odds, -40.0, 40.0))),
                    1e-6,
                    1.0 - 1e-6,
                )
                maximum_change = max(maximum_change, abs(updated - mastery[concept_id]))
                mastery[concept_id] = updated
            if maximum_change < self._tolerance:
                break
        return mastery

    @staticmethod
    def _variational_elbo(
        mastery: Mapping[str, Mapping[str, float]],
        priors: Mapping[str, float],
        item_estimates: Mapping[ItemKey, list[float]],
        requirements: Mapping[ItemKey, frozenset[str]],
        responses_by_learner: Mapping[str, list[tuple[ItemKey, bool]]],
    ) -> float:
        value = 0.0
        for learner_id in sorted(responses_by_learner):
            for concept_id, probability in mastery[learner_id].items():
                prior = priors[concept_id]
                value += probability * math.log(prior)
                value += (1.0 - probability) * math.log(1.0 - prior)
                value -= probability * math.log(probability)
                value -= (1.0 - probability) * math.log(1.0 - probability)
            for item_key, correct in responses_by_learner[learner_id]:
                slip, guess = item_estimates[item_key]
                capable_mass = math.prod(
                    mastery[learner_id][concept_id]
                    for concept_id in requirements[item_key]
                )
                value += capable_mass * math.log(
                    (1.0 - slip) if correct else slip
                )
                value += (1.0 - capable_mass) * math.log(
                    guess if correct else 1.0 - guess
                )
        return value

    @staticmethod
    def _variational_log_likelihood(
        mastery: Mapping[str, Mapping[str, float]],
        item_estimates: Mapping[ItemKey, list[float]],
        requirements: Mapping[ItemKey, frozenset[str]],
        responses_by_learner: Mapping[str, list[tuple[ItemKey, bool]]],
    ) -> float:
        value = 0.0
        for learner_id in sorted(responses_by_learner):
            for item_key, correct in responses_by_learner[learner_id]:
                slip, guess = item_estimates[item_key]
                capable_mass = math.prod(
                    mastery[learner_id][concept_id]
                    for concept_id in requirements[item_key]
                )
                correct_probability = (
                    capable_mass * (1.0 - slip)
                    + (1.0 - capable_mass) * guess
                )
                value += math.log(
                    correct_probability if correct else 1.0 - correct_probability
                )
        return value

    def _exact_expectation(
        self,
        *,
        concept_ids: list[str],
        profiles: list[Profile],
        priors: Mapping[str, float],
        item_estimates: Mapping[ItemKey, list[float]],
        requirements: Mapping[ItemKey, frozenset[str]],
        responses_by_learner: Mapping[str, list[tuple[ItemKey, bool]]],
    ) -> tuple[dict[str, dict[Profile, float]], float]:
        profile_maps = [
            dict(zip(concept_ids, profile, strict=True)) for profile in profiles
        ]
        posterior_by_learner: dict[str, dict[Profile, float]] = {}
        log_likelihood = 0.0
        for learner_id in sorted(responses_by_learner):
            log_weights: list[float] = []
            for profile, profile_map in zip(profiles, profile_maps, strict=True):
                log_weight = 0.0
                for index, concept_id in enumerate(concept_ids):
                    probability = _clip(priors[concept_id], 1e-9, 1.0 - 1e-9)
                    log_weight += math.log(
                        probability if profile[index] else 1.0 - probability
                    )
                for item_key, correct in responses_by_learner[learner_id]:
                    slip, guess = item_estimates[item_key]
                    probability = self.response_probability(
                        capable=self.is_capable(
                            profile=profile_map,
                            required=requirements[item_key],
                        ),
                        slip=slip,
                        guess=guess,
                    )
                    probability = _clip(probability, 1e-9, 1.0 - 1e-9)
                    log_weight += math.log(
                        probability if correct else 1.0 - probability
                    )
                log_weights.append(log_weight)
            normalizer = _logsumexp(log_weights)
            log_likelihood += normalizer
            posterior_by_learner[learner_id] = {
                profile: math.exp(log_weight - normalizer)
                for profile, log_weight in zip(profiles, log_weights, strict=True)
            }
        return posterior_by_learner, log_likelihood

    def _maximization(
        self,
        *,
        concept_ids: list[str],
        posterior_by_learner: Mapping[str, Mapping[Profile, float]],
        item_estimates: Mapping[ItemKey, list[float]],
        requirements: Mapping[ItemKey, frozenset[str]],
        responses_by_learner: Mapping[str, list[tuple[ItemKey, bool]]],
    ) -> tuple[dict[str, float], dict[ItemKey, list[float]]]:
        learner_count = len(posterior_by_learner)
        priors = {
            concept_id: _clip(
                math.fsum(
                    probability
                    for posterior in posterior_by_learner.values()
                    for profile, probability in posterior.items()
                    if profile[index]
                )
                / learner_count,
                0.01,
                0.99,
            )
            for index, concept_id in enumerate(concept_ids)
        }
        profile_maps = {
            profile: dict(zip(concept_ids, profile, strict=True))
            for posterior in posterior_by_learner.values()
            for profile in posterior
        }
        sufficient = {
            item_key: [0.0, 0.0, 0.0, 0.0] for item_key in requirements
        }
        for learner_id in sorted(responses_by_learner):
            posterior = posterior_by_learner[learner_id]
            for item_key, correct in responses_by_learner[learner_id]:
                capable_mass = math.fsum(
                    probability
                    for profile, probability in posterior.items()
                    if self.is_capable(
                        profile=profile_maps[profile],
                        required=requirements[item_key],
                    )
                )
                incapable_mass = 1.0 - capable_mass
                sufficient[item_key][0] += capable_mass
                sufficient[item_key][1] += capable_mass * (not correct)
                sufficient[item_key][2] += incapable_mass
                sufficient[item_key][3] += incapable_mass * correct
        estimates: dict[ItemKey, list[float]] = {}
        for item_key in requirements:
            capable_total, capable_errors, incapable_total, incapable_correct = (
                sufficient[item_key]
            )
            old_slip, old_guess = item_estimates[item_key]
            slip = capable_errors / capable_total if capable_total > 1e-12 else old_slip
            guess = (
                incapable_correct / incapable_total
                if incapable_total > 1e-12
                else old_guess
            )
            estimates[item_key] = [
                _clip(slip, 0.01, 0.40),
                _clip(guess, 0.01, 0.40),
            ]
        return priors, estimates

    @staticmethod
    def _raise_insufficient(reason: str) -> None:
        raise DomainError(
            code="INSUFFICIENT_MODEL_DATA",
            module="m5",
            message=reason,
            recoverable=True,
        )
