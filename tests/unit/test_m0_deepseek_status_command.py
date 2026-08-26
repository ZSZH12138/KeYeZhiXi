from __future__ import annotations

import json
from io import StringIO

from django.core.management import call_command
from django.test import override_settings


def test_deepseek_status_does_not_initialize_the_application_runtime(
    tmp_path,
    monkeypatch,
) -> None:
    from course_insight.infrastructure.deepseek import DEEPSEEK_API_KEY_ENV
    from course_insight.modules.m0_platform.django_app import runtime

    def _logging_conflict(*args, **kwargs):
        raise AssertionError("status inspection must not acquire the app log")

    monkeypatch.setattr(runtime, "get_web_runtime", _logging_conflict)
    monkeypatch.setattr(runtime, "get_application_container", _logging_conflict)
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "sk-private-command-test-key")
    output = StringIO()

    with override_settings(COURSE_INSIGHT_RUNTIME_DIR=tmp_path):
        call_command("deepseek_status", stdout=output)

    rendered = output.getvalue()
    assert "sk-private-command-test-key" not in rendered
    assert json.loads(rendered) == {
        "configured": True,
        "key_source": "env",
        "model_name": "deepseek-v4-flash",
        "privacy_gate_ready": False,
        "privacy_gate_reason": "missing_pinned_checksums",
        "scoring_ready": False,
        "thinking_enabled": False,
    }
