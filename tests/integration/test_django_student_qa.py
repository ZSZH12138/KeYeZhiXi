from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import reverse

from course_insight.modules.m0_platform.django_app.models import (
    ActorGrant,
    CourseClassWorkspace,
    CourseKnowledgeRelease,
    CourseSource,
    CourseSourceVersion,
    KnowledgeIngestionJob,
    ReleaseConcept,
    ReleaseConceptSource,
    ReleaseQuestion,
    ReleaseQuestionConceptLink,
    User,
)
from course_insight.modules.m0_platform.django_app.scoped_deepseek import (
    ScopedDeepSeekSettings,
    save_scoped_deepseek_settings,
)
from course_insight.modules.m0_platform.django_app.views.student_qa import (
    build_student_qa_adapter,
)
from course_insight.modules.m7_local_model.student_qa import (
    StudentQAAnswer,
    StudentQACitation,
    StudentQAExternalSource,
    StudentQAMatchedConcept,
)


pytestmark = pytest.mark.django_db


class _FakeAdapter:
    def __init__(self) -> None:
        self.example_count = 0

    def answer(self, question, concepts, context_loader, *, model_ref, created_at):
        del question, model_ref, created_at
        context = context_loader(concepts[0].concept_id)
        self.example_count = len(context.examples)
        evidence = context.evidence[0]
        return StudentQAAnswer(
            status="answered",
            concept_id=concepts[0].concept_id,
            concept_name=f"{concepts[0].name}、慢启动",
            matched_concepts=(
                StudentQAMatchedConcept(
                    concept_id=concepts[0].concept_id,
                    name=concepts[0].name,
                    confidence=0.94,
                ),
                StudentQAMatchedConcept(
                    concept_id="concept-2",
                    name="慢启动",
                    confidence=0.86,
                ),
            ),
            sources=(
                StudentQACitation(
                    evidence_id=evidence.evidence_id,
                    source_version_id=evidence.source_version_id,
                    file_name=evidence.file_name,
                    locator=evidence.locator,
                    quote=evidence.text,
                ),
            ),
            analysis="<script>alert(1)</script> 根据原文可知应控制网络负载。",
            examples=context.examples,
        )


def _student(course_id: str = "course_1", class_id: str = "class_1") -> User:
    actor_id = f"pseudonym_student_qa_{course_id}_{class_id}"
    user = User.objects.create_user(username=actor_id, actor_id=actor_id)
    group, _ = Group.objects.get_or_create(name=f"qa-{course_id}-{class_id}")
    group.permissions.add(
        Permission.objects.get(
            content_type__app_label="m0_platform_web",
            codename="ask_course_question",
        )
    )
    user.groups.add(group)
    ActorGrant.objects.create(
        user=user,
        role="student",
        course_id=course_id,
        class_id=class_id,
        source_checksum="a" * 64,
    )
    return user


