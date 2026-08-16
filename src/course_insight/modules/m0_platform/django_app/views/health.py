"""Non-disclosing liveness and readiness endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from course_insight.application.readiness import evaluate_capability_readiness
from course_insight.infrastructure.json_io import read_json
from course_insight.modules.m0_platform.django_app import runtime


_WORKER_STATES = frozenset({"running", "idle"})
_STATUS_KEYS = frozenset(
    {
        "worker_id",
        "state",
        "last_heartbeat_at",
        "last_success_at",
        "last_error_code",
        "claimed_count",
        "delivered_count",
        "dead_count",
    }
)
_MAX_STATUS_BYTES = 64 * 1024


@never_cache
@require_GET
def live(request: HttpRequest) -> HttpResponse:
    del request
    return JsonResponse({"status": "live"})


@never_cache
@require_GET
def ready(request: HttpRequest) -> HttpResponse:
    del request
    components = {
        "config": "unavailable",
        "database": "unavailable",
        "migrations": "unavailable",
        "runtime": "unavailable",
        "logging": "unavailable",
        "outbox": "unavailable",
        "capabilities": "unavailable",
    }
    try:
        web_runtime = runtime.get_web_runtime()
        container = web_runtime.container
        health = container.m0_service.health_check()
        capability_readiness = evaluate_capability_readiness(
            backend=getattr(
                container,
                "persistence_backend",
                getattr(container, "m1_service", None),
            ),
            parser_registry=getattr(
                getattr(container, "m1_service", None),
                "_parser_dispatch",
                None,
            ),
            m2_service=getattr(container, "m2_service", None),
            m3_service=getattr(container, "m3_service", None),
            production=(
                getattr(container.settings, "environment", None)
                == "production"
            ),
        )
        components = {
            "config": health.get("config", "unavailable"),
            "database": health.get("database", "unavailable"),
            "migrations": (
                "ok"
                if health.get("database") == "ok"
                else "unavailable"
            ),
            "runtime": "ok" if web_runtime.courses else "unavailable",
            "logging": (
                "ok"
                if web_runtime.logging_is_ready()
                else "unavailable"
            ),
            "outbox": _outbox_status(
                container.settings.runtime_dir,
                heartbeat_seconds=(
                    container.settings.outbox
                    .heartbeat_interval_seconds
                ),
                poll_seconds=(
                    container.settings.outbox
                    .poll_interval_seconds
                ),
                lease_seconds=(
                    container.settings.outbox.lease_seconds
                ),
            ),
            "capabilities": (
                "ok"
                if capability_readiness["status"] == "ready"
                else "unavailable"
            ),
        }
    except Exception:
        pass
    is_ready = all(value == "ok" for value in components.values())
    payload = {
        "status": "ready" if is_ready else "not_ready",
        **components,
    }
    return JsonResponse(payload, status=200 if is_ready else 503)


def _outbox_status(
    runtime_dir: Path,
    *,
    heartbeat_seconds: float,
    poll_seconds: float,
    lease_seconds: float,
) -> str:
    status_root = (Path(runtime_dir) / "outbox_worker").resolve()
    runtime_root = Path(runtime_dir).resolve()
    if (
        not status_root.is_relative_to(runtime_root)
        or not status_root.is_dir()
    ):
        return "unavailable"
    now = datetime.now(timezone.utc)
    freshness = timedelta(
        seconds=max(
            30.0,
            3.0 * heartbeat_seconds + poll_seconds,
            lease_seconds,
        )
    )
    try:
        candidates = tuple(status_root.glob("*.status.json"))
    except OSError:
        return "unavailable"
    for path in candidates:
        try:
            resolved = path.resolve()
            if (
                not resolved.is_relative_to(status_root)
                or not resolved.is_file()
                or resolved.stat().st_size > _MAX_STATUS_BYTES
            ):
                continue
            value = read_json(resolved)
            if type(value) is not dict or set(value) != _STATUS_KEYS:
                continue
            status = cast(dict[str, object], value)
            heartbeat = _parse_heartbeat(status["last_heartbeat_at"])
            if (
                status["state"] in _WORKER_STATES
                and heartbeat is not None
                and now - freshness <= heartbeat <= now + timedelta(seconds=5)
            ):
                return "ok"
        except Exception:
            continue
    return "unavailable"


def _parse_heartbeat(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)
