"""Four-parameter Bayesian Knowledge Tracing fitting and inference."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping

from course_insight.contracts.errors import DomainError
from course_insight.contracts.learning_models import (
    BktConceptParameters,
    BktModelArtifact,
    ConceptResponse,
    ConceptResponseSequence,
    KnowledgeTraceSnapshot,
)


def _clip(value: float, lower: float, upper: float) -> float:
    return min(upper, max(lower, value))


class BktEngine:
    """Fit and apply a no-forgetting, four-parameter BKT model."""

    def __init__(
        self,
        *,
        min_students: int = 100,
        min_observations_per_student: int = 5,
        max_iterations: int = 200,
        tolerance: float = 1e-6,
    ) -> None:
        if (
            min_students < 1
            or min_observations_per_student < 1
            or max_iterations < 1
        ):
            raise ValueError("BKT fitting thresholds must be positive")
        self._min_students = min_students
        self._min_observations_per_student = min_observations_per_student
        self._max_iterations = max_iterations
        self._tolerance = tolerance

    @staticmethod
    def observation_update(
        *,
        prior: float,
        correct: bool,
        guess: float,
        slip: float,
    ) -> float:
        """Return mastery posterior after one response, before learning."""

        mastered_likelihood = 1.0 - slip if correct else slip
        unmastered_likelihood = guess if correct else 1.0 - guess
        numerator = prior * mastered_likelihood
        denominator = numerator + (1.0 - prior) * unmastered_likelihood
        if denominator <= 0.0:
            raise DomainError(
                code="BKT_PROBABILITY_INVALID",
                module="m5",
                message="BKT observation update has zero probability mass",
            )
        return numerator / denominator

    def update(
        self,
        model: BktModelArtifact,
        sequence: ConceptResponseSequence,
    ) -> KnowledgeTraceSnapshot:
        """Replay one governed concept sequence into its latest mastery state."""

        if model.course_id != sequence.course_id or model.class_id != sequence.class_id:
            raise DomainError(
                code="MODEL_SCOPE_MISMATCH",
                module="m5",
                message="BKT sequence does not match the model scope",
                recoverable=True,
            )
        parameters = next(
            (
                item
                for item in model.concept_parameters
                if item.concept_id == sequence.concept_id
            ),
            None,
        )
        if parameters is None:
            raise DomainError(
                code="BKT_CONCEPT_NOT_FOUND",
                module="m5",
                message="BKT model does not contain the requested concept",
                recoverable=True,
            )
        responses = self._deduplicate_responses(sequence.responses)
        mastery = parameters.prior
        for response in responses:
            posterior = self.observation_update(
                prior=mastery,
                correct=response.is_correct,
                guess=parameters.guess,
                slip=parameters.slip,
            )
            mastery = posterior + (1.0 - posterior) * parameters.learn
        audit_keys = [
            f"{item.source_audit_id}:v{item.source_audit_version}"
            for item in responses
        ]
        identity_payload = {
            "model_version": model.model_version,
            "learner_id": sequence.learner_id,
            "concept_id": sequence.concept_id,
            "audit_keys": audit_keys,
        }
        digest = hashlib.sha256(
            json.dumps(
                identity_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return KnowledgeTraceSnapshot(
            trace_id=f"bkt_trace_{digest[:24]}",
            learner_id=sequence.learner_id,
            course_id=sequence.course_id,
            class_id=sequence.class_id,
            model_type="BKT",
            model_version=model.model_version,
            concept_probabilities={sequence.concept_id: mastery},
            observation_watermark=f"bkt-{digest[:20]}",
            observation_count=len(responses),
            processed_audit_keys=audit_keys,
            status="estimated",
            updated_at=(
                responses[-1].occurred_at if responses else sequence.created_at
            ),
        )

    def fit(
        self,
        sequences: list[ConceptResponseSequence],
    ) -> BktModelArtifact:
        """Fit independent concept HMMs with Baum-Welch."""

        prepared = self._prepare_sequences(sequences)
        course_id, class_id, created_at, grouped = prepared
        parameter_rows: list[BktConceptParameters] = []
        total_likelihood = 0.0
        maximum_iteration = 1
        all_converged = True
        learner_ids: set[str] = set()
        observation_count = 0
        for concept_id in sorted(grouped):
            concept_sequences = grouped[concept_id]
            learner_ids.update(concept_sequences)
            observation_count += sum(
                len(outcomes) for outcomes in concept_sequences.values()
            )
            parameters, likelihood, iteration, converged = self._fit_concept(
                concept_sequences
            )
            prior, learn, guess, slip = parameters
            parameter_rows.append(
                BktConceptParameters(
                    concept_id=concept_id,
                    prior=prior,
                    learn=learn,
                    guess=guess,
                    slip=slip,
                    learner_count=len(concept_sequences),
                    observation_count=sum(
                        len(outcomes) for outcomes in concept_sequences.values()
                    ),
                )
            )
            total_likelihood += likelihood
            maximum_iteration = max(maximum_iteration, iteration)
            all_converged = all_converged and converged
        payload = {
            "course_id": course_id,
            "class_id": class_id,
            "concept_parameters": [
                item.model_dump(mode="json") for item in parameter_rows
            ],
            "learner_count": len(learner_ids),
            "observation_count": observation_count,
            "log_likelihood": round(total_likelihood, 12),
            "iteration_count": maximum_iteration,
            "converged": all_converged,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return BktModelArtifact(
            model_id=f"bkt_{digest[:24]}",
            model_version=f"bkt-{digest[:16]}",
            created_at=created_at,
            **payload,
        )

    def _prepare_sequences(
        self,
        sequences: list[ConceptResponseSequence],
    ):
        if not sequences:
            self._raise_insufficient("BKT fitting requires response sequences")
        course_ids = {sequence.course_id for sequence in sequences}
        class_ids = {sequence.class_id for sequence in sequences}
        if len(course_ids) != 1 or len(class_ids) != 1:
            raise DomainError(
                code="MODEL_SCOPE_MISMATCH",
                module="m5",
                message="BKT training sequences must share one course and class",
                recoverable=True,
            )
        grouped_responses: dict[
            str,
            dict[str, list[ConceptResponse]],
        ] = defaultdict(lambda: defaultdict(list))
        for sequence in sequences:
            grouped_responses[sequence.concept_id][sequence.learner_id].extend(
                sequence.responses
            )
        grouped_outcomes: dict[str, dict[str, list[bool]]] = {}
        for concept_id, learner_responses in grouped_responses.items():
            if len(learner_responses) < self._min_students:
                self._raise_insufficient(
                    "too few distinct learners for BKT concept fitting"
                )
            grouped_outcomes[concept_id] = {}
            for learner_id, responses in learner_responses.items():
                governed = self._deduplicate_responses(responses)
                if len(governed) < self._min_observations_per_student:
                    self._raise_insufficient(
                        "each BKT learner requires enough ordered observations"
                    )
                grouped_outcomes[concept_id][learner_id] = [
                    item.is_correct for item in governed
                ]
        return (
            next(iter(course_ids)),
            next(iter(class_ids)),
            max(sequence.created_at for sequence in sequences),
            grouped_outcomes,
        )

    def _fit_concept(
        self,
        sequences: Mapping[str, list[bool]],
    ) -> tuple[tuple[float, float, float, float], float, int, bool]:
        parameters = (0.35, 0.15, 0.20, 0.10)
        previous_likelihood: float | None = None
        converged = False
        likelihood = float("-inf")
        for iteration in range(1, self._max_iterations + 1):
            statistics = [0.0] * 9
            likelihood = 0.0
            for learner_id in sorted(sequences):
                outcomes = sequences[learner_id]
                gamma, xi, sequence_likelihood = self._forward_backward(
                    outcomes,
                    parameters,
                )
                likelihood += sequence_likelihood
                statistics[0] += gamma[0][1]
                for index, correct in enumerate(outcomes):
                    statistics[1] += gamma[index][0]
                    statistics[2] += gamma[index][0] * correct
                    statistics[3] += gamma[index][1]
                    statistics[4] += gamma[index][1] * (not correct)
                for transitions in xi:
                    statistics[5] += transitions[0][0] + transitions[0][1]
                    statistics[6] += transitions[0][1]
            learner_count = len(sequences)
            prior = _clip(statistics[0] / learner_count, 0.01, 0.99)
            learn = _clip(
                statistics[6] / statistics[5]
                if statistics[5] > 1e-12
                else parameters[1],
                0.01,
                0.40,
            )
            guess = _clip(
                statistics[2] / statistics[1]
                if statistics[1] > 1e-12
                else parameters[2],
                0.01,
                0.40,
            )
            slip = _clip(
                statistics[4] / statistics[3]
                if statistics[3] > 1e-12
                else parameters[3],
                0.01,
                0.40,
            )
            parameters = (prior, learn, guess, slip)
            if (
                previous_likelihood is not None
                and abs(likelihood - previous_likelihood) < self._tolerance
            ):
                converged = True
                break
            previous_likelihood = likelihood
        likelihood = math.fsum(
            self._forward_backward(outcomes, parameters)[2]
            for outcomes in sequences.values()
        )
        return parameters, likelihood, iteration, converged

    @staticmethod
    def _forward_backward(
        outcomes: list[bool],
        parameters: tuple[float, float, float, float],
    ) -> tuple[
        list[tuple[float, float]],
        list[tuple[tuple[float, float], tuple[float, float]]],
        float,
    ]:
        prior, learn, guess, slip = parameters

        def emission(state: int, correct: bool) -> float:
            probability = (1.0 - slip) if state else guess
            return probability if correct else 1.0 - probability

        alpha: list[tuple[float, float]] = []
        scales: list[float] = []
        first = (
            (1.0 - prior) * emission(0, outcomes[0]),
            prior * emission(1, outcomes[0]),
        )
        scale = math.fsum(first)
        alpha.append((first[0] / scale, first[1] / scale))
        scales.append(scale)
        for correct in outcomes[1:]:
            previous = alpha[-1]
            current = (
                previous[0] * (1.0 - learn) * emission(0, correct),
                (previous[0] * learn + previous[1]) * emission(1, correct),
            )
            scale = math.fsum(current)
            alpha.append((current[0] / scale, current[1] / scale))
            scales.append(scale)
        beta: list[tuple[float, float]] = [(1.0, 1.0)] * len(outcomes)
        for index in range(len(outcomes) - 2, -1, -1):
            next_correct = outcomes[index + 1]
            next_beta = beta[index + 1]
            beta[index] = (
                (
                    (1.0 - learn) * emission(0, next_correct) * next_beta[0]
                    + learn * emission(1, next_correct) * next_beta[1]
                )
                / scales[index + 1],
                emission(1, next_correct) * next_beta[1] / scales[index + 1],
            )
        gamma: list[tuple[float, float]] = []
        for forward, backward in zip(alpha, beta, strict=True):
            masses = (forward[0] * backward[0], forward[1] * backward[1])
            total = math.fsum(masses)
            gamma.append((masses[0] / total, masses[1] / total))
        xi: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for index in range(len(outcomes) - 1):
            next_correct = outcomes[index + 1]
            next_beta = beta[index + 1]
            masses = (
                (
                    alpha[index][0]
                    * (1.0 - learn)
                    * emission(0, next_correct)
                    * next_beta[0],
                    alpha[index][0]
                    * learn
                    * emission(1, next_correct)
                    * next_beta[1],
                ),
                (
                    0.0,
                    alpha[index][1]
                    * emission(1, next_correct)
                    * next_beta[1],
                ),
            )
            total = math.fsum(value for row in masses for value in row)
            xi.append(
                (
                    (masses[0][0] / total, masses[0][1] / total),
                    (0.0, masses[1][1] / total),
                )
            )
        return gamma, xi, math.fsum(math.log(scale) for scale in scales)

    @staticmethod
    def _deduplicate_responses(
        responses: list[ConceptResponse],
    ) -> list[ConceptResponse]:
        governed: list[ConceptResponse] = []
        seen: set[tuple[str, int]] = set()
        for response in sorted(
            responses,
            key=lambda item: (
                item.occurred_at,
                item.attempt_id,
                item.observation_id,
            ),
        ):
            audit_key = (response.source_audit_id, response.source_audit_version)
            if audit_key in seen:
                continue
            seen.add(audit_key)
            governed.append(response)
        return governed

    @staticmethod
    def _raise_insufficient(reason: str) -> None:
        raise DomainError(
            code="INSUFFICIENT_MODEL_DATA",
            module="m5",
            message=reason,
            recoverable=True,
        )
