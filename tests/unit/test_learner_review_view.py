"""Teacher can open one learner's causes and correction results."""

from __future__ import annotations

from course_insight.modules.m0_platform.django_app.views.viewmodels import (
    learner_review_view,
)
from tests.factories.m5_m8 import make_knowledge_bundle, make_paper, make_scoring_bundle


def test_learner_review_lists_causes_and_per_item_correction_status() -> None:
    bundle = make_knowledge_bundle(subjective=False)
    paper = make_paper(subjective=False)
    scoring = make_scoring_bundle(paper, score=0.0)
    records = {
        paper.paper_id: {
            "learner_id": paper.learner_id,
            "course_id": "course_1",
            "class_id": "class_1",
            "items": {
                paper.all_items()[0].item_instance_id: {
                    "stem": "Question",
                    "cause": "书写与标准答案不一致。",
                    "concept_ids": ["concept_2"],
                    "hint_revealed": True,
                    "follow_up_paper_id": "paper_fu",
                    "follow_up_correct": True,
                }
            },
        }
    }
    view = learner_review_view(
        learner_id=paper.learner_id,
        knowledge_bundle=bundle,
        snapshot=None,
        records=records,
        paper=paper,
        scoring=scoring,
    )

    assert view.learner_id == paper.learner_id
    assert view.lost_items[0].cause == "书写与标准答案不一致。"
    assert view.lost_items[0].status == "提示后改对"
