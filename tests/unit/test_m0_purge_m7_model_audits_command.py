from __future__ import annotations

import os
from datetime import datetime
from io import StringIO
from types import SimpleNamespace

import django
import pytest
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError


os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "course_insight.web_project.settings",
)
if not apps.ready:
    django.setup()

from course_insight.modules.m0_platform.django_app import runtime  # noqa: E402
from course_insight.modules.m7_local_model.service import (  # noqa: E402
    M7_AUDIT_RETENTION_DAYS,
)


def test_purge_m7_model_audits_uses_fixed_admin_retention_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class _M7Service:
        def purge_expired_model_audits(self, **kwargs: object) -> int:
            calls.append(kwargs)
            return 3

    monkeypatch.setattr(
        runtime,
        "get_application_container",
        lambda: SimpleNamespace(m7_service=_M7Service()),
    )
    monkeypatch.setattr(
        runtime,
        "close_application_container",
        lambda: calls.append("close"),
    )
    output = StringIO()

    call_command("purge_m7_model_audits", stdout=output)

    request = calls[0]
    assert isinstance(request, dict)
    assert request["requester_role"] == "system_admin"
    assert request["retention_days"] == M7_AUDIT_RETENTION_DAYS == 180
    assert isinstance(request["now"], datetime)
    assert request["now"].utcoffset() is not None
    assert calls[1] == "close"
    assert "deleted=3" in output.getvalue()


def test_purge_m7_model_audits_fails_closed_and_releases_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _M7Service:
        def purge_expired_model_audits(self, **kwargs: object) -> int:
            del kwargs
            raise RuntimeError("private database detail")

    monkeypatch.setattr(
        runtime,
        "get_application_container",
        lambda: SimpleNamespace(m7_service=_M7Service()),
    )
    monkeypatch.setattr(
        runtime,
        "close_application_container",
        lambda: calls.append("close"),
    )

    with pytest.raises(
        CommandError,
        match="M7 audit retention could not complete safely",
    ) as raised:
        call_command("purge_m7_model_audits")

    assert "private database detail" not in str(raised.value)
    assert calls == ["close"]
