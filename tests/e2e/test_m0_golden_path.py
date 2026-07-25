from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import django
import pytest
from django.apps import apps


os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "course_insight.web_project.settings",
)
if not apps.ready:
    django.setup()

from django.contrib.auth.models import Group, Permission  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402

from course_insight.application.factory import (  # noqa: E402
    ApplicationContainer,
    build_application,
)
from course_insight.contracts.course import (  # noqa: E402
    ContentChunk,
    CoursePackage,
    SourceDocument,
)
from course_insight.contracts.knowledge import (  # noqa: E402
    AssessmentBlueprint,
    BlueprintSection,
    ItemCard,
    KnowledgeBundle,
    KnowledgeConcept,
    QMatrixEntry,
)
from course_insight.infrastructure.config import (  # noqa: E402
    DatabaseSettings,
    LoggingSettings,
    PlatformSettings,
)
from course_insight.infrastructure.json_io import write_json  # noqa: E402
from course_insight.modules.m0_platform.django_app import (  # noqa: E402
    runtime as django_runtime,
)
from course_insight.modules.m0_platform.django_app.forms.review import (  # noqa: E402
    TeacherReviewForm,
)
from course_insight.modules.m0_platform.django_app.models import (  # noqa: E402
    ActorGrant,
    User,
)
from course_insight.modules.m0_platform.django_app.runtime import (  # noqa: E402
    WebRuntime,
    restore_course_runtime_manifest,
)


pytestmark = pytest.mark.django_db

NOW = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
COURSE_ID = "course_1"
CLASS_ID = "class_1"
STUDENT_ID = "pseudonym_student_golden"
TEACHER_ID = "pseudonym_teacher_golden"
PASSWORD = "correct-horse-battery-staple"
APP_LABEL = "m0_platform_web"
STUDENT_PERMISSIONS = (
    "start_assessment",
    "submit_assessment",
    "view_own_result",
    "view_own_feedback",
)
TEACHER_PERMISSIONS = (
    "view_class_analytics",
    "view_student_report",
    "review_score",
)


def _settings(tmp_path: Path) -> PlatformSettings:
    runtime_dir = tmp_path / "runtime"
    return PlatformSettings(
        environment="test",
        runtime_dir=runtime_dir,
        config_dir=tmp_path / "config",
        database=DatabaseSettings(
            backend="sqlite",
            sqlite_path=runtime_dir / "course_insight.sqlite3",
        ),
        logging=LoggingSettings(directory=runtime_dir / "logs"),
    )


def _course_package() -> CoursePackage:
    governed_text = (
        "A governed rule states that the target proposition is true. "
        "Use this governed explanation when supporting the target concept."
    )
    chunk = ContentChunk(
        chunk_id="chunk_1",
        source_id="source_1",
        text=governed_text,
        locator="section:1",
        concept_hints=["concept_1"],
        sha256=hashlib.sha256(governed_text.encode("utf-8")).hexdigest(),
    )
    candidate = CoursePackage(
        course_package_id="package_1",
        course_id=COURSE_ID,
        package_version="1.0.0",
        source_documents=[
            SourceDocument(
                source_id="source_1",
                file_name="course.md",
                media_type="text/markdown",
                sha256=hashlib.sha256(b"governed course").hexdigest(),
                page_count=None,
                title="Governed course",
                version="1.0.0",
            )
        ],
        content_chunks=[chunk],
        source_authorizations=[],
        imported_at=NOW,
        status="ready",
        checksum="pending",
    )
    return candidate.model_copy(
        update={"checksum": candidate.recalculate_checksum()},
        deep=True,
    )


def _knowledge_bundle() -> KnowledgeBundle:
    blueprint = AssessmentBlueprint(
        blueprint_id="blueprint_1",
        version="1.0.0",
        course_id=COURSE_ID,
        sections=[
            BlueprintSection(
                section_id="section_1",
                name="Governed objective section",
                item_count=1,
                score=1.0,
                item_types=["true_false"],
                concept_weights={"concept_1": 1.0},
                difficulty_range=(1, 1),
                anchor_item_ids=["item_1"],
            )
        ],
        total_score=1.0,
        duration_minutes=30,
        status="teacher_approved",
    )
    return KnowledgeBundle(
        knowledge_bundle_id="bundle_1",
        course_package_id="package_1",
        course_id=COURSE_ID,
        bundle_version="1.0.0",
        concepts=[
            KnowledgeConcept(
                concept_id="concept_1",
                name="Governed concept",
                chapter_id="chapter_1",
                description="A deterministic golden-path concept.",
                aliases=[],
                status="published",
            )
        ],
        prerequisite_relations=[],
        misconception_tags=[],
        items=[
            ItemCard(
                item_id="item_1",
                version="1.0.0",
                stem="The governed proposition is true.",
                item_type="true_false",
                concept_ids=["concept_1"],
                misconception_ids=[],
                difficulty_level=1,
                cognitive_level="remember",
                parameter_rules=[],
                answer_key={"answer": True, "max_score": 1.0},
                rubric_id=None,
                source_evidence_ids=["evidence_chunk_1"],
                status="teacher_approved",
            )
        ],
        rubrics=[],
        blueprints=[blueprint],
        q_matrix=[
            QMatrixEntry(
                item_id="item_1",
                item_version="1.0.0",
                concept_id="concept_1",
                weight=1.0,
            )
        ],
        status="published",
        published_at=NOW,
    )


