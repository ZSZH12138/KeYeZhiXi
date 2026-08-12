"""Deterministic, side-effect-free ranking primitives for M2 retrieval."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from numbers import Real
from typing import TypeAlias

from course_insight.contracts.errors import DomainError


_ScoreMap: TypeAlias = Mapping[str, Real]
_Rerank: TypeAlias = Callable[["RetrievalCandidate"], Real]


def _error(code: str, message: str, *, recoverable: bool = False) -> DomainError:
    """Build a safe ranking error without echoing query or document content."""

    return DomainError(
        code=code,
        module="m2",
        message=message,
        details={},
        recoverable=recoverable,
    )


def _finite_score(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise _error(
            "RETRIEVAL_SCORE_INVALID",
            "retrieval scores must be finite numbers",
        )
    try:
        score = float(value)
    except (OverflowError, TypeError, ValueError):
        raise _error(
            "RETRIEVAL_SCORE_INVALID",
            "retrieval scores must be finite numbers",
        ) from None
    if not math.isfinite(score):
        raise _error(
            "RETRIEVAL_SCORE_INVALID",
            "retrieval scores must be finite numbers",
        )
    return score


def _evidence_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise _error(
            "RETRIEVAL_CANDIDATE_INVALID",
            "retrieval candidate identity is invalid",
        )
    return value


def _validate_candidate(candidate: "RetrievalCandidate") -> None:
    _evidence_id(candidate.evidence_id)
    for score in (
        candidate.lexical_score,
        candidate.vector_score,
        candidate.final_score,
    ):
        _finite_score(score)


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    """An immutable candidate carrying both signal scores and its final score."""

    evidence_id: str
    lexical_score: float = 0.0
    vector_score: float = 0.0
    final_score: float = 0.0

    def __post_init__(self) -> None:
        _validate_candidate(self)


def _validate_ids_and_scores(scores: _ScoreMap) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for evidence_id, score in scores.items():
        identifier = _evidence_id(evidence_id)
        normalized[identifier] = _finite_score(score)
    return normalized


def validate_finite_scores(
    scores: Mapping[str, Real] | Iterable[RetrievalCandidate] | Iterable[Real],
) -> dict[str, float] | tuple[RetrievalCandidate, ...] | tuple[float, ...]:
    """Validate finite values without changing the caller's collection."""

    if isinstance(scores, Mapping):
        return _validate_ids_and_scores(scores)

    try:
        values = tuple(scores)
    except TypeError:
        raise _error(
            "RETRIEVAL_SCORE_INVALID",
            "retrieval scores must be an iterable",
        ) from None

    if not values:
        return ()
    if all(isinstance(value, RetrievalCandidate) for value in values):
        for candidate in values:
            _validate_candidate(candidate)
        return values
    if any(isinstance(value, RetrievalCandidate) for value in values):
        raise _error(
            "RETRIEVAL_CANDIDATE_INVALID",
            "retrieval candidates must use one collection type",
        )
    return tuple(_finite_score(value) for value in values)


def _signal_scores(
    values: Mapping[str, Real] | Iterable[RetrievalCandidate],
    *,
    signal: str,
) -> dict[str, float]:
    if isinstance(values, Mapping):
        return _validate_ids_and_scores(values)

    try:
        candidates = tuple(values)
    except TypeError:
        raise _error(
            "RETRIEVAL_CANDIDATE_INVALID",
            "retrieval candidates must be iterable",
        ) from None

    result: dict[str, float] = {}
    for candidate in candidates:
        if not isinstance(candidate, RetrievalCandidate):
            raise _error(
                "RETRIEVAL_CANDIDATE_INVALID",
                "retrieval candidates have an invalid type",
            )
        evidence_id = _evidence_id(candidate.evidence_id)
        if evidence_id in result:
            raise _error(
                "RETRIEVAL_CANDIDATE_INVALID",
                "retrieval candidate identities must be unique",
            )
        result[evidence_id] = _finite_score(getattr(candidate, signal))
    return result


def merge_candidates(
    lexical: Mapping[str, Real] | Iterable[RetrievalCandidate],
    vector: Mapping[str, Real] | Iterable[RetrievalCandidate],
) -> tuple[RetrievalCandidate, ...]:
    """Merge lexical and vector candidate sets using zero for missing signals."""

    lexical_scores = _signal_scores(lexical, signal="lexical_score")
    vector_scores = _signal_scores(vector, signal="vector_score")
    evidence_ids = sorted(set(lexical_scores) | set(vector_scores))
    return tuple(
        RetrievalCandidate(
            evidence_id=evidence_id,
            lexical_score=lexical_scores.get(evidence_id, 0.0),
            vector_score=vector_scores.get(evidence_id, 0.0),
        )
        for evidence_id in evidence_ids
    )


