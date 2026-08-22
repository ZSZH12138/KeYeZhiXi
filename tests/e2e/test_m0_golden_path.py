from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sqlite3
import sys
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
from course_insight.contracts.evidence import (  # noqa: E402
    evidence_id_for_chunk,
)
from course_insight.contracts.tutoring import (  # noqa: E402
    STUDENT_CITATION_QUOTE_PLACEHOLDER,
)
from course_insight.contracts.course import (  # noqa: E402
    ContentChunk,
    CoursePackage,
    SourceAuthorization,
    SourceDocument,
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
from course_insight.modules.m1_course_governance.snapshots import (  # noqa: E402
    CourseImportSnapshot,
    SourcePayload,
)


pytestmark = pytest.mark.django_db

NOW = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
COURSE_ID = "course_1"
CLASS_ID = "class_1"
STUDENT_ID = "pseudonym_student_golden"
TEACHER_ID = "pseudonym_teacher_golden"
PASSWORD = "correct-horse-battery-staple"
APP_LABEL = "m0_platform_web"
GOVERNED_TEXT = (
    "Retrieve governed course rules, explanations, distinctions, and examples "
    "for the selected concepts. A governed rule states that the target "
    "proposition is true. Use this governed explanation when supporting the "
    "target concept."
)
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


def _course_package(source_bytes: bytes) -> CoursePackage:
    governed_text = source_bytes.decode("utf-8")
    text_sha256 = hashlib.sha256(source_bytes).hexdigest()
    chunk_id = "chunk_" + hashlib.sha256(
        f"source_1\0section:1\0{text_sha256}".encode("utf-8")
    ).hexdigest()
    chunk = ContentChunk(
        chunk_id=chunk_id,
        source_id="source_1",
        text=governed_text,
        locator="section:1",
        concept_hints=["concept_1"],
        sha256=text_sha256,
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
                sha256=text_sha256,
                page_count=None,
                title="Governed course",
                version="1.0.0",
            )
        ],
        content_chunks=[chunk],
        source_authorizations=[
            SourceAuthorization(
                source_id="source_1",
                authorized_by="Teacher",
                license_note="course use",
                authorized_at=NOW,
            )
        ],
        imported_at=NOW,
        status="ready",
        checksum="pending",
    )
    return candidate.model_copy(
        update={"checksum": candidate.recalculate_checksum()},
        deep=True,
    )