def _build_web_runtime(
    tmp_path: Path,
) -> tuple[ApplicationContainer, WebRuntime]:
    settings = _settings(tmp_path)
    container = build_application(settings)
    container.m0_service.initialize()

    package = _course_package()
    bundle = _knowledge_bundle()
    expected_index = container.m2_service.build_index(package)
    snapshot_dir = settings.runtime_dir / "snapshots"
    container.m0_service.save_contract_snapshot(
        package,
        snapshot_dir / "course-package.json",
    )
    container.m0_service.save_contract_snapshot(
        expected_index,
        snapshot_dir / "evidence-index.json",
    )
    container.m0_service.save_contract_snapshot(
        bundle,
        snapshot_dir / "knowledge-bundle.json",
    )

    state_policy_path = settings.runtime_dir / "policies/state.json"
    threshold_policy_path = settings.runtime_dir / "policies/teacher.json"
    write_json(
        state_policy_path,
        {
            "aggregation_policy_version": "1.0.0",
            "class_id": CLASS_ID,
            "class_size": 1,
            "consolidating_threshold": 0.5,
            "mastered_threshold": 0.8,
            "minimum_assessed_count": 1,
            "minimum_coverage": 1.0,
            "misconception_activation_threshold": 0.5,
        },
    )
    write_json(
        threshold_policy_path,
        {
            "minimum_coverage": 1.0,
            "minimum_assessed_count": 1,
            "minimum_confidence": 0.5,
            "weak_mastery_threshold": 0.8,
            "misconception_threshold": 0.5,
            "priority_support_threshold": 0.5,
        },
    )
    write_json(
        settings.runtime_dir / "snapshots/course_runtime_manifest.json",
        {
            "schema_version": 1,
            "courses": [
                {
                    "course_id": COURSE_ID,
                    "course_package_ref": "snapshots/course-package.json",
                    "evidence_index_ref": "snapshots/evidence-index.json",
                    "knowledge_bundle_ref": "snapshots/knowledge-bundle.json",
                    "state_policy_ref": "policies/state.json",
                    "teacher_threshold_policy_ref": (
                        "policies/teacher.json"
                    ),
                }
            ],
        },
    )
    return container, WebRuntime(
        container=container,
        courses=restore_course_runtime_manifest(
            container=container,
            runtime_dir=settings.runtime_dir,
        ),
    )


def _grant_user(
    *,
    actor_id: str,
    role: str,
    codenames: tuple[str, ...],
) -> User:
    user = User.objects.create_user(
        username=actor_id,
        actor_id=actor_id,
        password=PASSWORD,
    )
    permissions = tuple(
        Permission.objects.filter(
            content_type__app_label=APP_LABEL,
            codename__in=codenames,
        )
    )
    assert {permission.codename for permission in permissions} == set(
        codenames
    )
    group, _ = Group.objects.get_or_create(name=role)
    group.permissions.set(permissions)
    user.groups.add(group)
    ActorGrant.objects.create(
        user=user,
        role=role,
        course_id=COURSE_ID,
        class_id=CLASS_ID,
        source_checksum=hashlib.sha256(
            f"{actor_id}:{role}".encode("utf-8")
        ).hexdigest(),
    )
    return user


def _csrf(client: Client) -> str:
    return client.cookies["csrftoken"].value


def _login(client: Client, user: User) -> None:
    page = client.get(reverse("login"))
    assert page.status_code == 200
    response = client.post(
        reverse("login"),
        {
            "username": user.actor_id,
            "password": PASSWORD,
            "csrfmiddlewaretoken": _csrf(client),
        },
    )
    assert response.status_code == 302


def _m0_side_effect_counts(database_path: Path) -> tuple[int, int]:
    with sqlite3.connect(database_path) as connection:
        event_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM m0_learning_events"
            ).fetchone()[0]
        )
        outbox_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM m0_event_outbox"
            ).fetchone()[0]
        )
    return event_count, outbox_count


