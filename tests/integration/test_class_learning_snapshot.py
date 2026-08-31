from __future__ import annotations

import re
import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags

from course_insight.modules.m0_platform.django_app.models import (
    ActorGrant,
    AssessmentProjectionReceipt,
    CourseClassWorkspace,
    CourseKnowledgeRelease,
    CourseSource,
    CourseSourceVersion,
    KnowledgeIngestionJob,
    LearnerConceptMastery,
    LearningProfileProjectionEvent,
    ReleaseConcept,
    ReleaseConceptSource,
    SuggestedTeacherReviewCase,
    SuggestedTeacherReviewItem,
    User,
)
from course_insight.modules.m0_platform.django_app.learning_projection import (
    project_finalized_assessment,
)
from course_insight.modules.m0_platform.django_app import runtime
from course_insight.modules.m0_platform.django_app.scoped_deepseek import (
    save_scoped_deepseek_settings,
)
from course_insight.modules.m5_learner_class_state.class_snapshot import (
    current_class_learning_snapshot,
    synchronize_class_learning_snapshot,
)
from course_insight.modules.m0_platform.django_app.teaching_advice import (
    build_class_teaching_advice_prompt,
)
from course_insight.modules.m9_teacher_analytics.teaching_advice import (
    TeachingAdviceResult,
)
from course_insight.modules.m0_platform.django_app.views import (
    teacher as teacher_views,
)
from tests.factories.m5_m8 import make_paper, make_scoring_bundle, make_task_plan
from tests.integration._django_web_support import FakeCoordinator, FakeWebRuntime, make_user


pytestmark = pytest.mark.django_db


def _student(actor_id: str) -> User:
    user = User.objects.create_user(username=actor_id, actor_id=actor_id)
    ActorGrant.objects.create(
        user=user,
        role="student",
        course_id="course_1",
        class_id="class_1",
        source_checksum="a" * 64,
    )
    return user


def _workspace(owner: User) -> CourseClassWorkspace:
    source = CourseSource.objects.create(
        course_id="course_1",
        class_id="class_1",
        display_name="knowledge.txt",
        source_type="knowledge",
        status="active",
        created_by=owner,
    )
    CourseSourceVersion.objects.create(
        source=source,
        version_number=1,
        storage_key=f"{uuid.uuid4().hex}/{uuid.uuid4()}",
        sha256="b" * 64,
        media_type="text/plain",
        size_bytes=1,
        status="active",
    )
    job = KnowledgeIngestionJob.objects.create(
        course_id="course_1",
        class_id="class_1",
        requested_by=owner,
        change_set_checksum="c" * 64,
        status="succeeded",
        progress=100,
    )
    release = CourseKnowledgeRelease.objects.create(
        course_id="course_1",
        class_id="class_1",
        version_number=1,
        status="active",
        job=job,
        content_checksum="d" * 64,
    )
    ReleaseConcept.objects.create(
        release=release,
        concept_id="tcp",
        name="传输控制协议",
        description="可靠传输",
    )
    ReleaseConcept.objects.create(
        release=release,
        concept_id="udp",
        name="用户数据报协议",
        description="无连接传输",
    )
    return CourseClassWorkspace.objects.create(
        course_id="course_1",
        class_id="class_1",
        active_release=release,
        content_revision=1,
    )


def _mastery(
    workspace: CourseClassWorkspace,
    learner: User,
    *,
    concept_id: str,
    mastery: str,
) -> None:
    LearnerConceptMastery.objects.create(
        workspace=workspace,
        learner=learner,
        concept_id=concept_id,
        attempted_count=1,
        correct_count=1 if mastery != "0" else 0,
        attempt_status="attempted",
        mastery=Decimal(mastery),
    )