def _active_release(
    user: User,
    *,
    example_count: int = 3,
    with_choice_options: bool = False,
):
    source = CourseSource.objects.create(
        course_id="course_1",
        class_id="class_1",
        display_name="chapter.txt",
        source_type="knowledge",
        status="active",
        created_by=user,
    )
    version = CourseSourceVersion.objects.create(
        source=source,
        version_number=1,
        storage_key=f"{uuid.uuid4().hex}/{uuid.uuid4()}",
        sha256="a" * 64,
        media_type="text/plain",
        size_bytes=10,
        status="active",
    )
    question_source = CourseSource.objects.create(
        course_id="course_1",
        class_id="class_1",
        display_name="questions.txt",
        source_type="question",
        status="active",
        created_by=user,
    )
    question_version = CourseSourceVersion.objects.create(
        source=question_source,
        version_number=1,
        storage_key=f"{uuid.uuid4().hex}/{uuid.uuid4()}",
        sha256="b" * 64,
        media_type="text/plain",
        size_bytes=10,
        status="active",
    )
    job = KnowledgeIngestionJob.objects.create(
        course_id="course_1",
        class_id="class_1",
        requested_by=user,
        change_set_checksum="b" * 64,
        status="succeeded",
        progress=100,
    )
    release = CourseKnowledgeRelease.objects.create(
        course_id="course_1",
        class_id="class_1",
        version_number=1,
        status="active",
        job=job,
        content_checksum="c" * 64,
    )
    concept = ReleaseConcept.objects.create(
        release=release,
        concept_id="concept-1",
        name="拥塞控制",
        description="避免网络过载",
        aliases=["网络拥塞"],
    )
    ReleaseConceptSource.objects.create(
        concept=concept,
        source_version=version,
        chunk_id="chunk-1",
        locator="paragraph:1",
        span_start=0,
        span_end=4,
        relation_type="definition",
        chunk_text="拥塞控制用于避免网络过载。",
    )
    for index in range(example_count):
        is_choice = with_choice_options and index == 0
        question = ReleaseQuestion.objects.create(
            release=release,
            question_id=f"q-{index + 1}",
            source_version=question_version,
            question_type="choice" if is_choice else "fill_blank",
            ordinal=index + 1,
            locator=f"question:{index + 1}",
            stem=(
                "下列哪项符合UDP的主要特点?"
                if is_choice
                else f"例题 {index + 1}"
            ),
            payload=(
                {
                    "options": {
                        "A": "面向连接并保证可靠传输",
                        "B": "无连接并保留应用报文边界",
                        "C": "建立连接后才能发送数据",
                        "D": "只支持字节流传输",
                    },
                    "accepted_answers": ["B"],
                }
                if is_choice
                else {"accepted_answers": [f"答案 {index + 1}"]}
            ),
        )
        ReleaseQuestionConceptLink.objects.create(
            question=question,
            concept=concept,
            confidence=0.9,
            status="usable",
            evidence=[],
        )
    CourseClassWorkspace.objects.create(
        course_id="course_1",
        class_id="class_1",
        active_release=release,
        content_revision=1,
    )
    return source


def test_student_qa_requires_exact_course_class_scope(client: Client) -> None:
    client.force_login(_student("course_2", "class_2"))
    response = client.get(
        reverse(
            "student-qa",
            kwargs={"course_id": "course_1", "class_id": "class_1"},
        )
    )
    assert response.status_code == 403


def test_student_qa_is_disabled_without_teacher_scoped_key(
    client: Client, monkeypatch
) -> None:
    student = _student()
    _active_release(student)
    client.force_login(student)
    monkeypatch.setattr(
        "course_insight.modules.m0_platform.django_app.views.student_qa.build_student_qa_adapter",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not build")),
    )

    response = client.post(
        reverse(
            "student-qa",
            kwargs={"course_id": "course_1", "class_id": "class_1"},
        ),
        {"question": "拥塞控制有什么作用？"},
    )

    assert response.status_code == 200
    assert "教师尚未为当前课程班级配置 DeepSeek API" in response.content.decode()


def test_qa_builder_reuses_teacher_key_for_fixed_flash_web_search(
    monkeypatch,
) -> None:
    calls: list[dict[str, object]] = []

    class _Client:
        def __init__(self, **kwargs) -> None:
            calls.append(kwargs)
            self.model_name = str(kwargs["model_name"])
            self.model_version = "runtime-api"

    monkeypatch.setattr(
        "course_insight.modules.m0_platform.django_app.views.student_qa.DeepSeekClient",
        _Client,
    )

    build_student_qa_adapter(
        ScopedDeepSeekSettings(
            api_key="teacher-scoped-key",
            model_name="deepseek-v4-pro",
            thinking_enabled=True,
            api_revision=3,
        )
    )

    assert [call["model_name"] for call in calls] == [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    ]
    assert [call["api_key"] for call in calls] == [
        "teacher-scoped-key",
        "teacher-scoped-key",
    ]


