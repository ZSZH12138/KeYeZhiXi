from __future__ import annotations

import pytest
from django.db import IntegrityError

from course_insight.modules.m0_platform.django_app.learning_projection import (
    correction_records_view,
    link_correction_follow_up,
    project_finalized_assessment,
    rebuild_authoritative_profile_state,
    record_finalized_correction,
    selection_context_for_learner,
)
from course_insight.modules.m0_platform.django_app.models import (
    AssessmentProjectionReceipt,
    CourseClassWorkspace,
    LearnerConceptMastery,
    User,
    WrongQuestionRecord,
)
from tests.factories.m5_m8 import (
    make_paper,
    make_scoring_bundle,
    make_task_plan,
)
from tests.integration.test_django_student_qa import _active_release


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


def test_practice_is_not_persisted_and_does_not_change_correction_state() -> None:
    learner = _learner()
    task, paper, scoring = _contracts(learner, task_type="practice", score=0.0)
    assert not project_finalized_assessment(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=scoring,
    )
    assert not LearnerConceptMastery.objects.exists()
    assert not WrongQuestionRecord.objects.exists()
    assert not AssessmentProjectionReceipt.objects.exists()


def test_database_rejects_non_profile_projection_receipts() -> None:
    learner = _learner()
    workspace = CourseClassWorkspace.objects.create(
        course_id="course_1",
        class_id="class_1",
    )

    with pytest.raises(IntegrityError):
        AssessmentProjectionReceipt.objects.create(
            workspace=workspace,
            learner=learner,
            attempt_id="attempt_practice",
            paper_id="paper_practice",
            task_type="practice",
            scoring_checksum="a" * 64,
            projection_payload={},
        )


def test_rebuild_removes_pollution_and_preserves_real_correction_resolution() -> None:
    learner = _learner()
    task, paper, failed = _contracts(learner, task_type="diagnostic", score=0.0)
    project_finalized_assessment(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=failed,
    )
    real_wrong = WrongQuestionRecord.objects.get(item_id="item_2")
    real_wrong.status = WrongQuestionRecord.Status.RESOLVED
    real_wrong.hint_revealed = True
    real_wrong.save(update_fields=("status", "hint_revealed", "updated_at"))
    workspace = real_wrong.workspace
    LearnerConceptMastery.objects.create(
        workspace=workspace,
        learner=learner,
        concept_id="polluted_by_practice",
        attempted_count=1,
        correct_count=1,
        attempt_status="attempted",
        mastery="0.900",
    )
    WrongQuestionRecord.objects.create(
        workspace=workspace,
        learner=learner,
        item_id="practice_only_item",
        item_version="1",
        latest_attempt_id="practice_attempt",
    )

    summary = rebuild_authoritative_profile_state()

    mastery = LearnerConceptMastery.objects.get(concept_id="concept_a")
    rebuilt_wrong = WrongQuestionRecord.objects.get(item_id="item_2")
    assert summary["profile_receipts"] == 1
    assert mastery.attempted_count == 1 and mastery.correct_count == 0
    assert not LearnerConceptMastery.objects.filter(
        concept_id="polluted_by_practice"
    ).exists()
    assert not WrongQuestionRecord.objects.filter(
        item_id="practice_only_item"
    ).exists()
    assert rebuilt_wrong.status == WrongQuestionRecord.Status.RESOLVED
    assert rebuilt_wrong.hint_revealed is True


def test_failed_correction_stays_open_without_creating_profile_evidence() -> None:
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
    correction_task = task.model_copy(update={"task_type": "correction"})

    assert not record_finalized_correction(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=correction_task,
        paper=paper,
        scoring=scoring,
    )
    wrong = WrongQuestionRecord.objects.get(item_id="item_2")
    assert wrong.status == WrongQuestionRecord.Status.OPEN
    assert wrong.wrong_count == 1
    assert AssessmentProjectionReceipt.objects.count() == 1


def test_successful_correction_resolves_without_changing_mastery() -> None:
    learner = _learner()
    task, paper, failed = _contracts(learner, task_type="diagnostic", score=0.0)
    project_finalized_assessment(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=failed,
    )
    before = LearnerConceptMastery.objects.get(concept_id="concept_a")
    correction_task = task.model_copy(update={"task_type": "correction"})
    passed = make_scoring_bundle(paper, score=1.0)

    assert record_finalized_correction(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=correction_task,
        paper=paper,
        scoring=passed,
    )
    wrong = WrongQuestionRecord.objects.get(item_id="item_2")
    after = LearnerConceptMastery.objects.get(concept_id="concept_a")
    assert wrong.status == WrongQuestionRecord.Status.RESOLVED
    assert after.attempted_count == before.attempted_count == 1
    assert after.correct_count == before.correct_count == 0
    assert AssessmentProjectionReceipt.objects.count() == 1