def _normalization_range(lower: object, upper: object) -> tuple[float, float]:
    lower_bound = _finite_score(lower)
    upper_bound = _finite_score(upper)
    if lower_bound >= upper_bound:
        raise _error(
            "RETRIEVAL_NORMALIZATION_INVALID",
            "normalization bounds must be finite and strictly increasing",
        )
    return lower_bound, upper_bound


def min_max_normalize(scores: Mapping[str, Real]) -> dict[str, float]:
    """Map finite scores to ``[0, 1]`` using their observed min and max."""

    normalized_scores = _validate_ids_and_scores(scores)
    if not normalized_scores:
        return {}
    minimum = min(normalized_scores.values())
    maximum = max(normalized_scores.values())
    if minimum == maximum:
        return {evidence_id: 0.0 for evidence_id in normalized_scores}
    scale = max(abs(minimum), abs(maximum))
    scaled_minimum = minimum / scale
    scaled_maximum = maximum / scale
    span = scaled_maximum - scaled_minimum
    if not math.isfinite(span) or span <= 0.0:
        raise _error(
            "RETRIEVAL_NORMALIZATION_INVALID",
            "finite scores cannot form a normalization interval",
        )
    normalized: dict[str, float] = {}
    for evidence_id, value in normalized_scores.items():
        result = ((value / scale) - scaled_minimum) / span
        if not math.isfinite(result):
            raise _error(
                "RETRIEVAL_NORMALIZATION_INVALID",
                "finite scores cannot form a normalization interval",
            )
        normalized[evidence_id] = min(1.0, max(0.0, result))
    return normalized


def bounded_normalize(
    scores: Mapping[str, Real],
    *,
    lower: Real,
    upper: Real,
) -> dict[str, float]:
    """Clip scores to known bounds and map that interval to ``[0, 1]``."""

    lower_bound, upper_bound = _normalization_range(lower, upper)
    normalized_scores = _validate_ids_and_scores(scores)
    scale = max(abs(lower_bound), abs(upper_bound))
    scaled_lower = lower_bound / scale
    scaled_upper = upper_bound / scale
    span = scaled_upper - scaled_lower
    if not math.isfinite(span) or span <= 0.0:
        raise _error(
            "RETRIEVAL_NORMALIZATION_INVALID",
            "normalization bounds are too wide",
        )
    normalized: dict[str, float] = {}
    for evidence_id, score in normalized_scores.items():
        bounded = min(upper_bound, max(lower_bound, score))
        result = ((bounded / scale) - scaled_lower) / span
        if not math.isfinite(result):
            raise _error(
                "RETRIEVAL_NORMALIZATION_INVALID",
                "normalization bounds are too wide",
            )
        normalized[evidence_id] = min(1.0, max(0.0, result))
    return normalized


def validate_hybrid_weights(
    lexical_weight: Real,
    vector_weight: Real,
) -> tuple[float, float]:
    """Validate non-negative hybrid weights whose total is exactly one."""

    try:
        lexical = _finite_score(lexical_weight)
        vector = _finite_score(vector_weight)
    except DomainError:
        raise _error(
            "RETRIEVAL_WEIGHTS_INVALID",
            "hybrid weights must be finite numbers",
        ) from None
    if lexical < 0.0 or vector < 0.0 or not math.isclose(
        lexical + vector,
        1.0,
        rel_tol=0.0,
        abs_tol=1e-12,
    ) or lexical + vector <= 0.0:
        raise _error(
            "RETRIEVAL_WEIGHTS_INVALID",
            "hybrid weights must be non-negative and sum to one",
        )
    return lexical, vector


def _ordered(candidates: Iterable[RetrievalCandidate]) -> tuple[RetrievalCandidate, ...]:
    validated = validate_finite_scores(candidates)
    if not isinstance(validated, tuple) or any(
        not isinstance(candidate, RetrievalCandidate) for candidate in validated
    ):
        raise _error(
            "RETRIEVAL_CANDIDATE_INVALID",
            "retrieval candidates have an invalid type",
        )
    candidates_tuple = validated
    if len({candidate.evidence_id for candidate in candidates_tuple}) != len(
        candidates_tuple
    ):
        raise _error(
            "RETRIEVAL_CANDIDATE_INVALID",
            "retrieval candidate identities must be unique",
        )
    return tuple(
        sorted(
            candidates_tuple,
            key=lambda candidate: (-candidate.final_score, candidate.evidence_id),
        )
    )


