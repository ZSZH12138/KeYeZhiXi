"""Pure-function tests for the M2 lexical/vector ranking primitives."""

from __future__ import annotations

import math

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.modules.m2_evidence_retrieval.ranking import (
    RetrievalCandidate,
    bounded_normalize,
    merge_candidates,
    min_max_normalize,
    rank_candidates,
    rank_hybrid,
    top_k,
    validate_finite_scores,
    validate_hybrid_weights,
    validate_strategy_dependencies,
)


def test_merge_candidates_keeps_union_and_zero_fills_missing_signals() -> None:
    merged = merge_candidates(
        {"evidence_b": 0.4, "evidence_a": 0.2},
        {"evidence_c": 0.9, "evidence_a": 0.8},
    )

    assert [candidate.evidence_id for candidate in merged] == [
        "evidence_a",
        "evidence_b",
        "evidence_c",
    ]
    assert [(candidate.lexical_score, candidate.vector_score) for candidate in merged] == [
        (0.2, 0.8),
        (0.4, 0.0),
        (0.0, 0.9),
    ]


def test_merge_candidates_rejects_duplicate_candidate_ids() -> None:
    candidates = [
        RetrievalCandidate("evidence_a", lexical_score=0.1),
        RetrievalCandidate("evidence_a", lexical_score=0.2),
    ]

    with pytest.raises(DomainError) as captured:
        merge_candidates(candidates, ())

    assert captured.value.code == "RETRIEVAL_CANDIDATE_INVALID"
    assert captured.value.recoverable is False
    assert captured.value.details == {}


@pytest.mark.parametrize("score", [math.nan, math.inf, -math.inf])
def test_candidate_rejects_non_finite_scores(score: float) -> None:
    with pytest.raises(DomainError) as captured:
        RetrievalCandidate("evidence_a", lexical_score=score)

    assert captured.value.code == "RETRIEVAL_SCORE_INVALID"
    assert captured.value.details == {}


def test_validate_finite_scores_returns_an_immutable_snapshot() -> None:
    candidates = (
        RetrievalCandidate("evidence_a", lexical_score=-0.5),
        RetrievalCandidate("evidence_b", vector_score=0.25, final_score=0.75),
    )

    assert validate_finite_scores(candidates) == candidates


def test_min_max_normalize_maps_scores_to_zero_one() -> None:
    normalized = min_max_normalize(
        {"evidence_a": 10.0, "evidence_b": 5.0, "evidence_c": 0.0}
    )

    assert normalized == {
        "evidence_a": 1.0,
        "evidence_b": 0.5,
        "evidence_c": 0.0,
    }


def test_min_max_normalize_uses_zero_for_a_constant_signal() -> None:
    assert min_max_normalize({"evidence_a": 3.0, "evidence_b": 3.0}) == {
        "evidence_a": 0.0,
        "evidence_b": 0.0,
    }


def test_min_max_normalize_handles_widely_separated_finite_scores() -> None:
    normalized = min_max_normalize({"low": -1e308, "high": 1e308})

    assert normalized == {"low": 0.0, "high": 1.0}


def test_bounded_normalize_clips_outliers_before_scaling() -> None:
    normalized = bounded_normalize(
        {"low": -2.0, "middle": 0.5, "high": 4.0},
        lower=-1.0,
        upper=1.0,
    )

    assert normalized == {"low": 0.0, "middle": 0.75, "high": 1.0}


def test_bounded_normalize_rejects_an_empty_or_inverted_range() -> None:
    with pytest.raises(DomainError) as captured:
        bounded_normalize({"evidence_a": 1.0}, lower=1.0, upper=1.0)

    assert captured.value.code == "RETRIEVAL_NORMALIZATION_INVALID"
    assert captured.value.details == {}


def test_bounded_normalize_handles_widely_separated_finite_bounds() -> None:
    normalized = bounded_normalize(
        {"low": -1e308, "high": 1e308},
        lower=-1e308,
        upper=1e308,
    )

    assert normalized == {"low": 0.0, "high": 1.0}


