"""Parameter substitution freezes the matching objective answers."""

from __future__ import annotations

from course_insight.contracts.knowledge import ParameterRule
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


def test_frozen_instance_answers_follow_selected_parameter_and_score_exactly() -> None:
    bundle = make_knowledge_bundle(subjective=False)
    item = bundle.items[0].model_copy(
        update={
            "stem": "Write the selected protocol {protocol}.",
            "parameter_rules": [
                ParameterRule(
                    name="protocol",
                    value_type="string",
                    minimum=None,
                    maximum=None,
                    choices=["IPv4", "IPv6"],
                    constraints=[],
                )
            ],
            "answer_key": {
                "answer": "{protocol}",
                "answers": ["{protocol}"],
                "max_score": 1.0,
            },
        }
    )
    bundle = bundle.model_copy(update={"items": [item]})
    paper = PaperGenerator(FixedClock(UTC_TIME)).generate(
        make_task_plan(),
        bundle,
        None,
        None,
    )
    instance = paper.all_items()[0]
    frozen = instance.parameters.get("_frozen_answers")
    protocol = instance.parameters["protocol"]

    assert frozen == [protocol]
    assert protocol in instance.stem
    audit = RuleScorer(FixedClock(UTC_TIME)).score(
        attempt_id="attempt_1",
        item_instance=instance,
        item=item,
        raw_answer=protocol,
    )
    wrong = RuleScorer(FixedClock(UTC_TIME)).score(
        attempt_id="attempt_1",
        item_instance=instance,
        item=item,
        raw_answer="other",
    )
    assert audit.total_score == 1.0
    assert wrong.total_score == 0.0
