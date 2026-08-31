from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.modules.m5_learner_class_state.service import M5StateService
from course_insight.modules.m9_teacher_analytics.service import (
    M9TeacherAnalyticsService,
)
from tests.integration.test_web_workflow_persistence import (
    _m5_knowledge,
    _m5_policy,
    _m5_scoring_bundle,
    _state_result,
    _teacher_policy,
)


def _m5_arguments(policy_path: Path) -> dict[str, object]:
    return {
        "scoring_result_bundle": _m5_scoring_bundle(
            attempt_id="attempt_frozen_policy",
            course_id="course_1",
            class_id="class_1",
            latest_audit_version=1,
        ),
        "knowledge_bundle": _m5_knowledge("course_1"),
        "previous_learner_state_snapshot": None,
        "previous_class_state_snapshot": None,
        "state_policy_path": policy_path,
    }


def _m9_arguments(policy_path: Path) -> dict[str, object]:
    attempt_id = "attempt_frozen_policy"
    return {
        "knowledge_bundle": _m5_knowledge("course_1"),
        "scoring_result_bundle": _m5_scoring_bundle(
            attempt_id=attempt_id,
            course_id="course_1",
            class_id="class_1",
            latest_audit_version=1,
        ),
        "state_update_result": _state_result(
            attempt_id=attempt_id,
            course_id="course_1",
            class_id="class_1",
            state_version=1,
        ),
        "teacher_threshold_policy_path": policy_path,
    }


def _mutate_after_first_read(
    monkeypatch: pytest.MonkeyPatch,
    policy_path: Path,
) -> list[Path]:
    original_read_bytes = Path.read_bytes
    reads: list[Path] = []

    def read_then_mutate(path: Path) -> bytes:
        content = original_read_bytes(path)
        if path == policy_path:
            reads.append(path)
            path.write_bytes(b"{invalid after verified read")
        return content

    monkeypatch.setattr(Path, "read_bytes", read_then_mutate)
    return reads


def test_m5_frozen_policy_reads_once_and_executes_the_verified_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = _m5_policy(
        tmp_path,
        class_id="class_1",
        name="state-policy",
    )
    expected_checksum = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    reads = _mutate_after_first_read(monkeypatch, policy_path)

    result = M5StateService(
        object(),
        object(),
        object(),
    ).update_state_with_frozen_policy(
        **_m5_arguments(policy_path),
        expected_policy_checksum=expected_checksum,
    )

    assert result.learner_state_snapshot.class_id == "class_1"
    assert reads == [policy_path]


def test_m5_frozen_policy_uses_dynamic_roster_size_instead_of_legacy_json(
    tmp_path: Path,
) -> None:
    policy_path = _m5_policy(
        tmp_path,
        class_id="class_1",
        name="state-policy-dynamic-roster",
    )
    expected_checksum = hashlib.sha256(policy_path.read_bytes()).hexdigest()

    result = M5StateService(
        object(),
        object(),
        object(),
    ).update_state_with_frozen_policy(
        **_m5_arguments(policy_path),
        expected_policy_checksum=expected_checksum,
        class_roster_size=3,
    )

    assert result.class_state_snapshot.class_size == 3
    assert result.class_state_snapshot.assessed_count == 1


def test_m5_frozen_policy_uses_authoritative_dynamic_class_scope(
    tmp_path: Path,
) -> None:
    policy_path = _m5_policy(
        tmp_path,
        class_id="class_legacy_template",
        name="state-policy-dynamic-class",
    )
    expected_checksum = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    arguments = _m5_arguments(policy_path)
    arguments["scoring_result_bundle"] = _m5_scoring_bundle(
        attempt_id="attempt_frozen_policy",
        course_id="course_1",
        class_id="class_runtime_387",
        latest_audit_version=1,
    )

    result = M5StateService(
        object(),
        object(),
        object(),
    ).update_state_with_frozen_policy(
        **arguments,
        expected_policy_checksum=expected_checksum,
        authoritative_class_id="class_runtime_387",
    )

    assert result.learner_state_snapshot.class_id == "class_runtime_387"
    assert result.class_state_snapshot.class_id == "class_runtime_387"


def test_m5_frozen_policy_checksum_error_does_not_disclose_the_path(
    tmp_path: Path,
) -> None:
    private_path = tmp_path / "private" / "tenant-17"
    private_path.mkdir(parents=True)
    policy_path = _m5_policy(
        private_path,
        class_id="class_1",
        name="state-policy",
    )

    with pytest.raises(DomainError) as raised:
        M5StateService(
            object(),
            object(),
            object(),
        ).update_state_with_frozen_policy(
            **_m5_arguments(policy_path),
            expected_policy_checksum="0" * 64,
        )

    assert raised.value.code == "STATE_POLICY_INVALID"
    assert str(policy_path) not in str(raised.value.details)


def test_m5_frozen_policy_read_error_does_not_disclose_the_path(
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "private" / "tenant-17" / "missing-state.json"

    with pytest.raises(DomainError) as raised:
        M5StateService(
            object(),
            object(),
            object(),
        ).update_state_with_frozen_policy(
            **_m5_arguments(policy_path),
            expected_policy_checksum="0" * 64,
        )

    assert raised.value.code == "STATE_POLICY_INVALID"
    assert str(policy_path) not in str(raised.value.details)


def test_m9_frozen_policy_reads_once_and_executes_the_verified_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = _teacher_policy(tmp_path, name="teacher-policy")
    expected_checksum = hashlib.sha256(policy_path.read_bytes()).hexdigest()
    reads = _mutate_after_first_read(monkeypatch, policy_path)

    result = M9TeacherAnalyticsService(
        object(),
        object(),
        object(),
    ).build_teacher_analytics_with_frozen_policy(
        **_m9_arguments(policy_path),
        expected_policy_checksum=expected_checksum,
    )

    assert result.report_id.startswith("report_course_1_")
    assert reads == [policy_path]


def test_m9_frozen_policy_checksum_error_does_not_disclose_the_path(
    tmp_path: Path,
) -> None:
    private_path = tmp_path / "private" / "tenant-17"
    private_path.mkdir(parents=True)
    policy_path = _teacher_policy(
        private_path,
        name="teacher-policy",
    )

    with pytest.raises(DomainError) as raised:
        M9TeacherAnalyticsService(
            object(),
            object(),
            object(),
        ).build_teacher_analytics_with_frozen_policy(
            **_m9_arguments(policy_path),
            expected_policy_checksum="0" * 64,
        )

    assert raised.value.code == "REPORT_SCOPE_INVALID"
    assert str(policy_path) not in str(raised.value.details)


def test_m9_frozen_policy_read_error_does_not_disclose_the_path(
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "private" / "tenant-17" / "missing-teacher.json"

    with pytest.raises(DomainError) as raised:
        M9TeacherAnalyticsService(
            object(),
            object(),
            object(),
        ).build_teacher_analytics_with_frozen_policy(
            **_m9_arguments(policy_path),
            expected_policy_checksum="0" * 64,
        )

    assert raised.value.code == "REPORT_SCOPE_INVALID"
    assert str(policy_path) not in str(raised.value.details)
