from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from course_insight.application.assessment_dependencies import (
    capture_assessment_dependencies,
    verify_policy_dependencies,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceIndexRef
from course_insight.contracts.knowledge import KnowledgeBundle


NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _bundle(*, course_package_id: str = "package_1") -> KnowledgeBundle:
    return KnowledgeBundle(
        knowledge_bundle_id="bundle_1",
        course_package_id=course_package_id,
        course_id="course_1",
        bundle_version="1.0.0",
        concepts=[],
        prerequisite_relations=[],
        misconception_tags=[],
        items=[],
        rubrics=[],
        blueprints=[],
        q_matrix=[],
        status="published",
        published_at=NOW,
    )


def _index(*, course_package_id: str = "package_1") -> EvidenceIndexRef:
    return EvidenceIndexRef(
        index_id="index_1",
        course_package_id=course_package_id,
        index_version="1.0.0",
        storage_ref="lexical:index_1",
        backend="lexical",
        source_count=0,
        chunk_count=0,
        built_at=NOW,
        checksum="source-checksum",
        status="ready",
    )


def _policies(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "state.json"
    teacher = tmp_path / "teacher.json"
    state.write_text(
        json.dumps(
            {
                "aggregation_policy_version": "1.0.0",
                "class_id": "class_1",
                "class_size": 1,
                "consolidating_threshold": 0.5,
                "mastered_threshold": 0.8,
                "minimum_assessed_count": 1,
                "minimum_coverage": 1.0,
                "misconception_activation_threshold": 0.5,
            }
        ),
        encoding="utf-8",
    )
    teacher.write_text(
        json.dumps(
            {
                "minimum_coverage": 1.0,
                "minimum_assessed_count": 1,
                "minimum_confidence": 0.5,
                "weak_mastery_threshold": 0.8,
                "misconception_threshold": 0.5,
                "priority_support_threshold": 0.5,
            }
        ),
        encoding="utf-8",
    )
    return state, teacher


def test_capture_freezes_every_executable_dependency(tmp_path: Path) -> None:
    state, teacher = _policies(tmp_path)
    bundle = _bundle()
    index = _index()

    captured = capture_assessment_dependencies(
        knowledge_bundle=bundle,
        evidence_index_ref=index,
        state_policy_path=state,
        teacher_policy_path=teacher,
    )

    assert captured.knowledge_bundle_id == "bundle_1"
    assert captured.knowledge_bundle_version == "1.0.0"
    assert captured.knowledge_bundle_checksum == bundle.content_checksum()
    assert captured.course_package_id == "package_1"
    assert captured.evidence_index_id == "index_1"
    assert captured.evidence_index_version == "1.0.0"
    assert captured.evidence_index_checksum == index.content_checksum()
    assert len(captured.state_policy_checksum or "") == 64
    assert len(captured.teacher_policy_checksum or "") == 64


def test_capture_rejects_mismatched_bundle_and_index(tmp_path: Path) -> None:
    state, teacher = _policies(tmp_path)

    with pytest.raises(DomainError) as raised:
        capture_assessment_dependencies(
            knowledge_bundle=_bundle(),
            evidence_index_ref=_index(course_package_id="package_2"),
            state_policy_path=state,
            teacher_policy_path=teacher,
        )

    assert raised.value.code == "WORKFLOW_DEPENDENCY_MISMATCH"


def test_policy_verification_fails_closed_after_file_changes(
    tmp_path: Path,
) -> None:
    state, teacher = _policies(tmp_path)
    captured = capture_assessment_dependencies(
        knowledge_bundle=_bundle(),
        evidence_index_ref=_index(),
        state_policy_path=state,
        teacher_policy_path=teacher,
    )
    state.write_text(state.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(DomainError) as raised:
        verify_policy_dependencies(
            captured,
            state_policy_path=state,
            teacher_policy_path=teacher,
        )

    assert raised.value.code == "WORKFLOW_DEPENDENCY_MISMATCH"


def test_capture_validates_policy_documents_before_freezing(
    tmp_path: Path,
) -> None:
    state, teacher = _policies(tmp_path)
    state.write_text("{}", encoding="utf-8")

    with pytest.raises(DomainError) as raised:
        capture_assessment_dependencies(
            knowledge_bundle=_bundle(),
            evidence_index_ref=None,
            state_policy_path=state,
            teacher_policy_path=teacher,
        )

    assert raised.value.code == "STATE_POLICY_INVALID"


def test_capture_validates_and_hashes_each_policy_from_one_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, teacher = _policies(tmp_path)
    original_read_bytes = Path.read_bytes
    original_payloads = {
        state: original_read_bytes(state),
        teacher: original_read_bytes(teacher),
    }
    reads: list[Path] = []

    def read_then_replace(path: Path) -> bytes:
        payload = original_read_bytes(path)
        if path in original_payloads:
            reads.append(path)
            path.write_bytes(b"{invalid")
        return payload

    monkeypatch.setattr(Path, "read_bytes", read_then_replace)

    captured = capture_assessment_dependencies(
        knowledge_bundle=_bundle(),
        evidence_index_ref=_index(),
        state_policy_path=state,
        teacher_policy_path=teacher,
    )

    assert reads == [state, teacher]
    assert captured.state_policy_checksum == hashlib.sha256(
        original_payloads[state]
    ).hexdigest()
    assert captured.teacher_policy_checksum == hashlib.sha256(
        original_payloads[teacher]
    ).hexdigest()


def test_verify_validates_and_hashes_each_policy_from_one_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, teacher = _policies(tmp_path)
    captured = capture_assessment_dependencies(
        knowledge_bundle=_bundle(),
        evidence_index_ref=_index(),
        state_policy_path=state,
        teacher_policy_path=teacher,
    )
    original_read_bytes = Path.read_bytes
    policy_paths = frozenset((state, teacher))
    reads: list[Path] = []

    def read_then_replace(path: Path) -> bytes:
        payload = original_read_bytes(path)
        if path in policy_paths:
            reads.append(path)
            path.write_bytes(b"{invalid")
        return payload

    monkeypatch.setattr(Path, "read_bytes", read_then_replace)

    verify_policy_dependencies(
        captured,
        state_policy_path=state,
        teacher_policy_path=teacher,
    )

    assert reads == [state, teacher]