def test_new_profile_success_does_not_close_an_unfinished_correction() -> None:
    learner = _learner()
    task, paper, failed = _contracts(learner, task_type="diagnostic", score=0.0)
    project_finalized_assessment(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=failed,
    )
    next_paper = paper.model_copy(
        update={"paper_id": "paper_2", "task_id": "task_2"},
        deep=True,
    )
    next_task = task.model_copy(update={"task_id": "task_2"}, deep=True)
    passed = make_scoring_bundle(next_paper, score=1.0).model_copy(
        update={"attempt_id": "attempt_2"},
        deep=True,
    )

    project_finalized_assessment(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=next_task,
        paper=next_paper,
        scoring=passed,
    )

    assert WrongQuestionRecord.objects.get(item_id="item_2").status == "open"


def test_later_profile_failure_reopens_a_resolved_correction() -> None:
    learner = _learner()
    task, paper, failed = _contracts(learner, task_type="diagnostic", score=0.0)
    project_finalized_assessment(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=failed,
    )
    correction_task = task.model_copy(update={"task_type": "correction"})
    passed = make_scoring_bundle(paper, score=1.0)
    record_finalized_correction(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=correction_task,
        paper=paper,
        scoring=passed,
    )
    next_paper = paper.model_copy(
        update={"paper_id": "paper_2", "task_id": "task_2"},
        deep=True,
    )
    next_task = task.model_copy(update={"task_id": "task_2"}, deep=True)
    failed_again = make_scoring_bundle(next_paper, score=0.0).model_copy(
        update={"attempt_id": "attempt_2"},
        deep=True,
    )

    project_finalized_assessment(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=next_task,
        paper=next_paper,
        scoring=failed_again,
    )

    wrong = WrongQuestionRecord.objects.get(item_id="item_2")
    assert wrong.status == WrongQuestionRecord.Status.OPEN
    assert wrong.latest_attempt_id == "attempt_2"
    assert wrong.wrong_count == 2


def test_follow_up_link_resolves_the_source_wrong_item_without_history() -> None:
    learner = _learner()
    task, paper, failed = _contracts(learner, task_type="diagnostic", score=0.0)
    project_finalized_assessment(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=failed,
    )
    follow_paper = paper.model_copy(
        update={"paper_id": "paper_follow", "task_id": "task_follow"},
        deep=True,
    )
    correction_task = task.model_copy(
        update={"task_id": "task_follow", "task_type": "correction"},
        deep=True,
    )
    passed = make_scoring_bundle(follow_paper, score=1.0).model_copy(
        update={"attempt_id": "attempt_follow"},
        deep=True,
    )
    source_instance = paper.all_items()[0]
    follow_instance = follow_paper.all_items()[0]

    link_correction_follow_up(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        source_paper_id=paper.paper_id,
        source_item_instance_id=source_instance.item_instance_id,
        source_item_id=source_instance.item_id,
        follow_up_paper_id=follow_paper.paper_id,
        follow_up_item_id=follow_instance.item_id,
    )
    linked = WrongQuestionRecord.objects.get(item_id="item_2")
    assert linked.active_follow_up_paper_id == "paper_follow"

    assert record_finalized_correction(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=correction_task,
        paper=follow_paper,
        scoring=passed,
    )
    resolved = WrongQuestionRecord.objects.get(item_id="item_2")
    assert resolved.status == WrongQuestionRecord.Status.RESOLVED
    assert resolved.resolved_through_attempt_id == "attempt_1"
    assert resolved.active_follow_up_paper_id == ""
    assert AssessmentProjectionReceipt.objects.count() == 1


def test_correction_view_is_rebuilt_from_profile_evidence_and_current_state() -> None:
    learner = _learner()
    task, paper, failed = _contracts(learner, task_type="diagnostic", score=0.0)
    project_finalized_assessment(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=failed,
    )

    records = correction_records_view(
        course_id="course_1",
        class_id="class_1",
        learner_id=learner.actor_id,
    )

    item = records[paper.paper_id]["items"][paper.all_items()[0].item_instance_id]
    assert item["stem"] == "Question"
    assert item["cause"] == "Test score."
    assert item["follow_up_correct"] is False


def test_correction_view_recovers_question_stem_when_receipt_is_missing() -> None:
    learner = _learner()
    _active_release(learner, example_count=1)
    workspace = CourseClassWorkspace.objects.get(
        course_id="course_1",
        class_id="class_1",
    )
    WrongQuestionRecord.objects.create(
        workspace=workspace,
        learner=learner,
        item_id="q-1",
        item_version="1",
        latest_attempt_id="attempt-without-receipt",
        source_paper_id="paper-without-receipt",
        source_item_instance_id="q-1-instance",
    )

    records = correction_records_view(
        course_id="course_1",
        class_id="class_1",
        learner_id=learner.actor_id,
    )

    item = records["paper-without-receipt"]["items"]["q-1-instance"]
    assert item["stem"] == "例题 1"


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