def test_snapshot_uses_active_roster_and_excludes_unattempted_from_rates() -> None:
    first = _student("pseudonym_snapshot_first")
    second = _student("pseudonym_snapshot_second")
    _student("pseudonym_snapshot_unattempted")
    workspace = _workspace(first)
    _mastery(workspace, first, concept_id="tcp", mastery="0.800")
    _mastery(workspace, second, concept_id="tcp", mastery="0.200")

    snapshot = synchronize_class_learning_snapshot(
        workspace=workspace,
        reason="test",
    )
    concepts = {item.concept_id: item for item in snapshot.concepts}

    tcp = concepts["tcp"]
    assert tcp.name == "传输控制协议"
    assert tcp.attempted_student_count == 2
    assert tcp.unattempted_student_count == 1
    assert tcp.average_mastery == pytest.approx(0.5)
    assert tcp.priority_support_rate == pytest.approx(0.5)
    # No learner has attempted UDP: it stays visible, but the class mastery is 0.
    udp = concepts["udp"]
    assert udp.attempted_student_count == 0
    assert udp.unattempted_student_count == 3
    assert udp.average_mastery == 0.0
    assert udp.priority_support_rate == 0.0

    reloaded = current_class_learning_snapshot(
        course_id="course_1",
        class_id="class_1",
    )
    assert reloaded == snapshot


def test_snapshot_excludes_disabled_revoked_expired_and_other_class_students() -> None:
    active = _student("pseudonym_roster_active")
    disabled = _student("pseudonym_roster_disabled")
    disabled.is_active = False
    disabled.save(update_fields=("is_active",))

    revoked = _student("pseudonym_roster_revoked")
    ActorGrant.objects.filter(user=revoked).update(
        is_active=False,
        revoked_at=timezone.now(),
    )

    expired = _student("pseudonym_roster_expired")
    ActorGrant.objects.filter(user=expired).update(
        valid_from=timezone.now() - timedelta(days=2),
        valid_until=timezone.now() - timedelta(days=1),
    )

    other_class = _student("pseudonym_roster_other_class")
    ActorGrant.objects.filter(user=other_class).update(class_id="class_2")
    workspace = _workspace(active)

    snapshot = synchronize_class_learning_snapshot(
        workspace=workspace,
        reason="roster_test",
    )

    assert snapshot.active_student_count == 1
    assert all(
        concept.unattempted_student_count == 1
        for concept in snapshot.concepts
    )


def test_profile_projection_emits_an_idempotent_sync_event_and_refreshes_snapshot() -> None:
    learner = _student("pseudonym_snapshot_projection")
    workspace = _workspace(learner)
    paper = make_paper(concept_ids=["tcp"]).model_copy(
        update={"learner_id": learner.actor_id}
    )
    task = make_task_plan(learner_id=learner.actor_id).model_copy(
        update={"task_type": "diagnostic"}
    )

    assert project_finalized_assessment(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=make_scoring_bundle(paper, score=1.0),
    )
    initial = current_class_learning_snapshot(
        course_id="course_1",
        class_id="class_1",
    )
    assert initial is not None
    assert {item.concept_id: item for item in initial.concepts}["tcp"].average_mastery == 0.9
    assert LearningProfileProjectionEvent.objects.filter(
        workspace=workspace
    ).count() == 1

    assert project_finalized_assessment(
        course_id="course_1",
        class_id="class_1",
        learner=learner,
        task=task,
        paper=paper,
        scoring=make_scoring_bundle(paper, score=0.0),
    )
    revised = current_class_learning_snapshot(
        course_id="course_1",
        class_id="class_1",
    )
    assert revised is not None
    tcp = {item.concept_id: item for item in revised.concepts}["tcp"]
    assert tcp.average_mastery == 0.0
    assert tcp.priority_support_count == 1
    assert LearningProfileProjectionEvent.objects.filter(workspace=workspace).count() == 2


