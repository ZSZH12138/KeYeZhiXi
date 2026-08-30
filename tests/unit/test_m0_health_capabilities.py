from __future__ import annotations

from datetime import datetime, timezone

import pytest
from django.test import Client
from django.urls import reverse

from course_insight.infrastructure.json_io import write_json
from course_insight.modules.m0_platform.django_app import runtime
from tests.integration._django_web_support import FakeCoordinator, FakeWebRuntime


pytestmark = pytest.mark.django_db


def test_missing_workers_degrade_but_do_not_block_web_auth(tmp_path, monkeypatch) -> None:
    web_runtime = FakeWebRuntime(
        coordinator=FakeCoordinator("pseudonym_student_health"),
        runtime_dir=tmp_path,
    )
    monkeypatch.setattr(runtime, "get_web_runtime", lambda: web_runtime)

    response = Client().get(reverse("health-ready"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["capabilities"]["web_auth"] == "ready"
    assert payload["capabilities"]["learning_outbox"] == "not_ready"
    assert payload["capabilities"]["knowledge_ingestion"] == "not_ready"


def test_fresh_both_workers_make_all_capabilities_ready(tmp_path, monkeypatch) -> None:
    web_runtime = FakeWebRuntime(
        coordinator=FakeCoordinator("pseudonym_student_health_ready"),
        runtime_dir=tmp_path,
    )
    monkeypatch.setattr(runtime, "get_web_runtime", lambda: web_runtime)
    now = datetime.now(timezone.utc).isoformat()
    write_json(
        tmp_path / "outbox_worker" / "worker.status.json",
        {
            "worker_id": "worker-1",
            "state": "running",
            "last_heartbeat_at": now,
            "last_success_at": None,
            "last_error_code": None,
            "claimed_count": 0,
            "delivered_count": 0,
            "dead_count": 0,
        },
    )
    ingestion_dir = tmp_path / "ingestion_worker"
    ingestion_dir.mkdir()
    (ingestion_dir / "worker.lock").write_text(
        '{"pid":1234,"heartbeat_at":"' + now + '"}',
        encoding="utf-8",
    )

    response = Client().get(reverse("health-ready"))

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_fresh_empty_platform_is_login_ready_before_a_course_exists(
    tmp_path,
    monkeypatch,
) -> None:
    web_runtime = FakeWebRuntime(
        coordinator=FakeCoordinator("pseudonym_fresh_desktop_health"),
        runtime_dir=tmp_path,
    )
    web_runtime.courses = {}
    monkeypatch.setattr(runtime, "get_web_runtime", lambda: web_runtime)
    now = datetime.now(timezone.utc).isoformat()
    write_json(
        tmp_path / "outbox_worker" / "worker.status.json",
        {
            "worker_id": "worker-1",
            "state": "running",
            "last_heartbeat_at": now,
            "last_success_at": None,
            "last_error_code": None,
            "claimed_count": 0,
            "delivered_count": 0,
            "dead_count": 0,
        },
    )
    ingestion_dir = tmp_path / "ingestion_worker"
    ingestion_dir.mkdir()
    (ingestion_dir / "worker.lock").write_text(
        '{"pid":1234,"heartbeat_at":"' + now + '"}',
        encoding="utf-8",
    )

    response = Client().get(reverse("health-ready"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["capabilities"]["web_auth"] == "ready"
    assert payload["capabilities"]["course_runtime"] == "not_ready"
    assert payload["capabilities"]["learning_outbox"] == "ready"
    assert payload["capabilities"]["knowledge_ingestion"] == "ready"


def test_core_logging_failure_remains_not_ready(tmp_path, monkeypatch) -> None:
    web_runtime = FakeWebRuntime(
        coordinator=FakeCoordinator("pseudonym_student_health_core"),
        runtime_dir=tmp_path,
    )
    monkeypatch.setattr(web_runtime, "logging_is_ready", lambda: False)
    monkeypatch.setattr(runtime, "get_web_runtime", lambda: web_runtime)

    response = Client().get(reverse("health-ready"))

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["capabilities"]["web_auth"] == "not_ready"
