from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.modules.m0_platform.django_app.models import (
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
from course_insight.modules.m3_knowledge_bundle.release_compatibility import (
    knowledge_bundle_from_release,
    summarize_release_quality,
)
from course_insight.modules.m4_task_orchestration.service import (
    M4TaskOrchestrationService,
)
from course_insight.modules.m8_assessment_scoring.paper_generator import PaperGenerator
from course_insight.modules.m8_assessment_scoring.rule_scorer import RuleScorer


pytestmark = pytest.mark.django_db


class _TaskRepository:
    def __init__(self) -> None:
        self.plans = {}

    def insert_or_get_task_plan(self, plan, key):
        self.plans.setdefault(key, plan)
        return self.plans[key]

    def get_task_plan(self, task_id):
        return next((plan for plan in self.plans.values() if plan.task_id == task_id), None)


def _release(version_number: int, *, concept_name: str = "拥塞控制") -> CourseKnowledgeRelease:
    user, _ = User.objects.get_or_create(
        username="pseudonym_downstream_teacher",
        defaults={"actor_id": "pseudonym_downstream_teacher"},
    )
    source = CourseSource.objects.create(
        course_id="course_1",
        display_name=f"chapter-{version_number}.txt",
        source_type="knowledge",
        status="active",
        created_by=user,
    )
    source_version = CourseSourceVersion.objects.create(
        source=source,
        version_number=1,
        storage_key=f"{uuid.uuid4().hex}/{uuid.uuid4()}",
        sha256=f"{version_number:064x}",
        media_type="text/plain",
        size_bytes=20,
        status="active",
    )
    job = KnowledgeIngestionJob.objects.create(
        course_id="course_1",
        requested_by=user,
        change_set_checksum=f"{version_number + 10:064x}",
        status="succeeded",
        progress=100,
    )
    CourseKnowledgeRelease.objects.filter(course_id="course_1", status="active").update(status="retired")
    release = CourseKnowledgeRelease.objects.create(
        course_id="course_1",
        version_number=version_number,
        status="active",
        job=job,
        content_checksum=f"{version_number + 20:064x}",
        activated_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )
    concept = ReleaseConcept.objects.create(
        release=release,
        concept_id=f"concept-{version_number}",
        name=concept_name,
        description=f"{concept_name}课程定义",
        aliases=[],
    )
    source_ref = ReleaseConceptSource.objects.create(
        concept=concept,
        source_version=source_version,
        chunk_id=f"chunk-{version_number}",
        locator="paragraph:1",
        chunk_text=f"{concept_name}用于课程测试。",
        span_start=0,
        span_end=len(concept_name),
        relation_type="definition",
    )
    question = ReleaseQuestion.objects.create(
        release=release,
        question_id=f"question-{version_number}",
        source_version=source_version,
        question_type="fill_blank",
        ordinal=1,
        locator="lines:1-7",
        stem=f"课程中的核心机制是 ____。",
        payload={
            "options": {},
            "accepted_answers": [concept_name],
            "rubric": None,
            "explanation": "对应课程定义。",
        },
    )
    ReleaseQuestionConceptLink.objects.create(
        question=question,
        concept=concept,
        confidence=0.9,
        status="usable",
        evidence=[{"source_ref_id": source_ref.pk}],
    )
    return release


def test_m4_freezes_release_identity_and_m8_keeps_historical_paper_stable() -> None:
    first_release = _release(1)
    first_bundle = knowledge_bundle_from_release(first_release)
    service = M4TaskOrchestrationService(
        _TaskRepository(),
        lambda identity: first_bundle.content_checksum()[:24],
    )
    plan = service.create_task_plan(
        student_text="请生成练习",
        task_type_hint="practice",
        course_id="course_1",
        class_id="class_1",
        learner_id="pseudonym_student_1",
        session_id="session_1",
        knowledge_bundle=first_bundle,
        learner_state_snapshot=None,
    )
    paper = PaperGenerator().generate(plan, first_bundle, None, None)
    frozen_checksum = paper.immutable_checksum

    second_release = _release(2, concept_name="慢启动")
    second_bundle = knowledge_bundle_from_release(second_release)

    assert plan.knowledge_bundle_id == str(first_release.pk)
    assert first_bundle.concepts[0].name == "拥塞控制"
    assert second_bundle.concepts[0].name == "慢启动"
    assert paper.immutable_checksum == frozen_checksum
    with pytest.raises(DomainError, match="not assessment-aligned"):
        PaperGenerator().generate(plan, second_bundle, None, None)


def test_release_answer_contract_scores_objective_submission() -> None:
    release = _release(1)
    bundle = knowledge_bundle_from_release(release)
    service = M4TaskOrchestrationService(
        _TaskRepository(),
        lambda identity: bundle.content_checksum()[:24],
    )
    plan = service.create_task_plan(
        student_text="请生成诊断测评",
        task_type_hint="diagnostic",
        course_id="course_1",
        class_id="class_1",
        learner_id="pseudonym_student_1",
        session_id="session_1",
        knowledge_bundle=bundle,
        learner_state_snapshot=None,
    )
    paper = PaperGenerator().generate(plan, bundle, None, None)
    instance = paper.all_items()[0]

    audit = RuleScorer().score(
        attempt_id="attempt_1",
        item_instance=instance,
        item=bundle.items[0],
        raw_answer="拥塞控制",
    )

    assert audit.total_score == 1.0
    assert audit.review_status == "not_required"


def test_quality_summary_reports_links_without_approval_state() -> None:
    release = _release(1)

    summary = summarize_release_quality(release)

    assert summary == {
        "release_id": str(release.pk),
        "concept_count": 1,
        "source_reference_count": 1,
        "question_count": 1,
        "usable_question_link_count": 1,
        "needs_review_link_count": 0,
        "isolated_concept_count": 0,
    }
    assert "approval" not in summary