def _course_import_snapshot(
    package: CoursePackage,
    source_bytes: bytes,
) -> CourseImportSnapshot:
    metadata_bytes = json.dumps(
        {
            "course_package_id": package.course_package_id,
            "course_id": package.course_id,
            "package_version": package.package_version,
            "course_name": package.source_documents[0].title,
            "imported_at": package.imported_at.isoformat(),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    authorization_buffer = io.StringIO(newline="")
    authorization_writer = csv.DictWriter(
        authorization_buffer,
        fieldnames=(
            "file_name",
            "source_id",
            "expected_sha256",
            "authorized_by",
            "authorized_at",
            "license_note",
        ),
        lineterminator="\n",
    )
    authorization_writer.writeheader()
    documents_by_source_id = {
        document.source_id: document for document in package.source_documents
    }
    for authorization in package.source_authorizations:
        document = documents_by_source_id[authorization.source_id]
        authorization_writer.writerow(
            {
                "file_name": document.file_name,
                "source_id": authorization.source_id,
                "expected_sha256": document.sha256,
                "authorized_by": authorization.authorized_by,
                "authorized_at": authorization.authorized_at.isoformat(),
                "license_note": authorization.license_note,
            }
        )
    authorization_bytes = authorization_buffer.getvalue().encode("utf-8")
    source_documents = tuple(package.source_documents)
    if len(source_documents) != 1:
        raise AssertionError("golden fixture expects one source document")
    source = source_documents[0]
    return CourseImportSnapshot(
        course_metadata_bytes=metadata_bytes,
        source_authorization_bytes=authorization_bytes,
        source_payloads=(
            SourcePayload(
                source_id=source.source_id,
                file_name=source.file_name,
                raw_bytes=source_bytes,
            ),
        ),
    )


def test_course_import_snapshot_derives_authorization_rows_from_package() -> None:
    source_bytes = GOVERNED_TEXT.encode("utf-8")
    package = _course_package(source_bytes)
    document = package.source_documents[0].model_copy(
        update={
            "source_id": "derived_source",
            "file_name": "derived-course.txt",
        }
    )
    authorization = package.source_authorizations[0].model_copy(
        update={
            "source_id": "derived_source",
            "authorized_by": "Derived teacher",
            "authorized_at": NOW.replace(hour=10),
            "license_note": "derived license",
        }
    )
    derived_package = package.model_copy(
        update={
            "source_documents": [document],
            "source_authorizations": [authorization],
        }
    )

    snapshot = _course_import_snapshot(derived_package, source_bytes)
    rows = list(
        csv.DictReader(
            io.StringIO(snapshot.source_authorization_bytes.decode("utf-8"))
        )
    )

    assert rows == [
        {
            "file_name": "derived-course.txt",
            "source_id": "derived_source",
            "expected_sha256": document.sha256,
            "authorized_by": "Derived teacher",
            "authorized_at": NOW.replace(hour=10).isoformat(),
            "license_note": "derived license",
        }
    ]


def test_knowledge_seed_paths_use_canonical_evidence_id_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _course_package(GOVERNED_TEXT.encode("utf-8"))
    calls: list[str] = []

    def fake_evidence_id_for_chunk(chunk_id: str) -> str:
        calls.append(chunk_id)
        return "canonical-evidence-id"

    monkeypatch.setattr(
        sys.modules[__name__],
        "evidence_id_for_chunk",
        fake_evidence_id_for_chunk,
    )

    paths = _knowledge_seed_paths(tmp_path, package)
    concept_payload = json.loads(
        paths["concept"].read_text(encoding="utf-8")
    )
    item_payload = json.loads(paths["item"].read_text(encoding="utf-8"))

    assert calls == [package.content_chunks[0].chunk_id]
    assert concept_payload["concept_evidence_ids"] == {
        "concept_1": ["canonical-evidence-id"]
    }
    assert item_payload["items"][0]["source_evidence_ids"] == [
        "canonical-evidence-id"
    ]


def test_build_web_runtime_closes_container_when_initialization_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingM0Service:
        def initialize(self) -> None:
            raise RuntimeError("initialization failed")

    class FakeContainer:
        def __init__(self) -> None:
            self.m0_service = FailingM0Service()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    fake_container = FakeContainer()
    monkeypatch.setattr(
        sys.modules[__name__],
        "build_application",
        lambda settings: fake_container,
    )

    with pytest.raises(RuntimeError, match="initialization failed"):
        _build_web_runtime(tmp_path)

    assert fake_container.closed is True


def _knowledge_seed_paths(
    seed_dir: Path,
    package: CoursePackage,
) -> dict[str, Path]:
    seed_dir.mkdir(parents=True, exist_ok=True)
    evidence_id = evidence_id_for_chunk(package.content_chunks[0].chunk_id)
    seed_payloads: dict[str, dict[str, object]] = {
        "concept": {
            "knowledge_bundle_id": "bundle_1",
            "bundle_version": "1.0.0",
            "published_at": NOW.isoformat(),
            "course_id": package.course_id,
            "course_package_id": package.course_package_id,
            "course_package_checksum": package.checksum,
            "concepts": [
                {
                    "concept_id": "concept_1",
                    "name": "Governed concept",
                    "chapter_id": "chapter_1",
                    "description": "A deterministic golden-path concept.",
                    "aliases": [],
                    "status": "published",
                }
            ],
            "concept_evidence_ids": {"concept_1": [evidence_id]},
        },
        "item": {
            "items": [
                {
                    "item_id": "item_1",
                    "version": "1.0.0",
                    "stem": "The governed proposition is true.",
                    "item_type": "true_false",
                    "concept_ids": ["concept_1"],
                    "misconception_ids": [],
                    "difficulty_level": 1,
                    "cognitive_level": "remember",
                    "parameter_rules": [],
                    "answer_key": {"answer": True, "max_score": 1.0},
                    "rubric_id": None,
                    "source_evidence_ids": [evidence_id],
                    "status": "teacher_approved",
                }
            ],
            "q_matrix": [
                {
                    "item_id": "item_1",
                    "item_version": "1.0.0",
                    "concept_id": "concept_1",
                    "weight": 1.0,
                }
            ],
        },
        "rubric": {"rubrics": []},
        "blueprint": {
            "blueprints": [
                {
                    "blueprint_id": "blueprint_1",
                    "version": "1.0.0",
                    "course_id": package.course_id,
                    "sections": [
                        {
                            "section_id": "section_1",
                            "name": "Governed objective section",
                            "item_count": 1,
                            "score": 1.0,
                            "item_types": ["true_false"],
                            "concept_weights": {"concept_1": 1.0},
                            "difficulty_range": [1, 1],
                            "anchor_item_ids": ["item_1"],
                            "anchor_item_versions": {"item_1": "1.0.0"},
                        }
                    ],
                    "total_score": 1.0,
                    "duration_minutes": 30,
                    "status": "teacher_approved",
                }
            ]
        },
        "prerequisite": {"prerequisite_relations": []},
        "misconception": {"misconception_tags": []},
    }
    paths: dict[str, Path] = {}
    for role, payload in seed_payloads.items():
        path = seed_dir / f"{role}.json"
        path.write_text(
            json.dumps(payload, separators=(",", ":")),
            encoding="utf-8",
        )
        paths[role] = path
    return paths


def _build_web_runtime(
    tmp_path: Path,
) -> tuple[ApplicationContainer, WebRuntime]:
    settings = _settings(tmp_path)
    container = build_application(settings)
    try:
        container.m0_service.initialize()

        source_bytes = GOVERNED_TEXT.encode("utf-8")
        package = _course_package(source_bytes)
        container.m1_service._repository.save_course_import(  # noqa: SLF001
            package,
            _course_import_snapshot(package, source_bytes),
        )
        expected_index = container.m2_service.build_index(package)
        seed_paths = _knowledge_seed_paths(tmp_path / "teacher-seeds", package)
        bundle = container.m3_service.build_knowledge_bundle(
            package,
            seed_paths["concept"],
            seed_paths["item"],
            seed_paths["rubric"],
            seed_paths["blueprint"],
            seed_paths["prerequisite"],
            seed_paths["misconception"],
        )
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
    except BaseException:
        try:
            container.close()
        except BaseException:
            pass
        raise


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


def _frozen_m0_and_m6_policy_identity(
    database_path: Path,
) -> tuple[tuple[object, ...], dict[str, object]]:
    with sqlite3.connect(database_path) as connection:
        m0_row = connection.execute(
            """
            SELECT policy_id,
                   adapter_id,
                   adapter_version,
                   artifact_sha256,
                   feature_schema_version,
                   action_space_version,
                   gate_policy_version
            FROM m0_assessment_runs
            WHERE operation = 'submit'
            """
        ).fetchone()
        m6_row = connection.execute(
            "SELECT payload FROM m6_policy_executions"
        ).fetchone()
    assert m0_row is not None
    assert m6_row is not None
    return tuple(m0_row), json.loads(str(m6_row[0]))


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
        frozen_m0, private_m6 = _frozen_m0_and_m6_policy_identity(
            database_path
        )
        assert frozen_m0 == (
            private_m6["policy_id"],
            private_m6["adapter_id"],
            private_m6["adapter_version"],
            private_m6["artifact_sha256"],
            private_m6["feature_schema_version"],
            private_m6["action_space_version"],
            private_m6["gate_policy_version"],
        )
        assert set(private_m6) == {
            "request_fingerprint",
            "input_fingerprint",
            "mode",
            "policy_id",
            "adapter_id",
            "adapter_version",
            "artifact_sha256",
            "feature_schema_version",
            "action_space_version",
            "gate_policy_version",
        }
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
        assert feedback.citations[0].quote == STUDENT_CITATION_QUOTE_PLACEHOLDER
        assert "source_1" in feedback.citations[0].label

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