def test_hybrid_weights_must_be_finite_nonnegative_and_sum_to_one() -> None:
    assert validate_hybrid_weights(0.25, 0.75) == (0.25, 0.75)

    for weights in ((0.0, 0.0), (-0.1, 1.1), (0.2, 0.2), (math.nan, 1.0)):
        with pytest.raises(DomainError) as captured:
            validate_hybrid_weights(*weights)
        assert captured.value.code == "RETRIEVAL_WEIGHTS_INVALID"
        assert captured.value.details == {}


def test_hybrid_rank_normalizes_each_signal_combines_weights_and_applies_top_k() -> None:
    ranked = rank_hybrid(
        {"evidence_a": 10.0, "evidence_b": 5.0, "evidence_c": 0.0},
        {"evidence_a": 0.1, "evidence_b": 0.9, "evidence_c": 0.4},
        lexical_weight=0.5,
        vector_weight=0.5,
        top_k=2,
    )

    assert [candidate.evidence_id for candidate in ranked] == [
        "evidence_b",
        "evidence_a",
    ]
    assert ranked[0].final_score == 0.75
    assert ranked[1].final_score == 0.5


def test_top_k_sorts_descending_and_breaks_score_ties_by_evidence_id() -> None:
    candidates = [
        RetrievalCandidate("evidence_c", final_score=0.5),
        RetrievalCandidate("evidence_b", final_score=0.5),
        RetrievalCandidate("evidence_a", final_score=0.75),
    ]

    assert [candidate.evidence_id for candidate in top_k(candidates, 2)] == [
        "evidence_a",
        "evidence_b",
    ]


def test_rank_candidates_rerank_is_deterministic_and_validates_rerank_scores() -> None:
    candidates = [
        RetrievalCandidate("evidence_b", final_score=0.9),
        RetrievalCandidate("evidence_a", final_score=0.5),
    ]

    def rerank(candidate: RetrievalCandidate) -> float:
        return 1.0 if candidate.evidence_id == "evidence_a" else 0.0

    first = rank_candidates(candidates, rerank=rerank)
    second = rank_candidates(list(reversed(candidates)), rerank=rerank)
    assert [candidate.evidence_id for candidate in first] == [
        "evidence_a",
        "evidence_b",
    ]
    assert first == second

    with pytest.raises(DomainError) as captured:
        rank_candidates(candidates, rerank=lambda _: math.nan)
    assert captured.value.code == "RETRIEVAL_SCORE_INVALID"
    assert captured.value.details == {}


@pytest.mark.parametrize(
    ("strategy", "lexical_available", "vector_available"),
    [
        ("lexical", False, True),
        ("vector", True, False),
        ("hybrid", True, False),
        ("hybrid", False, True),
    ],
)
def test_unavailable_strategy_dependency_fails_closed(
    strategy: str,
    lexical_available: bool,
    vector_available: bool,
) -> None:
    with pytest.raises(DomainError) as captured:
        validate_strategy_dependencies(
            strategy,
            lexical_available=lexical_available,
            vector_available=vector_available,
        )

    assert captured.value.code == "RETRIEVAL_DEPENDENCY_UNAVAILABLE"
    assert captured.value.recoverable is True
    assert captured.value.details == {}


def test_invalid_strategy_fails_closed_instead_of_returning_empty() -> None:
    with pytest.raises(DomainError) as captured:
        validate_strategy_dependencies(
            "unknown",
            lexical_available=True,
            vector_available=True,
        )

    assert captured.value.code == "RETRIEVAL_STRATEGY_INVALID"
    assert captured.value.recoverable is False
    assert captured.value.details == {}


def test_unhashable_strategy_fails_closed() -> None:
    with pytest.raises(DomainError) as captured:
        validate_strategy_dependencies(
            [],
            lexical_available=True,
            vector_available=True,
        )

    assert captured.value.code == "RETRIEVAL_STRATEGY_INVALID"
    assert captured.value.details == {}


def test_available_strategy_dependencies_are_accepted() -> None:
    validate_strategy_dependencies(
        "lexical", lexical_available=True, vector_available=False
    )
    validate_strategy_dependencies(
        "vector", lexical_available=False, vector_available=True
    )
    validate_strategy_dependencies(
        "hybrid", lexical_available=True, vector_available=True
    )
