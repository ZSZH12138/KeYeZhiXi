from __future__ import annotations

import math

from course_insight.modules.m0_platform.django_app.forms.review import (
    TeacherItemRescoreForm,
)
from course_insight.modules.m0_platform.django_app.views.viewmodels import (
    criterion_caps,
)
from tests.factories.m5_m8 import (
    make_knowledge_bundle,
    make_paper,
    make_scoring_bundle,
)


def test_item_rescore_accepts_one_bounded_score_and_optional_note() -> None:
    paper = make_paper()
    audit = make_scoring_bundle(paper).score_audit_records[0]
    form = TeacherItemRescoreForm(
        audit=audit,
        reviewer_id="pseudonym_teacher_1",
        criterion_caps={audit.criterion_scores[0].criterion_id: 1.0},
        data={
            "score": "0.5",
            "teacher_note": "保留部分过程分",
            "flow_token": "signed-flow",
        },
    )

    assert form.is_valid(), form.errors
    submission = form.to_submission(
        submission_id="review_submission_1",
        submitted_at=audit.created_at,
    )
    assert submission.decision == "override"
    assert submission.final_total_score == 0.5
    assert submission.teacher_comment == "保留部分过程分"
    assert math.isclose(
        sum(item.new_score for item in submission.criterion_overrides),
        0.5,
    )


def test_item_rescore_rejects_a_score_above_the_item_maximum() -> None:
    paper = make_paper()
    audit = make_scoring_bundle(paper).score_audit_records[0]
    form = TeacherItemRescoreForm(
        audit=audit,
        reviewer_id="pseudonym_teacher_1",
        criterion_caps={audit.criterion_scores[0].criterion_id: 1.0},
        data={
            "score": "1.1",
            "teacher_note": "",
            "flow_token": "signed-flow",
        },
    )

    assert not form.is_valid()
    assert "score" in form.errors


def test_legacy_rule_scored_subjective_item_keeps_its_single_audit_cap() -> None:
    """A historical rule-scored subjective item must remain rejudgeable."""

    paper = make_paper(subjective=True)
    audit = make_scoring_bundle(paper, score=0.0).score_audit_records[0]

    assert criterion_caps(
        paper=paper,
        audit=audit,
        knowledge_bundle=make_knowledge_bundle(subjective=True),
    ) == {audit.criterion_scores[0].criterion_id: audit.max_score}
