from __future__ import annotations

import pytest

from course_insight.modules.m0_platform.django_app.learning_projection import (
    project_finalized_assessment,
    selection_context_for_learner,
)
from course_insight.modules.m0_platform.django_app.models import (
    AssessmentProjectionReceipt,
    LearnerConceptMastery,
    User,
    WrongQuestionRecord,
)
from tests.factories.m5_m8 import (
    make_paper,
    make_scoring_bundle,
    make_task_plan,
)


pytestmark = pytest.mark.django_db


def _learner() -> User:
    actor_id = "pseudonym_projection_student"
    return User.objects.create_user(username=actor_id, actor_id=actor_id)


def _contracts(user: User, *, task_type: str, score: float):
    paper = make_paper(concept_ids=["concept_a"]).model_copy(
        update={"learner_id": user.actor_id}
    )
    task = make_task_plan(learner_id=user.actor_id).model_copy(
        update={"task_type": task_type}
    )
    scoring = make_scoring_bundle(paper, score=score)
    return task, paper, scoring


def test_diagnostic_updates_counts_caps_mastery_and_is_idempotent() -> None:
    learner = _learner()
    task, paper, scoring = _contracts(learner, task_type="diagnostic", score=1.0)

    assert project_finalized_assessment(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=scoring,
    )
    assert not project_finalized_assessment(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=scoring,
    )
    record = LearnerConceptMastery.objects.get(concept_id="concept_a")
    assert record.attempt_status == "attempted"
    assert record.attempted_count == 1 and record.correct_count == 1
    assert float(record.mastery) == 0.9
    assert AssessmentProjectionReceipt.objects.count() == 1
    assert not WrongQuestionRecord.objects.exists()


def test_practice_changes_wrong_ledger_but_not_profile() -> None:
    learner = _learner()
    task, paper, scoring = _contracts(learner, task_type="practice", score=0.0)
    project_finalized_assessment(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=scoring,
    )
    assert not LearnerConceptMastery.objects.exists()
    wrong = WrongQuestionRecord.objects.get()
    assert wrong.status == "open" and wrong.wrong_count == 1


def test_selection_context_distinguishes_unseen_and_retires_removed_items() -> None:
    learner = _learner()
    task, paper, scoring = _contracts(learner, task_type="diagnostic", score=0.0)
    project_finalized_assessment(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=scoring,
    )
    context = selection_context_for_learner(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        current_item_ids=(),
    )
    assert context.mastery_by_concept["concept_a"].attempt_status == "attempted"
    assert context.open_wrong_item_ids == ()
    assert WrongQuestionRecord.objects.get().status == "retired"


def test_multi_concept_partial_credit_attempts_every_concept_without_correct() -> None:
    learner = _learner()
    paper = make_paper(concept_ids=["concept_a", "concept_b"]).model_copy(
        update={"learner_id": learner.actor_id}
    )
    task = make_task_plan(learner_id=learner.actor_id).model_copy(
        update={"task_type": "stage_assessment"}
    )
    scoring = make_scoring_bundle(paper, score=0.5)
    project_finalized_assessment(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=scoring,
    )
    rows = tuple(LearnerConceptMastery.objects.order_by("concept_id"))
    assert tuple(row.concept_id for row in rows) == ("concept_a", "concept_b")
    assert all(row.attempted_count == 1 and row.correct_count == 0 for row in rows)
    assert all(row.attempt_status == "attempted" for row in rows)


def test_teacher_score_revision_adjusts_existing_attempt_without_double_counting() -> None:
    learner = _learner()
    task, paper, scoring = _contracts(learner, task_type="diagnostic", score=1.0)
    project_finalized_assessment(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=scoring,
    )
    revised = make_scoring_bundle(paper, score=0.0)
    assert project_finalized_assessment(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=revised,
    )
    mastery = LearnerConceptMastery.objects.get(concept_id="concept_a")
    assert mastery.attempted_count == 1 and mastery.correct_count == 0
    assert float(mastery.mastery) == 0.0
    assert WrongQuestionRecord.objects.get(item_id="item_2").status == "open"
    assert AssessmentProjectionReceipt.objects.count() == 1