def _jsonl_events(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def test_sqlite_web_golden_path_delivers_student_and_teacher_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, web_runtime = _build_web_runtime(tmp_path)
    monkeypatch.setattr(
        django_runtime,
        "get_web_runtime",
        lambda: web_runtime,
    )
    student = _grant_user(
        actor_id=STUDENT_ID,
        role="student",
        codenames=STUDENT_PERMISSIONS,
    )
    teacher = _grant_user(
        actor_id=TEACHER_ID,
        role="teacher",
        codenames=TEACHER_PERMISSIONS,
    )
    student_client = Client(enforce_csrf_checks=True)
    teacher_client = Client(enforce_csrf_checks=True)

    try:
        _login(student_client, student)
        start_url = reverse(
            "student-start",
            kwargs={"course_id": COURSE_ID, "class_id": CLASS_ID},
        )
        start_page = student_client.get(start_url)
        assert start_page.status_code == 200
        start_flow = start_page.context["form"].initial["flow_token"]
        started = student_client.post(
            start_url,
            {
                "student_text": "Start a governed practice assessment.",
                "task_type_hint": "practice",
                "flow_token": start_flow,
                "csrfmiddlewaretoken": _csrf(student_client),
            },
        )
        assert started.status_code == 302

        paper_page = student_client.get(started["Location"])
        assert paper_page.status_code == 200
        paper_id = paper_page.context["paper"].paper_id
        answer_fields = tuple(paper_page.context["form"].fields)
        assert len(answer_fields) == 1
        submitted = student_client.post(
            paper_page.context["submit_url"],
            {
                answer_fields[0]: "true",
                "flow_token": paper_page.context["flow"],
                "csrfmiddlewaretoken": _csrf(student_client),
            },
        )
        assert submitted.status_code == 302

        database_path = container.settings.database.sqlite_path
        audit_path = (
            container.settings.runtime_dir
            / "audit"
            / "learning_events.jsonl"
        )
        assert _m0_side_effect_counts(database_path) == (1, 1)
        assert not audit_path.exists()
        assert container.outbox_worker is not None
        first_delivery = container.outbox_worker.run(once=True)
        assert first_delivery.delivered_count == 1
        assert _m0_side_effect_counts(database_path) == (1, 0)
        delivered = _jsonl_events(audit_path)
        assert [event["event_type"] for event in delivered] == [
            "assessment_scored"
        ]
        assert delivered[0]["course_id"] == COURSE_ID
        assert delivered[0]["class_id"] == CLASS_ID
        assert delivered[0]["payload"]["total_score"] == 1.0

        result_page = student_client.get(submitted["Location"])
        assert result_page.status_code == 200
        result = result_page.context["result"]
        assert result.total_score == 1.0
        assert result.max_score == 1.0
        assert len(result.audits) == 1
        rule_audit = result.audits[0]
        assert rule_audit.confidence == 1.0
        assert len(rule_audit.criteria) == 1
        assert rule_audit.criteria[0].criterion_id == "objective_item_1"
        assert rule_audit.criteria[0].reason == (
            "The normalized answer matches the approved answer key."
        )
        feedback_page = student_client.get(
            result_page.context["feedback_url"]
        )
        assert feedback_page.status_code == 200
        feedback = feedback_page.context["feedback"]
        assert feedback.citations
        assert "governed rule" in feedback.citations[0].quote

        _login(teacher_client, teacher)
        context_url = reverse(
            "teacher-review-context",
            kwargs={
                "course_id": COURSE_ID,
                "class_id": CLASS_ID,
                "paper_id": paper_id,
            },
        )
        initial_context = teacher_client.get(context_url)
        assert initial_context.status_code == 200
        initial_analytics = initial_context.context["analytics"]
        assert dict(initial_analytics.score_statistics)["score_mean"] == 1.0
        initial_audit, review_url = initial_context.context["review_links"][0]
        assert initial_audit.audit_version == 1
        assert initial_audit.total_score == 1.0

        review_page = teacher_client.get(review_url)
        assert review_page.status_code == 200
        review = review_page.context["review"]
        criterion_id = review.audit.criteria[0].criterion_id
        reviewed = teacher_client.post(
            review_url,
            {
                "decision": "override",
                "final_total_score": "0.25",
                "teacher_comment": (
                    "Override after reviewing governed course evidence."
                ),
                TeacherReviewForm.score_field_name(criterion_id): "0.25",
                TeacherReviewForm.reason_field_name(criterion_id): (
                    "The teacher applied the governed review policy."
                ),
                "flow_token": review_page.context["flow"],
                "csrfmiddlewaretoken": _csrf(teacher_client),
            },
        )
        assert reviewed.status_code == 302
        assert reviewed["Location"] == context_url

        assert _m0_side_effect_counts(database_path) == (2, 1)
        container.outbox_worker.run(once=True)
        assert _m0_side_effect_counts(database_path) == (2, 0)
        delivered = _jsonl_events(audit_path)
        assert [event["event_type"] for event in delivered] == [
            "assessment_scored",
            "teacher_review_applied",
        ]
        assert delivered[1]["payload"]["audit_version"] == 2
        assert delivered[1]["payload"]["total_score"] == 0.25

        refreshed_context = teacher_client.get(reviewed["Location"])
        assert refreshed_context.status_code == 200
        refreshed_analytics = refreshed_context.context["analytics"]
        assert (
            dict(refreshed_analytics.score_statistics)["score_mean"] == 0.25
        )
        assert refreshed_analytics.individual_reports[0].recent_score == 0.25
        refreshed_audit, _ = refreshed_context.context["review_links"][0]
        assert refreshed_audit.audit_version == 2
        assert refreshed_audit.total_score == 0.25
    finally:
        container.close()
