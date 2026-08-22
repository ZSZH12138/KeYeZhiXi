from __future__ import annotations

import os
from io import StringIO
from pathlib import Path
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

from course_insight.application.legacy_score_inventory import (  # noqa: E402
    inspect_legacy_score_posting,
)
from course_insight.modules.m0_platform.django_app import runtime  # noqa: E402
from tests.unit.test_legacy_score_inventory import (  # noqa: E402
    _pending_scoring,
    _submit_run,
)


def test_inventory_command_writes_polluted_flags_and_never_marks_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scoring = _pending_scoring()
    polluted = _submit_run()
    parked = _submit_run(
        operation_id="submit:waiting-1",
        status="awaiting_review",
        checkpoint="scoring_saved",
        feedback_id=None,
        report_id=None,
        state_version=None,
        previous_state_frozen=True,
    )
    calls: list[str] = []

    class _M0:
        def list_assessment_runs(self):
            return (polluted, parked)

    class _M8:
        def get_scoring_result(self, attempt_id: str):
            del attempt_id
            return scoring

    monkeypatch.setattr(
        runtime,
        "get_application_container",
        lambda: SimpleNamespace(
            m0_service=_M0(),
            m8_service=_M8(),
            settings=SimpleNamespace(runtime_dir=tmp_path),
        ),
    )
    monkeypatch.setattr(
        runtime,
        "close_application_container",
        lambda: calls.append("close"),
    )
    output = StringIO()

    call_command("inventory_legacy_score_posting", stdout=output)

    report = (tmp_path / "legacy_score_inventory.json").read_text(encoding="utf-8")
    assert '"count": 1' in report
    assert "legacy_score_posted_before_review" in report
    assert '"clean": false' in report
    assert "submit:waiting-1" not in report
    assert "legacy polluted submits=1; none marked clean" in output.getvalue()
    assert inspect_legacy_score_posting(parked, scoring) is None
    assert calls == ["close"]


def test_inventory_command_refuses_automatic_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime,
        "get_application_container",
        lambda: SimpleNamespace(),
    )
    monkeypatch.setattr(runtime, "close_application_container", lambda: None)

    with pytest.raises(CommandError, match="automatic M5 rewind is disabled"):
        call_command("inventory_legacy_score_posting", apply=True)
