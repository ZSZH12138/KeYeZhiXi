from __future__ import annotations

import json
from pathlib import Path

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.modules.m1_course_governance.service import (
    M1CourseGovernanceService,
)
from course_insight.modules.m3_knowledge_bundle.service import (
    M3KnowledgeBundleService,
)
from course_insight.modules.m5_learner_class_state.update_policy import (
    StatePolicy,
)
from course_insight.modules.m9_teacher_analytics.suggestions import (
    TeacherThresholdPolicy,
)


def _serialized(error: DomainError) -> str:
    return json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True)


@pytest.mark.parametrize("missing", [True, False])
def test_state_policy_errors_do_not_expose_host_paths(
    tmp_path: Path,
    missing: bool,
) -> None:
    policy_path = tmp_path / "private" / "state-policy.json"
    if not missing:
        policy_path.parent.mkdir()
        policy_path.write_text("{}", encoding="utf-8")

    with pytest.raises(DomainError) as raised:
        StatePolicy.from_path(policy_path)

    assert raised.value.code == "STATE_POLICY_INVALID"
    assert str(tmp_path) not in _serialized(raised.value)
    assert "path" not in raised.value.details


def test_teacher_policy_read_error_does_not_expose_host_path(
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "private" / "teacher-policy.json"

    with pytest.raises(DomainError) as raised:
        TeacherThresholdPolicy.from_path(policy_path)

    assert raised.value.code == "REPORT_SCOPE_INVALID"
    assert str(tmp_path) not in _serialized(raised.value)
    assert "path" not in raised.value.details


def test_course_authorization_error_does_not_expose_host_path(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "private" / "authorizations.csv"
    service = M1CourseGovernanceService(
        parser_registry=None,
        hash_tool=None,
        repository=None,  # type: ignore[arg-type]
    )

    with pytest.raises(DomainError) as raised:
        service._load_authorizations(manifest_path)

    assert raised.value.code == "UNAUTHORIZED_SOURCE"
    assert str(tmp_path) not in _serialized(raised.value)
    assert "path" not in raised.value.details


def test_missing_course_source_error_does_not_expose_host_path(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "private" / "course.md"
    service = M1CourseGovernanceService(
        parser_registry=None,
        hash_tool=None,
        repository=None,  # type: ignore[arg-type]
    )

    with pytest.raises(DomainError) as raised:
        service._import_source(source_path, {}, {}, 0)

    assert raised.value.code == "COURSE_PARSE_FAILED"
    assert str(tmp_path) not in _serialized(raised.value)
    assert "path" not in raised.value.details


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (None, "KNOWLEDGE_SEED_READ_FAILED"),
        ("{", "KNOWLEDGE_SEED_INVALID"),
        ("[]", "KNOWLEDGE_SEED_INVALID"),
    ],
)
def test_knowledge_seed_errors_are_categorized_without_host_paths(
    tmp_path: Path,
    payload: str | None,
    expected_code: str,
) -> None:
    seed_path = tmp_path / "private" / "concepts.json"
    if payload is not None:
        seed_path.parent.mkdir()
        seed_path.write_text(payload, encoding="utf-8")

    with pytest.raises(DomainError) as raised:
        M3KnowledgeBundleService._load_seed(seed_path)

    assert raised.value.code == expected_code
    assert str(tmp_path) not in _serialized(raised.value)
    assert raised.value.details == {}
    assert raised.value.__cause__ is None
