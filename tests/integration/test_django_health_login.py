from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import django
import pytest
from django.apps import apps
from django.conf import settings
from django.test import Client, override_settings
from django.urls import reverse


os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "course_insight.web_project.settings",
)
if not apps.ready:
    django.setup()

from course_insight.infrastructure.json_io import write_json  # noqa: E402
from course_insight.modules.m0_platform.django_app import runtime  # noqa: E402
from course_insight.modules.m0_platform.django_app.authz import (  # noqa: E402
    register_login_failure,
)
from course_insight.modules.m0_platform.django_app.models import (  # noqa: E402
    ActorGrant,
    LoginFailureBucket,
    User,
)
from tests.integration._django_web_support import (  # noqa: E402
    FakeCoordinator,
    FakeWebRuntime,
)


pytestmark = pytest.mark.django_db


def test_login_uses_csrf_password_hashing_and_shared_rate_limit() -> None:
    user = User.objects.create_user(
        username="pseudonym_login_001",
        actor_id="pseudonym_login_001",
        password="correct-horse-battery-staple",
    )
    assert user.password != "correct-horse-battery-staple"
    security = settings.PLATFORM_SETTINGS.security.model_copy(
        update={"login_failure_limit": 2}
    )
    platform = settings.PLATFORM_SETTINGS.model_copy(
        update={"security": security}
    )
    client = Client(enforce_csrf_checks=True)

    page = client.get(reverse("login"))
    assert page.status_code == 200
    without_csrf = client.post(
        reverse("login"),
        {"username": user.username, "password": "wrong-password"},
    )
    assert without_csrf.status_code == 403
    csrf = client.cookies["csrftoken"].value
    with override_settings(PLATFORM_SETTINGS=platform):
        first = client.post(
            reverse("login"),
            {
                "username": user.username,
                "password": "wrong-password",
                "csrfmiddlewaretoken": csrf,
            },
            REMOTE_ADDR="192.0.2.20",
        )
        second = client.post(
            reverse("login"),
            {
                "username": user.username,
                "password": "wrong-password",
                "csrfmiddlewaretoken": csrf,
            },
            REMOTE_ADDR="192.0.2.20",
        )
    assert first.status_code == 200
    assert second.status_code == 429
    body = second.content.decode("utf-8")
    assert "登录暂时受限" in body
    assert "wrong-password" not in body


def test_login_rate_limit_cannot_be_bypassed_by_rotating_client_ips() -> None:
    user = User.objects.create_user(
        username="pseudonym_login_distributed",
        actor_id="pseudonym_login_distributed",
        password="correct-horse-battery-staple",
    )
    security = settings.PLATFORM_SETTINGS.security.model_copy(
        update={"login_failure_limit": 2}
    )
    platform = settings.PLATFORM_SETTINGS.model_copy(
        update={"security": security}
    )
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("login"))
    csrf = client.cookies["csrftoken"].value

    with override_settings(PLATFORM_SETTINGS=platform):
        first = client.post(
            reverse("login"),
            {
                "username": user.username,
                "password": "wrong-password",
                "csrfmiddlewaretoken": csrf,
            },
            REMOTE_ADDR="192.0.2.30",
        )
        second = client.post(
            reverse("login"),
            {
                "username": user.username,
                "password": "wrong-password",
                "csrfmiddlewaretoken": csrf,
            },
            REMOTE_ADDR="198.51.100.30",
        )
        blocked_correct_password = client.post(
            reverse("login"),
            {
                "username": user.username,
                "password": "correct-horse-battery-staple",
                "csrfmiddlewaretoken": csrf,
            },
            REMOTE_ADDR="203.0.113.30",
        )

    assert first.status_code == 200
    assert second.status_code == 429
    assert blocked_correct_password.status_code == 429


def test_successful_login_preserves_shared_ip_failure_history() -> None:
    user = User.objects.create_user(
        username="pseudonym_login_success",
        actor_id="pseudonym_login_success",
        password="correct-horse-battery-staple",
    )
    target = User.objects.create_user(
        username="pseudonym_login_target",
        actor_id="pseudonym_login_target",
        password="another-correct-password",
    )
    client_ip = "192.0.2.21"
    security = settings.PLATFORM_SETTINGS.security.model_copy(
        update={"login_failure_limit": 3}
    )
    platform = settings.PLATFORM_SETTINGS.model_copy(
        update={"security": security}
    )
    assert (
        register_login_failure(
            actor_hint=user.username,
            client_ip=client_ip,
            secret=settings.SECRET_KEY,
            limit=3,
            window_seconds=60,
        )
        is True
    )
    assert LoginFailureBucket.objects.count() == 3
    client = Client(enforce_csrf_checks=True)
    page = client.get(reverse("login"))
    csrf = client.cookies["csrftoken"].value

    with override_settings(PLATFORM_SETTINGS=platform):
        response = client.post(
            reverse("login"),
            {
                "username": user.username,
                "password": "correct-horse-battery-staple",
                "csrfmiddlewaretoken": csrf,
            },
            REMOTE_ADDR=client_ip,
        )

    assert response.status_code == 302
    assert LoginFailureBucket.objects.count() == 1

    attacker = Client(enforce_csrf_checks=True)
    attacker.get(reverse("login"))
    attacker_csrf = attacker.cookies["csrftoken"].value
    with override_settings(PLATFORM_SETTINGS=platform):
        first = attacker.post(
            reverse("login"),
            {
                "username": target.username,
                "password": "wrong-password",
                "csrfmiddlewaretoken": attacker_csrf,
            },
            REMOTE_ADDR=client_ip,
        )
        second = attacker.post(
            reverse("login"),
            {
                "username": target.username,
                "password": "wrong-password",
                "csrfmiddlewaretoken": attacker_csrf,
            },
            REMOTE_ADDR=client_ip,
        )

    assert first.status_code == 200
    assert second.status_code == 429


