"""Objective scoring accepts only governed, finite scalar answers."""

import math

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.modules.m8_assessment_scoring.paper_generator import (
    PaperGenerator,
)
from course_insight.modules.m8_assessment_scoring.rule_scorer import RuleScorer
from tests.factories.m5_m8 import (
    FixedClock,
    UTC_TIME,
    make_knowledge_bundle,
    make_task_plan,
)


def _objective_case():
    bundle = make_knowledge_bundle(subjective=False)
    paper = PaperGenerator(FixedClock()).generate(
        make_task_plan(),
        bundle,
        None,
        None,
    )
    return bundle.items[0], paper.all_items()[0]


@pytest.mark.parametrize(
    ("value", "normalized", "displayed"),
    [
        (7, "7", "7"),
        (2.5, "2.5", "2.5"),
        (True, "true", "true"),
        ("  Yes  ", "Yes", "Yes"),
        ("TCP  /IP", "TCP  /IP", "TCP  /IP"),
    ],
)
def test_rule_scorer_normalizes_supported_json_scalars(
    value: object,
    normalized: str,
    displayed: str,
) -> None:
    assert RuleScorer.normalize(value) == normalized
    assert RuleScorer.display(value) == displayed


@pytest.mark.parametrize(
    ("value", "displayed"),
    [(["yes"], ""), (math.inf, "inf"), (math.nan, "nan")],
)
def test_rule_scorer_rejects_non_scalar_or_non_finite_answers(
    value: object,
    displayed: str,
) -> None:
    with pytest.raises(DomainError) as captured:
        RuleScorer.normalize(value)
    assert captured.value.code == "ANSWER_FORMAT_INVALID"
    assert RuleScorer.display(value) == displayed


def test_rule_scorer_rejects_misaligned_item_and_missing_answer_key() -> None:
    item, instance = _objective_case()
    scorer = RuleScorer(FixedClock())

    with pytest.raises(DomainError, match="aligned objective"):
        scorer.score(
            attempt_id="attempt_1",
            item_instance=instance,
            item=item.model_copy(update={"version": "2.0.0"}),
            raw_answer="yes",
        )
    with pytest.raises(DomainError, match="answer key"):
        scorer.score(
            attempt_id="attempt_1",
            item_instance=instance,
            item=item.model_copy(update={"answer_key": {"max_score": 1.0}}),
            raw_answer="yes",
        )


def test_rule_scorer_accepts_any_teacher_listed_string_answer() -> None:
    item, instance = _objective_case()
    item = item.model_copy(
        update={
            "answer_key": {
                "answers": ["IPv6", "ipv6"],
                "max_score": 1.0,
            }
        }
    )

    scorer = RuleScorer(FixedClock())
    first = scorer.score(
        attempt_id="attempt_1",
        item_instance=instance,
        item=item,
        raw_answer="IPv6",
    )
    second = scorer.score(
        attempt_id="attempt_1",
        item_instance=instance,
        item=item,
        raw_answer="  ipv6  ",
    )
    missed = scorer.score(
        attempt_id="attempt_1",
        item_instance=instance,
        item=item,
        raw_answer="Ipv6",
    )

    assert first.total_score == 1.0
    assert second.total_score == 1.0
    assert missed.total_score == 0.0
    assert missed.review_status == "not_required"


def test_rule_scorer_rejects_empty_or_non_list_answers() -> None:
    item, instance = _objective_case()
    scorer = RuleScorer(FixedClock())

    with pytest.raises(DomainError, match="answer key"):
        scorer.score(
            attempt_id="attempt_1",
            item_instance=instance,
            item=item.model_copy(update={"answer_key": {"answers": [], "max_score": 1.0}}),
            raw_answer="yes",
        )
    with pytest.raises(DomainError, match="answer key"):
        scorer.score(
            attempt_id="attempt_1",
            item_instance=instance,
            item=item.model_copy(
                update={"answer_key": {"answers": "yes", "max_score": 1.0}}
            ),
            raw_answer="yes",
        )


def test_rule_scorer_uses_injected_clock_for_audit_time() -> None:
    item, instance = _objective_case()

    result = RuleScorer(FixedClock(UTC_TIME)).score(
        attempt_id="attempt_1",
        item_instance=instance,
        item=item,
        raw_answer="yes",
    )

    assert result.total_score == 1.0
    assert result.created_at == UTC_TIME