def test_teacher_class_map_uses_named_m5_snapshot_and_has_no_paper_preview(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher = make_user(
        actor_id="pseudonym_snapshot_teacher",
        role="teacher",
        permissions=("view_class_analytics",),
    )
    learner = User.objects.create_user(
        username="student_demo1",
        actor_id="pseudonym_snapshot_page_student",
    )
    ActorGrant.objects.create(
        user=learner,
        role="student",
        course_id="course_1",
        class_id="class_1",
        source_checksum="a" * 64,
    )
    second_learner = User.objects.create_user(
        username="student_demo2",
        actor_id="pseudonym_snapshot_page_student_2",
    )
    ActorGrant.objects.create(
        user=second_learner,
        role="student",
        course_id="course_1",
        class_id="class_1",
        source_checksum="b" * 64,
    )
    workspace = _workspace(teacher)
    _mastery(workspace, learner, concept_id="tcp", mastery="0.000")
    synchronize_class_learning_snapshot(workspace=workspace, reason="test")
    review_case = SuggestedTeacherReviewCase.objects.create(
        workspace=workspace,
        learner=second_learner,
        attempt_id="attempt-student-demo2-review",
        paper_id="paper-student-demo2-review",
        task_type="diagnostic",
        scoring_checksum="e" * 64,
        attempted_at=timezone.now(),
    )
    SuggestedTeacherReviewItem.objects.create(
        case=review_case,
        item_instance_id="item-student-demo2-review",
        audit_id="audit-student-demo2-review",
        audit_version=1,
        confidence=Decimal("0.500"),
        review_reasons=["low_confidence"],
    )
    coordinator = FakeCoordinator(learner.actor_id)
    coordinator.analytics = coordinator.analytics.model_copy(
        update={
            "individual_reports": [
                coordinator.analytics.individual_reports[0].model_copy(
                    update={
                        "weak_concept_ids": ["concept_should_not_appear"],
                        "active_misconception_ids": [
                            "misconception_should_not_appear"
                        ],
                    }
                )
            ]
        },
        deep=True,
    )
    web_runtime = FakeWebRuntime(
        coordinator=coordinator,
        runtime_dir=tmp_path,
    )
    second_analytics = FakeCoordinator(second_learner.actor_id).analytics.model_copy(
        update={
            "individual_reports": [
                FakeCoordinator(second_learner.actor_id)
                .analytics.individual_reports[0]
                .model_copy(update={"review_required_count": 0})
            ]
        },
        deep=True,
    )

    def latest_analytics(*, learner_id=None, **_):
        if learner_id is None or learner_id == learner.actor_id:
            return coordinator.analytics.model_copy(deep=True)
        if learner_id == second_learner.actor_id:
            return second_analytics.model_copy(deep=True)
        return None

    web_runtime.container.m9_service = SimpleNamespace(
        get_latest_analytics=latest_analytics
    )
    monkeypatch.setattr(runtime, "get_web_runtime", lambda: web_runtime)
    client = Client()
    client.force_login(teacher)

    response = client.get(
        reverse(
            "teacher-class",
            kwargs={"course_id": "course_1", "class_id": "class_1"},
        )
    )

    content = response.content.decode("utf-8")
    visible_text = strip_tags(content)
    assert response.status_code == 200, content
    assert "传输控制协议" in content
    assert "未做 1 人" in content
    assert "生成卷预览" not in content
    assert '<details class="card" id="class-map">' in content
    assert '<details class="card" id="class-map" open>' not in content
    assert "规则建议" not in content
    assert "薄弱：concept_should_not_appear" not in content
    assert "误区：misconception_should_not_appear" not in content
    assert "查看该生掌握、错因、订正" in content
    assert "student_demo1：掌握度" in visible_text
    assert "student_demo2：掌握度" in visible_text
    assert re.search(
        r"<summary>student_demo2：掌握度 [^<]*待复核 1</summary>",
        content,
    )
    assert learner.actor_id not in visible_text
    assert second_learner.actor_id not in visible_text
    assert "<h2>教学建议</h2>" not in content


def test_teacher_review_lookup_uses_course_class_and_student_account(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher = make_user(
        actor_id="pseudonym_review_lookup_teacher",
        role="teacher",
        permissions=("view_student_report", "review_score"),
    )
    learner = _student("pseudonym_review_lookup_student")
    workspace = _workspace(teacher)
    AssessmentProjectionReceipt.objects.create(
        workspace=workspace,
        learner=learner,
        attempt_id="attempt-profile-review",
        paper_id="paper-profile-review",
        task_type="diagnostic",
        scoring_checksum="e" * 64,
        projection_payload={"item": {"correct": False}},
    )
    monkeypatch.setattr(
        runtime,
        "get_web_runtime",
        lambda: FakeWebRuntime(
            coordinator=FakeCoordinator(learner.actor_id),
            runtime_dir=tmp_path,
        ),
    )
    client = Client()
    client.force_login(teacher)

    response = client.get(
        reverse("teacher-review-lookup"),
        {
            "course_id": "course_1",
            "class_id": "class_1",
            "learner_account": learner.actor_id,
        },
    )

    expected = reverse(
        "teacher-learner-review-list",
        kwargs={
            "course_id": "course_1",
            "class_id": "class_1",
            "learner_id": learner.actor_id,
        },
    )
    assert response.status_code == 302
    assert response["Location"] == expected
    page = client.get(expected)
    assert page.status_code == 200
    assert "paper-profile-review" in page.content.decode("utf-8")


def test_teaching_advice_uses_current_snapshot_weak_concepts_and_source_text() -> None:
    learner = _student("pseudonym_snapshot_advice_student")
    workspace = _workspace(learner)
    release = workspace.active_release
    assert release is not None
    concept = ReleaseConcept.objects.get(release=release, concept_id="tcp")
    version = CourseSourceVersion.objects.get(
        source__course_id="course_1",
        source__class_id="class_1",
    )
    ReleaseConceptSource.objects.create(
        concept=concept,
        source_version=version,
        chunk_id="tcp-definition",
        locator="paragraph:1",
        chunk_text="TCP 通过确认与重传机制提供可靠传输。",
        span_start=0,
        span_end=3,
        relation_type="definition",
    )
    _mastery(workspace, learner, concept_id="tcp", mastery="0.200")
    snapshot = synchronize_class_learning_snapshot(
        workspace=workspace,
        reason="test",
    )

    prompt = build_class_teaching_advice_prompt(
        snapshot=snapshot,
        weak_mastery_threshold=0.5,
    )

    assert prompt.concept_names == ("传输控制协议",)
    assert "TCP 通过确认与重传机制提供可靠传输。" in prompt.messages[1][
        "content"
    ]
    assert "用户数据报协议" not in prompt.messages[1]["content"]


def test_teacher_class_page_generates_one_source_bound_teaching_advice(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    teacher = make_user(
        actor_id="pseudonym_snapshot_advice_teacher",
        role="teacher",
        permissions=("view_class_analytics",),
    )
    learner = _student("pseudonym_snapshot_advice_page_student")
    workspace = _workspace(teacher)
    release = workspace.active_release
    assert release is not None
    concept = ReleaseConcept.objects.get(release=release, concept_id="tcp")
    version = CourseSourceVersion.objects.get(
        source__course_id="course_1",
        source__class_id="class_1",
    )
    ReleaseConceptSource.objects.create(
        concept=concept,
        source_version=version,
        chunk_id="tcp-teaching-source",
        locator="paragraph:1",
        chunk_text="TCP 的可靠传输依赖确认、重传和顺序控制。",
        span_start=0,
        span_end=3,
        relation_type="definition",
    )
    _mastery(workspace, learner, concept_id="tcp", mastery="0.200")
    save_scoped_deepseek_settings(
        course_id="course_1",
        class_id="class_1",
        api_key="sk-teaching-advice-test-key",
        model_name="deepseek-v4-flash",
        thinking_enabled=False,
        updated_by=teacher,
    )
    calls: list[object] = []

    def _generate(**kwargs):
        calls.append(kwargs["prompt"])
        return TeachingAdviceResult(
            advice="先围绕可靠传输的确认与重传进行短讲，再安排一题变式练习检查理解。",
            concept_names=kwargs["prompt"].concept_names,
            citation_ids=(),
        )

    monkeypatch.setattr(teacher_views, "generate_teaching_advice", _generate)
    monkeypatch.setattr(
        teacher_views,
        "_class_weak_mastery_threshold",
        lambda course: 0.5,
    )
    monkeypatch.setattr(
        runtime,
        "get_web_runtime",
        lambda: FakeWebRuntime(
            coordinator=FakeCoordinator(teacher.actor_id),
            runtime_dir=tmp_path,
        ),
    )
    client = Client()
    client.force_login(teacher)
    class_url = reverse(
        "teacher-class",
        kwargs={"course_id": "course_1", "class_id": "class_1"},
    )

    page = client.get(class_url)
    content = page.content.decode("utf-8")
    assert page.status_code == 200, content
    assert "生成教学建议" in content
    assert "sk-teaching-advice-test-key" not in content
    flow = re.search(
        r'name="flow_token" value="([^"]+)"',
        content,
    )
    assert flow is not None

    response = client.post(
        reverse(
            "teacher-class-teaching-advice",
            kwargs={"course_id": "course_1", "class_id": "class_1"},
        ),
        {"flow_token": flow.group(1)},
    )

    response_content = response.content.decode("utf-8")
    assert response.status_code == 200, response_content
    assert "先围绕可靠传输" in response_content
    assert len(calls) == 1
    assert calls[0].concept_names == ("传输控制协议",)