def test_answer_renders_required_sections_and_at_most_two_examples(
    client: Client, monkeypatch
) -> None:
    student = _student()
    _active_release(student, example_count=3)
    save_scoped_deepseek_settings(
        course_id="course_1",
        class_id="class_1",
        api_key="sk-student-qa-test-key",
        model_name="deepseek-v4-flash",
        thinking_enabled=False,
        updated_by=student,
    )
    adapter = _FakeAdapter()
    client.force_login(student)
    monkeypatch.setattr(
        "course_insight.modules.m0_platform.django_app.views.student_qa.build_student_qa_adapter",
        lambda *args: adapter,
    )

    response = client.post(
        reverse(
            "student-qa",
            kwargs={"course_id": "course_1", "class_id": "class_1"},
        ),
        {"question": "拥塞控制有什么作用？"},
    )

    body = response.content.decode()
    assert response.status_code == 200
    assert "涉及知识点" in body and "拥塞控制" in body
    assert "慢启动" in body and "concept-2" in body
    assert "知识点对应原文" in body and "chapter.txt" in body
    assert "paragraph:1" in body and "拥塞控制用于避免网络过载" in body
    assert "分析" in body and "相关例题" in body
    assert "例题 1" in body and "答案 1" in body
    assert "例题 2" in body and "答案 2" in body
    assert "例题 3" not in body
    assert adapter.example_count == 2
    assert "&lt;script&gt;" in body and "<script>alert(1)</script>" not in body


def test_related_choice_example_renders_all_options(
    client: Client,
    monkeypatch,
) -> None:
    student = _student()
    _active_release(student, example_count=1, with_choice_options=True)
    save_scoped_deepseek_settings(
        course_id="course_1",
        class_id="class_1",
        api_key="sk-student-qa-test-key",
        model_name="deepseek-v4-flash",
        thinking_enabled=False,
        updated_by=student,
    )
    adapter = _FakeAdapter()
    client.force_login(student)
    monkeypatch.setattr(
        "course_insight.modules.m0_platform.django_app.views.student_qa.build_student_qa_adapter",
        lambda *args: adapter,
    )

    response = client.post(
        reverse(
            "student-qa",
            kwargs={"course_id": "course_1", "class_id": "class_1"},
        ),
        {"question": "UDP有什么特点？"},
    )

    body = response.content.decode()
    assert response.status_code == 200
    assert "下列哪项符合UDP的主要特点?" in body
    assert "A. 面向连接并保证可靠传输" in body
    assert "B. 无连接并保留应用报文边界" in body
    assert "C. 建立连接后才能发送数据" in body
    assert "D. 只支持字节流传输" in body
    assert "参考答案：B" in body


def test_deleted_source_is_not_retrievable_in_student_qa(
    client: Client, monkeypatch
) -> None:
    student = _student()
    source = _active_release(student)
    source.status = "deleted"
    source.save(update_fields=("status", "updated_at"))
    save_scoped_deepseek_settings(
        course_id="course_1",
        class_id="class_1",
        api_key="sk-student-qa-test-key",
        model_name="deepseek-v4-flash",
        thinking_enabled=False,
        updated_by=student,
    )
    client.force_login(student)
    class _NoCourseAdapter:
        def answer(self, question, concepts, context_loader, *, model_ref, created_at):
            del question, context_loader, model_ref, created_at
            assert concepts == ()
            return StudentQAAnswer(
                status="answered",
                coverage="none",
                warning=(
                    "⚠ 当前回答没有课程知识点和教师资料支撑，"
                    "内容来自外部检索，请核对事实。"
                ),
                analysis="外部检索回答。",
                external_sources=(
                    StudentQAExternalSource(
                        source_id="web-source-1",
                        title="外部资料",
                        url="https://example.edu/reference",
                    ),
                ),
            )

    monkeypatch.setattr(
        "course_insight.modules.m0_platform.django_app.views.student_qa.build_student_qa_adapter",
        lambda *args: _NoCourseAdapter(),
    )

    response = client.post(
        reverse(
            "student-qa",
            kwargs={"course_id": "course_1", "class_id": "class_1"},
        ),
        {"question": "拥塞控制有什么作用？"},
    )

    body = response.content.decode()
    assert "没有课程知识点" in body and "请核对事实" in body
    assert "https://example.edu/reference" in body
    assert "chapter.txt" not in body
