from __future__ import annotations

from pathlib import Path

import pytest
from django.test import Client
from django.urls import reverse

from course_insight.contracts.assessment import CriterionScore
from course_insight.contracts.platform import AssessmentSubmission
from course_insight.modules.m0_platform.django_app import runtime
from course_insight.modules.m0_platform.django_app.models import (
    LearnerConceptMastery,
    TeacherItemReviewNote,
)
from course_insight.modules.m5_learner_class_state.class_snapshot import (
    current_class_learning_snapshot,
)
from tests.factories.m5_m8 import UTC_TIME, make_paper, make_scoring_bundle, make_task_plan
from tests.integration._django_web_support import (
    FakeCoordinator,
    FakeWebRuntime,
    make_user,
)
from tests.integration.test_django_student_qa import _active_release


pytestmark = pytest.mark.django_db


class _QuestionReviewCoordinator(FakeCoordinator):
    def __init__(self, learner_id: str, *, release_id: str) -> None:
        super().__init__(learner_id)
        candidate = make_paper(
            item_id="q-1",
            concept_ids=["concept-1"],
        ).model_copy(update={"learner_id": learner_id})
        self.paper = candidate.model_copy(
            update={"immutable_checksum": candidate.freeze()}
        )
        self.scoring = make_scoring_bundle(self.paper, score=0.0)
        self.task = make_task_plan(learner_id=learner_id).model_copy(
            update={
                "task_type": "diagnostic",
                "knowledge_bundle_id": release_id,
                "course_package_id": f"course-release-{release_id}",
            }
        )
        self._submission = AssessmentSubmission(
            submission_id="submission-question-review",
            attempt_id=self.scoring.attempt_id,
            paper_id=self.paper.paper_id,
            learner_id=learner_id,
            answers={self.paper.all_items()[0].item_instance_id: "学生填写的答案"},
            submitted_at=UTC_TIME,
        )
        self.review_student_evidence: str | None = None
        self.review_index_ref = None

    def get_teacher_review_context(self, **kwargs):
        assert kwargs["paper_id"] == self.paper.paper_id
        return {
            "task_plan": self.task.model_copy(deep=True),
            "assessment_paper": self.paper.model_copy(deep=True),
            "scoring_result": self.scoring.model_copy(deep=True),
            "analytics": self.analytics.model_copy(deep=True),
        }

    def frozen_assessment_submission(self, attempt_id: str):
        assert attempt_id == self.scoring.attempt_id
        return self._submission.model_copy(deep=True)

    def review_assessment(self, **kwargs):
        self.review_student_evidence = kwargs.get("student_evidence")
        self.review_index_ref = kwargs.get("index_ref")
        submission = kwargs["review_submission"]
        current = self.scoring.score_audit_records[-1]
        replacement = current.model_copy(
            update={
                "audit_version": current.audit_version + 1,
                "criterion_scores": [
                    CriterionScore(
                        criterion_id=criterion.criterion_id,
                        score=override.new_score,
                        student_evidence="学生填写的答案"
                        if override.new_score > 0
                        else "",
                        course_evidence_id=criterion.course_evidence_id,
                        reason=override.reason,
                    )
                    for criterion, override in zip(
                        current.criterion_scores,
                        submission.criterion_overrides,
                        strict=True,
                    )
                ],
                "total_score": submission.final_total_score,
                "scoring_method": "teacher_override",
                "review_status": "approved",
                "review_reason": ["teacher_item_rescore"],
            },
            deep=True,
        )
        self.scoring = self.scoring.model_copy(
            update={
                "score_audit_records": [
                    *self.scoring.score_audit_records,
                    replacement,
                ],
                "total_score": submission.final_total_score,
            },
            deep=True,
        )
        return {}


def test_teacher_review_context_shows_question_answer_score_and_single_rescore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher = make_user(
        actor_id="pseudonym_question_review_teacher",
        role="teacher",
        permissions=(
            "view_class_analytics",
            "view_student_report",
            "review_score",
        ),
    )
    learner = make_user(
        actor_id="pseudonym_question_review_student",
        role="student",
        permissions=("view_own_result",),
    )
    source = _active_release(teacher, example_count=1)
    release = source.versions.first().concept_references.first().concept.release
    coordinator = _QuestionReviewCoordinator(
        learner.actor_id,
        release_id=str(release.pk),
    )
    monkeypatch.setattr(
        runtime,
        "get_web_runtime",
        lambda: FakeWebRuntime(coordinator=coordinator, runtime_dir=tmp_path),
    )
    client = Client()
    client.force_login(teacher)

    response = client.get(
        reverse(
            "teacher-review-context",
            kwargs={
                "course_id": "course_1",
                "class_id": "class_1",
                "paper_id": coordinator.paper.paper_id,
            },
        )
    )

    content = response.content.decode("utf-8")
    assert response.status_code == 200, content
    assert "Question" in content
    assert "学生答案" in content
    assert "学生填写的答案" in content
    assert "学生得分" in content
    assert "AI评判" in content
    assert "教师备注" in content
    assert "拥塞控制" in content
    assert content.count("改判本题") == 1


def test_item_rescore_persists_note_and_refreshes_profile_and_class_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher = make_user(
        actor_id="pseudonym_item_rescore_teacher",
        role="teacher",
        permissions=(
            "view_class_analytics",
            "view_student_report",
            "review_score",
        ),
    )
    learner = make_user(
        actor_id="pseudonym_item_rescore_student",
        role="student",
        permissions=("view_own_result",),
    )
    source = _active_release(teacher, example_count=1)
    release = source.versions.first().concept_references.first().concept.release
    coordinator = _QuestionReviewCoordinator(
        learner.actor_id,
        release_id=str(release.pk),
    )
    monkeypatch.setattr(
        runtime,
        "get_web_runtime",
        lambda: FakeWebRuntime(coordinator=coordinator, runtime_dir=tmp_path),
    )
    client = Client()
    client.force_login(teacher)
    context_url = reverse(
        "teacher-review-context",
        kwargs={
            "course_id": "course_1",
            "class_id": "class_1",
            "paper_id": coordinator.paper.paper_id,
        },
    )
    page = client.get(context_url)
    review = page.context["question_reviews"][0]

    response = client.post(
        review.action_url,
        {
            "score": "1",
            "teacher_note": "核对后判为正确。",
            "flow_token": review.form.initial["flow_token"],
        },
    )

    assert response.status_code == 302
    assert response.url == context_url
    assert coordinator.review_student_evidence == "学生填写的答案"
    note = TeacherItemReviewNote.objects.get()
    assert note.teacher_note == "核对后判为正确。"
    mastery = LearnerConceptMastery.objects.get(
        learner=learner,
        concept_id="concept-1",
    )
    assert mastery.correct_count == 1
    snapshot = current_class_learning_snapshot(
        course_id="course_1",
        class_id="class_1",
    )
    assert snapshot is not None
    assert snapshot.concepts[0].average_mastery == 0.9