def test_home_pages_require_django_permission_even_with_actor_grant() -> None:
    student = User.objects.create_user(
        username="pseudonym_grant_only_student",
        actor_id="pseudonym_grant_only_student",
    )
    ActorGrant.objects.create(
        user=student,
        role="student",
        course_id="course_1",
        class_id="class_1",
        source_checksum="a" * 64,
    )
    teacher = User.objects.create_user(
        username="pseudonym_grant_only_teacher",
        actor_id="pseudonym_grant_only_teacher",
    )
    ActorGrant.objects.create(
        user=teacher,
        role="teacher",
        course_id="course_1",
        class_id="class_1",
        source_checksum="a" * 64,
    )
    client = Client()

    client.force_login(student)
    assert client.get(reverse("student-home")).status_code == 403
    client.force_login(teacher)
    assert client.get(reverse("teacher-home")).status_code == 403


def test_readiness_requires_fresh_valid_running_worker_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web_runtime = FakeWebRuntime(
        coordinator=FakeCoordinator("pseudonym_student_001"),
        runtime_dir=tmp_path,
    )
    monkeypatch.setattr(runtime, "get_web_runtime", lambda: web_runtime)
    client = Client()

    assert client.get(reverse("health-live")).json() == {"status": "live"}
    missing = client.get(reverse("health-ready"))
    assert missing.status_code == 200
    assert missing.json()["status"] == "degraded"
    assert missing.json()["outbox"] == "unavailable"
    assert missing.json()["ingestion"] == "unavailable"
    assert missing.json()["capabilities"]["web_auth"] == "ready"

    status_path = (
        tmp_path / "outbox_worker" / "worker-safe.status.json"
    )
    write_json(
        status_path,
        _worker_status(datetime.now(timezone.utc)),
    )
    outbox_only = client.get(reverse("health-ready"))
    assert outbox_only.status_code == 200
    assert outbox_only.json()["status"] == "degraded"
    assert outbox_only.json()["outbox"] == "ok"

    ingestion_dir = tmp_path / "ingestion_worker"
    ingestion_dir.mkdir()
    now = datetime.now(timezone.utc)
    (ingestion_dir / "worker.lock").write_text(
        '{"pid":1234,"heartbeat_at":"' + now.isoformat() + '"}',
        encoding="utf-8",
    )
    ready = client.get(reverse("health-ready"))
    assert ready.status_code == 200
    payload = ready.json()
    assert payload["status"] == "ready"
    assert payload["capabilities"] == {
        "web_auth": "ready",
        "course_runtime": "ready",
        "student_read": "ready",
        "learning_outbox": "ready",
        "knowledge_ingestion": "ready",
    }
    serialized = ready.content.decode("utf-8")
    assert str(tmp_path) not in serialized
    assert "DATABASE_URL" not in serialized
    assert "SELECT " not in serialized

    write_json(
        status_path,
        _worker_status(
            datetime.now(timezone.utc) - timedelta(minutes=10)
        ),
    )
    stale = client.get(reverse("health-ready"))
    assert stale.status_code == 200
    assert stale.json()["status"] == "degraded"
    assert stale.json()["outbox"] == "unavailable"
    assert stale.json()["capabilities"]["web_auth"] == "ready"


def test_readiness_fails_closed_when_logging_is_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web_runtime = FakeWebRuntime(
        coordinator=FakeCoordinator("pseudonym_student_001"),
        runtime_dir=tmp_path,
    )
    monkeypatch.setattr(
        web_runtime,
        "logging_is_ready",
        lambda: False,
    )
    monkeypatch.setattr(runtime, "get_web_runtime", lambda: web_runtime)
    write_json(
        tmp_path / "outbox_worker" / "worker-safe.status.json",
        _worker_status(datetime.now(timezone.utc)),
    )

    response = Client().get(reverse("health-ready"))

    assert response.status_code == 503
    assert response.json()["logging"] == "unavailable"


def test_readiness_degrades_when_legacy_production_capabilities_are_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web_runtime = FakeWebRuntime(
        coordinator=FakeCoordinator("pseudonym_student_001"),
        runtime_dir=tmp_path,
    )
    web_runtime.container.settings.environment = "production"
    web_runtime.container.persistence_backend = SimpleNamespace(
        backend_name="sqlite",
        is_ready=lambda: True,
    )
    web_runtime.container.m1_service = SimpleNamespace(_parser_dispatch=None)
    web_runtime.container.m2_service = SimpleNamespace(
        _embedding_provider=None,
        _vector_store=None,
        _audit_store=None,
    )
    web_runtime.container.m3_service = SimpleNamespace(
        _require_teacher_approval=True,
        _review_workflow=None,
    )
    monkeypatch.setattr(runtime, "get_web_runtime", lambda: web_runtime)
    write_json(
        tmp_path / "outbox_worker" / "worker-safe.status.json",
        _worker_status(datetime.now(timezone.utc)),
    )

    response = Client().get(reverse("health-ready"))

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["legacy_capabilities"] == "unavailable"
    assert response.json()["capabilities"]["web_auth"] == "ready"
    assert response.json()["capabilities"]["student_read"] == "not_ready"


def _worker_status(heartbeat: datetime) -> dict[str, object]:
    return {
        "worker_id": "worker-safe",
        "state": "idle",
        "last_heartbeat_at": heartbeat.isoformat(),
        "last_success_at": None,
        "last_error_code": None,
        "claimed_count": 0,
        "delivered_count": 0,
        "dead_count": 0,
    }