def top_k(
    candidates: Iterable[RetrievalCandidate],
    k: int,
) -> tuple[RetrievalCandidate, ...]:
    """Return at most ``k`` candidates in deterministic score order."""

    if type(k) is not int or k < 0:
        raise _error(
            "RETRIEVAL_TOP_K_INVALID",
            "top-k must be a non-negative integer",
        )
    return _ordered(candidates)[:k]


def rank_candidates(
    candidates: Iterable[RetrievalCandidate],
    *,
    rerank: _Rerank | None = None,
    top_k: int | None = None,
) -> tuple[RetrievalCandidate, ...]:
    """Optionally apply a deterministic score function, then stably rank."""

    if top_k is not None and (type(top_k) is not int or top_k < 0):
        raise _error(
            "RETRIEVAL_TOP_K_INVALID",
            "top-k must be a non-negative integer",
        )
    ordered_input = tuple(
        sorted(
            _ordered(candidates),
            key=lambda candidate: candidate.evidence_id,
        )
    )
    if rerank is not None and not callable(rerank):
        raise _error(
            "RETRIEVAL_RERANK_INVALID",
            "rerank must be callable",
        )
    if rerank is not None:
        reranked = []
        for candidate in ordered_input:
            try:
                rerank_score = _finite_score(rerank(candidate))
            except DomainError:
                raise
            except Exception:
                raise _error(
                    "RETRIEVAL_RERANK_INVALID",
                    "rerank evaluation failed",
                    recoverable=True,
                ) from None
            reranked.append(replace(candidate, final_score=rerank_score))
        ordered_input = tuple(reranked)
    ranked = _ordered(ordered_input)
    return ranked if top_k is None else ranked[:top_k]


def rank_hybrid(
    lexical: Mapping[str, Real] | Iterable[RetrievalCandidate],
    vector: Mapping[str, Real] | Iterable[RetrievalCandidate],
    *,
    lexical_weight: Real,
    vector_weight: Real,
    top_k: int | None = None,
    rerank: _Rerank | None = None,
) -> tuple[RetrievalCandidate, ...]:
    """Normalize, combine, optionally rerank, and truncate hybrid candidates."""

    lexical_scores = _signal_scores(lexical, signal="lexical_score")
    vector_scores = _signal_scores(vector, signal="vector_score")
    lexical_weight_value, vector_weight_value = validate_hybrid_weights(
        lexical_weight,
        vector_weight,
    )
    merged = merge_candidates(lexical_scores, vector_scores)
    normalized_lexical = min_max_normalize(lexical_scores)
    normalized_vector = min_max_normalize(vector_scores)
    combined = tuple(
        replace(
            candidate,
            final_score=lexical_weight_value
            * normalized_lexical.get(candidate.evidence_id, 0.0)
            + vector_weight_value
            * normalized_vector.get(candidate.evidence_id, 0.0),
        )
        for candidate in merged
    )
    return rank_candidates(combined, rerank=rerank, top_k=top_k)


def validate_strategy_dependencies(
    strategy: str,
    *,
    lexical_available: bool,
    vector_available: bool,
) -> None:
    """Reject unsupported strategy dependencies instead of returning empty."""

    if not isinstance(strategy, str) or strategy not in {
        "lexical",
        "vector",
        "hybrid",
    }:
        raise _error(
            "RETRIEVAL_STRATEGY_INVALID",
            "retrieval strategy is invalid",
        )
    if type(lexical_available) is not bool or type(vector_available) is not bool:
        raise _error(
            "RETRIEVAL_DEPENDENCY_UNAVAILABLE",
            "retrieval strategy dependency availability is invalid",
            recoverable=True,
        )
    required = {
        "lexical": lexical_available,
        "vector": vector_available,
        "hybrid": lexical_available and vector_available,
    }[strategy]
    if not required:
        raise _error(
            "RETRIEVAL_DEPENDENCY_UNAVAILABLE",
            "retrieval strategy dependency is unavailable",
            recoverable=True,
        )


__all__ = [
    "RetrievalCandidate",
    "bounded_normalize",
    "merge_candidates",
    "min_max_normalize",
    "rank_candidates",
    "rank_hybrid",
    "top_k",
    "validate_finite_scores",
    "validate_hybrid_weights",
    "validate_strategy_dependencies",
]
