from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from course_insight.contracts.course import (
    ContentChunk,
    CoursePackage,
    SourceAuthorization,
    SourceDocument,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.modules.m3_knowledge_bundle.repository import M3Repository
from course_insight.modules.m3_knowledge_bundle.seed_snapshot import (
    M3ValidationIssue,
    capture_seed_snapshot,
    create_validation_report,
)
from course_insight.modules.m3_knowledge_bundle.service import M3KnowledgeBundleService
from course_insight.modules.m3_knowledge_bundle import stubs as m3_stubs
from course_insight.modules.m3_knowledge_bundle.stubs import _MemoryM3Repository
from course_insight.modules.m3_knowledge_bundle.teacher_review import (
    InMemoryTeacherReviewRepository,
    TeacherReviewWorkflow,
)
from course_insight.modules.m3_knowledge_bundle.validation import M3ValidationOutcome


NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _package() -> CoursePackage:
    payload: dict[str, Any] = {
        "course_package_id": "package_1",
        "course_id": "course_1",
        "package_version": "v1",
        "source_documents": [SourceDocument(source_id="source_1", file_name="course.txt", media_type="text/plain", sha256="1" * 64, page_count=1, title="Course", version="v1")],
        "content_chunks": [
            ContentChunk(chunk_id="chunk_1", source_id="source_1", text="one", locator="line:1", concept_hints=["concept_1"], sha256="2" * 64),
            ContentChunk(chunk_id="chunk_2", source_id="source_1", text="two", locator="line:2", concept_hints=["concept_1"], sha256="3" * 64),
        ],
        "source_authorizations": [SourceAuthorization(source_id="source_1", authorized_by="teacher", license_note="course", authorized_at=NOW)],
        "imported_at": NOW,
        "status": "ready",
        "checksum": "0" * 64,
    }
    payload["checksum"] = CoursePackage.model_validate(payload).recalculate_checksum()
    return CoursePackage.model_validate(payload)


def _roles(package: CoursePackage, *, version: str = "v1") -> dict[str, dict[str, Any]]:
    return {
        "concept": {"knowledge_bundle_id": "bundle_1", "bundle_version": version, "published_at": NOW.isoformat(), "course_id": package.course_id, "course_package_id": package.course_package_id, "course_package_checksum": package.checksum, "concepts": [{"concept_id": "concept_1", "name": "Linear equation", "chapter_id": "chapter_1", "description": "Solve equations.", "aliases": ["equation"], "status": "published"}], "concept_evidence_ids": {"concept_1": ["evidence_chunk_1"]}},
        "item": {"items": [{"item_id": "item_1", "version": "v1", "stem": "One plus one equals two.", "item_type": "true_false", "concept_ids": ["concept_1"], "misconception_ids": [], "difficulty_level": 1, "cognitive_level": "remember", "parameter_rules": [], "answer_key": {"answer": True, "max_score": 1.0}, "rubric_id": None, "source_evidence_ids": ["evidence_chunk_1"], "status": "teacher_approved"}, {"item_id": "item_2", "version": "v1", "stem": "Explain the fact.", "item_type": "short_answer", "concept_ids": ["concept_1"], "misconception_ids": [], "difficulty_level": 2, "cognitive_level": "explain", "parameter_rules": [], "answer_key": {}, "rubric_id": "rubric_1", "source_evidence_ids": ["evidence_chunk_2"], "status": "teacher_approved"}], "q_matrix": [{"item_id": "item_1", "item_version": "v1", "concept_id": "concept_1", "weight": 1.0}, {"item_id": "item_2", "item_version": "v1", "concept_id": "concept_1", "weight": 1.0}]},
        "rubric": {"rubrics": [{"rubric_id": "rubric_1", "version": "v1", "total_score": 2.0, "criteria": [{"criterion_id": "criterion_1", "description": "Explains the fact.", "max_score": 2.0, "expected_student_evidence": "Explanation.", "course_evidence_ids": ["evidence_chunk_2"]}], "review_policy": {"low_confidence_threshold": 0.5, "double_score_disagreement_threshold": 1.0, "require_evidence_for_positive_score": True}, "status": "published"}]},
        "blueprint": {"blueprints": [{"blueprint_id": "blueprint_1", "version": "v1", "course_id": package.course_id, "sections": [{"section_id": "section_1", "name": "Mixed", "item_count": 2, "score": 3.0, "item_types": [], "concept_weights": {"concept_1": 1.0}, "difficulty_range": [1, 2], "anchor_item_ids": ["item_1"], "anchor_item_versions": {"item_1": "v1"}}], "total_score": 3.0, "duration_minutes": 30, "status": "teacher_approved"}]},
        "prerequisite": {"prerequisite_relations": []},
        "misconception": {"misconception_tags": []},
    }


def _paths(tmp_path: Path, roles: dict[str, dict[str, Any]], *, indent: int | None = None) -> dict[str, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for name, payload in roles.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=indent), encoding="utf-8")
        result[name] = path
    return result


def _build(service: M3KnowledgeBundleService, package: CoursePackage, paths: dict[str, Path]) -> KnowledgeBundle:
    return service.build_knowledge_bundle(package, paths["concept"], paths["item"], paths["rubric"], paths["blueprint"], paths["prerequisite"], paths["misconception"])


def test_teacher_review_api_binds_seed_checksum_before_production_publication(
    tmp_path: Path,
) -> None:
    package = _package()
    paths = _paths(tmp_path, _roles(package))
    workflow = TeacherReviewWorkflow(InMemoryTeacherReviewRepository())
    service = M3KnowledgeBundleService(
        _MemoryM3Repository(),
        None,
        review_workflow=workflow,
        require_teacher_approval=True,
    )

    draft = service.create_teacher_review_draft(
        review_id="review-bundle-1",
        subject_id=package.course_package_id,
        validation_report_ref="report-bundle-1",
        now=NOW,
        concept_seed_path=paths["concept"],
        item_seed_path=paths["item"],
        rubric_seed_path=paths["rubric"],
        blueprint_seed_path=paths["blueprint"],
        prerequisite_seed_path=paths["prerequisite"],
        misconception_seed_path=paths["misconception"],
    )
    submitted = service.submit_teacher_review(
        "review-bundle-1", "teacher-1", "checked", draft.version, NOW
    )
    approved = service.approve_teacher_review(
        "review-bundle-1", "teacher-1", "approved", submitted.version, NOW
    )

    bundle = service.build_knowledge_bundle_after_approval(
        review_id="review-bundle-1",
        review_version=approved.version,
        course_package=package,
        concept_seed_path=paths["concept"],
        item_seed_path=paths["item"],
        rubric_seed_path=paths["rubric"],
        blueprint_seed_path=paths["blueprint"],
        prerequisite_seed_path=paths["prerequisite"],
        misconception_seed_path=paths["misconception"],
    )

    assert bundle.course_package_checksum == package.checksum
    assert workflow.require_approved("review-bundle-1", approved.version).input_checksum == (
        capture_seed_snapshot(
            concept_seed_path=paths["concept"],
            item_seed_path=paths["item"],
            rubric_seed_path=paths["rubric"],
            blueprint_seed_path=paths["blueprint"],
            prerequisite_seed_path=paths["prerequisite"],
            misconception_seed_path=paths["misconception"],
        ).checksum
    )


def test_teacher_review_cannot_publish_against_a_different_course_package(
    tmp_path: Path,
) -> None:
    package = _package()
    paths = _paths(tmp_path, _roles(package))
    workflow = TeacherReviewWorkflow(InMemoryTeacherReviewRepository())
    service = M3KnowledgeBundleService(
        _MemoryM3Repository(),
        None,
        review_workflow=workflow,
        require_teacher_approval=True,
    )
    draft = service.create_teacher_review_draft(
        review_id="review-subject-binding",
        subject_id=package.course_package_id,
        validation_report_ref="report-subject-binding",
        now=NOW,
        concept_seed_path=paths["concept"],
        item_seed_path=paths["item"],
        rubric_seed_path=paths["rubric"],
        blueprint_seed_path=paths["blueprint"],
        prerequisite_seed_path=paths["prerequisite"],
        misconception_seed_path=paths["misconception"],
    )
    submitted = service.submit_teacher_review(
        "review-subject-binding", "teacher-1", "checked", draft.version, NOW
    )
    approved = service.approve_teacher_review(
        "review-subject-binding", "teacher-1", "approved", submitted.version, NOW
    )

    other_package = package.model_copy(update={"course_package_id": "other-package"})
    with pytest.raises(DomainError) as captured:
        service.build_knowledge_bundle_after_approval(
            review_id="review-subject-binding",
            review_version=approved.version,
            course_package=other_package,
            concept_seed_path=paths["concept"],
            item_seed_path=paths["item"],
            rubric_seed_path=paths["rubric"],
            blueprint_seed_path=paths["blueprint"],
            prerequisite_seed_path=paths["prerequisite"],
            misconception_seed_path=paths["misconception"],
        )

    assert captured.value.code == "M3_REVIEW_SUBJECT_MISMATCH"


class _RecordingRepository(_MemoryM3Repository):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def save_bundle_artifact(self, bundle: Any, report: Any, snapshot: Any) -> None:
        self.calls.append("approved")
        super().save_bundle_artifact(bundle, report, snapshot)

    def save_rejected_validation(self, report: Any, snapshot: Any) -> None:
        self.calls.append("rejected")
        super().save_rejected_validation(report, snapshot)


def test_build_persists_one_complete_approved_artifact_before_return(tmp_path: Path) -> None:
    package = _package()
    repository = _RecordingRepository()
    bundle = _build(M3KnowledgeBundleService(repository, None), package, _paths(tmp_path, _roles(package)))

    assert repository.calls == ["approved"]
    artifact = repository.load_bundle_artifact(bundle.knowledge_bundle_id, bundle.bundle_version)
    assert artifact is not None
    stored_bundle, report, snapshot = artifact
    assert report.status == "approved"
    assert report.bundle_checksum == stored_bundle.content_checksum()
    assert report.seed_snapshot_checksum == snapshot.checksum
    assert bundle is not stored_bundle


def test_blocking_validation_persists_only_rejected_report_then_raises_safe_id(tmp_path: Path) -> None:
    package = _package()
    roles = _roles(package)
    roles["item"]["q_matrix"] = []
    repository = _RecordingRepository()

    with pytest.raises(DomainError) as raised:
        _build(M3KnowledgeBundleService(repository, None), package, _paths(tmp_path, roles))

    assert raised.value.code == "Q_MATRIX_CONFLICT"
    assert raised.value.module == "m3"
    assert set(raised.value.details) == {"report_id"}
    assert repository.calls == ["rejected"]
    assert repository.load_rejected_validation(package.course_package_id, raised.value.details["report_id"]) is not None


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            lambda roles: roles["concept"]["concepts"][0].pop("name"),
            "KNOWLEDGE_ENTITY_INVALID",
        ),
        (
            lambda roles: roles["prerequisite"]["prerequisite_relations"].append(
                {
                    "from_concept_id": "concept_1",
                    "to_concept_id": "missing",
                    "relation_type": "prerequisite",
                    "strength": 1.0,
                }
            ),
            "KNOWLEDGE_REFERENCE_MISSING",
        ),
        (
            lambda roles: roles["concept"].__setitem__(
                "course_package_id", "other_package"
            ),
            "KNOWLEDGE_COURSE_BINDING_INVALID",
        ),
    ],
)
def test_validation_rejection_exposes_first_safe_report_code(
    tmp_path: Path,
    mutation: Any,
    expected_code: str,
) -> None:
    package = _package()
    roles = _roles(package)
    mutation(roles)
    repository = _RecordingRepository()

    with pytest.raises(DomainError) as raised:
        _build(
            M3KnowledgeBundleService(repository, None),
            package,
            _paths(tmp_path, roles),
        )

    assert raised.value.code == expected_code
    assert set(raised.value.details) == {"report_id"}
    assert raised.value.__cause__ is None
    assert str(tmp_path) not in str(raised.value)
    assert repository.calls == ["rejected"]


def test_seed_read_failure_exposes_safe_report_code(tmp_path: Path) -> None:
    package = _package()
    paths = _paths(tmp_path, _roles(package))
    paths["concept"].unlink()

    with pytest.raises(DomainError) as raised:
        _build(M3KnowledgeBundleService(_MemoryM3Repository(), None), package, paths)

    assert raised.value.code == "KNOWLEDGE_SEED_READ_FAILED"
    assert set(raised.value.details) == {"report_id"}
    assert raised.value.__cause__ is None
    assert str(tmp_path) not in str(raised.value)


def test_seed_json_and_schema_failures_expose_distinct_safe_report_codes(
    tmp_path: Path,
) -> None:
    package = _package()
    json_paths = _paths(tmp_path / "json", _roles(package))
    json_paths["concept"].write_text("{", encoding="utf-8")

    with pytest.raises(DomainError) as json_raised:
        _build(
            M3KnowledgeBundleService(_MemoryM3Repository(), None),
            package,
            json_paths,
        )

    schema_roles = _roles(package)
    schema_roles["concept"].pop("concepts")
    schema_paths = _paths(tmp_path / "schema", schema_roles)

    with pytest.raises(DomainError) as schema_raised:
        _build(
            M3KnowledgeBundleService(_MemoryM3Repository(), None),
            package,
            schema_paths,
        )

    assert json_raised.value.code == "KNOWLEDGE_SEED_INVALID"
    assert schema_raised.value.code == "KNOWLEDGE_SEED_INVALID"
    for raised in (json_raised, schema_raised):
        assert set(raised.value.details) == {"report_id"}
        assert raised.value.__cause__ is None
        assert str(tmp_path) not in str(raised.value)


def test_oversized_seed_exposes_seed_invalid_code(tmp_path: Path) -> None:
    package = _package()
    paths = _paths(tmp_path, _roles(package))
    paths["concept"].write_bytes(b"x" * (1024 * 1024 + 1))

    with pytest.raises(DomainError) as raised:
        _build(M3KnowledgeBundleService(_MemoryM3Repository(), None), package, paths)

    assert raised.value.code == "KNOWLEDGE_SEED_INVALID"
    assert set(raised.value.details) == {"report_id"}
    assert raised.value.__cause__ is None
    assert str(tmp_path) not in str(raised.value)


def test_q_matrix_conflict_has_priority_over_other_report_issues(
    tmp_path: Path,
) -> None:
    package = _package()
    roles = _roles(package)
    roles["concept"]["course_package_id"] = "other_package"
    roles["item"]["q_matrix"] = []

    with pytest.raises(DomainError) as raised:
        _build(
            M3KnowledgeBundleService(_MemoryM3Repository(), None),
            package,
            _paths(tmp_path, roles),
        )

    assert raised.value.code == "Q_MATRIX_CONFLICT"
    assert set(raised.value.details) == {"report_id"}
    assert raised.value.__cause__ is None


def test_unknown_report_issue_uses_fixed_safe_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package()
    paths = _paths(tmp_path, _roles(package))
    snapshot = capture_seed_snapshot(
        concept_seed_path=paths["concept"],
        item_seed_path=paths["item"],
        rubric_seed_path=paths["rubric"],
        blueprint_seed_path=paths["blueprint"],
        prerequisite_seed_path=paths["prerequisite"],
        misconception_seed_path=paths["misconception"],
    )
    report = create_validation_report(
        course_package_id=package.course_package_id,
        course_package_checksum=package.checksum,
        seed_snapshot=snapshot,
        issues=(
            M3ValidationIssue(
                "UNRECOGNIZED_INTERNAL_ISSUE", "concept", "", "concepts"
            ),
        ),
    )
    monkeypatch.setattr(
        "course_insight.modules.m3_knowledge_bundle.service.validate_seed_snapshot",
        lambda **_: M3ValidationOutcome(bundle=None, report=report),
    )

    with pytest.raises(DomainError) as raised:
        _build(M3KnowledgeBundleService(_MemoryM3Repository(), None), package, paths)

    assert raised.value.code == "KNOWLEDGE_SEED_INVALID"
    assert "UNRECOGNIZED" not in str(raised.value)
    assert set(raised.value.details) == {"report_id"}
    assert raised.value.__cause__ is None


@pytest.mark.parametrize("error", [RuntimeError("C:\\teacher\\seed.json"), DomainError(code="KNOWLEDGE_VERSION_CONFLICT", module="m3", message="C:\\teacher\\seed.json", details={"path": "C:\\teacher"}), DomainError(code="FOREIGN", module="m2", message="C:\\teacher\\seed.json", details={})])
def test_storage_failures_never_leave_success_or_leak_repository_errors(tmp_path: Path, error: Exception) -> None:
    package = _package()

    class FailingRepository(_MemoryM3Repository):
        def save_bundle_artifact(self, bundle: Any, report: Any, snapshot: Any) -> None:
            raise error

    repository = FailingRepository()
    with pytest.raises(DomainError) as raised:
        _build(M3KnowledgeBundleService(repository, None), package, _paths(tmp_path, _roles(package)))

    assert raised.value.code in {"KNOWLEDGE_ARTIFACT_INVALID", "KNOWLEDGE_VERSION_CONFLICT"}
    assert raised.value.module == "m3"
    assert raised.value.details == {}
    assert raised.value.__cause__ is None
    assert "teacher" not in str(raised.value)
    assert repository.bundles == {}


def test_memory_approved_save_is_atomic_when_compatibility_copy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _package()
    repository = _MemoryM3Repository()
    original_decode = m3_stubs._bundle_from_bytes

    def fail_compatibility_copy(payload: bytes) -> KnowledgeBundle:
        del payload
        raise RuntimeError("copy failed")

    monkeypatch.setattr(m3_stubs, "_bundle_from_bytes", fail_compatibility_copy)
    with pytest.raises(DomainError) as raised:
        _build(M3KnowledgeBundleService(repository, None), package, _paths(tmp_path, _roles(package)))
    monkeypatch.setattr(m3_stubs, "_bundle_from_bytes", original_decode)

    assert raised.value.code == "KNOWLEDGE_ARTIFACT_INVALID"
    assert repository.load_bundle_artifact("bundle_1", "v1") is None
    assert repository.bundles == {}


def test_memory_approved_save_rolls_back_when_legacy_mirror_write_fails(
    tmp_path: Path,
) -> None:
    package = _package()
    repository = _MemoryM3Repository()

    class FailingMirror(dict[tuple[str, str], KnowledgeBundle]):
        def __setitem__(self, key: tuple[str, str], value: KnowledgeBundle) -> None:
            del key, value
            raise RuntimeError("legacy mirror failed")

    repository.bundles = FailingMirror()
    with pytest.raises(DomainError) as raised:
        _build(M3KnowledgeBundleService(repository, None), package, _paths(tmp_path, _roles(package)))

    assert raised.value.code == "KNOWLEDGE_ARTIFACT_INVALID"
    assert repository.load_bundle_artifact("bundle_1", "v1") is None
    assert repository.bundles == {}


def test_build_returns_persisted_semantics_when_repository_mutates_input(tmp_path: Path) -> None:
    package = _package()

    class MutatingRepository(_MemoryM3Repository):
        def save_bundle_artifact(self, bundle: Any, report: Any, snapshot: Any) -> None:
            super().save_bundle_artifact(bundle, report, snapshot)
            bundle.concepts[0].name = "repository mutation"

    repository = MutatingRepository()
    result = _build(M3KnowledgeBundleService(repository, None), package, _paths(tmp_path, _roles(package)))
    persisted = repository.load_bundle_artifact("bundle_1", "v1")

    assert persisted is not None
    assert result.model_dump(mode="json") == persisted[0].model_dump(mode="json")
    assert result.concepts[0].name == "Linear equation"


def test_build_rejects_repository_substitution_of_a_coherent_other_artifact(
    tmp_path: Path,
) -> None:
    package = _package()

    class SubstitutingRepository:
        artifact: tuple[Any, Any, Any] | None = None

        def save_bundle_artifact(self, bundle: Any, report: Any, snapshot: Any) -> None:
            replacement = bundle.model_copy(deep=True)
            replacement.concepts[0].name = "A different but valid concept"
            replacement_report = create_validation_report(
                course_package_id=report.course_package_id,
                course_package_checksum=report.course_package_checksum,
                seed_snapshot=snapshot,
                issues=(),
                knowledge_bundle_id=replacement.knowledge_bundle_id,
                bundle_version=replacement.bundle_version,
                bundle_checksum=replacement.content_checksum(),
            )
            self.artifact = replacement, replacement_report, snapshot

        def load_bundle_artifact(self, *args: Any) -> Any:
            del args
            return self.artifact

    service = M3KnowledgeBundleService(SubstitutingRepository(), None)  # type: ignore[arg-type]
    with pytest.raises(DomainError) as raised:
        _build(service, package, _paths(tmp_path, _roles(package)))

    assert raised.value.code == "KNOWLEDGE_ARTIFACT_INVALID"
    assert raised.value.details == {}
    assert service._last_course_package is None


def test_build_is_format_independent_and_never_rereads_after_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = _package()
    first_paths = _paths(tmp_path / "first", _roles(package), indent=2)
    second_paths = _paths(tmp_path / "second", _roles(package))
    first_repo, second_repo = _MemoryM3Repository(), _MemoryM3Repository()
    original_capture = capture_seed_snapshot

    def capture_then_forbid(**kwargs: Any):
        snapshot = original_capture(**kwargs)
        monkeypatch.setattr(Path, "open", lambda *args, **inner: (_ for _ in ()).throw(AssertionError("seed reread")))
        return snapshot

    monkeypatch.setattr("course_insight.modules.m3_knowledge_bundle.service.capture_seed_snapshot", capture_then_forbid)
    first = _build(M3KnowledgeBundleService(first_repo, None), package, first_paths)
    monkeypatch.undo()
    second = _build(M3KnowledgeBundleService(second_repo, None), package, second_paths)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first_repo.load_bundle_artifact("bundle_1", "v1")[1] == second_repo.load_bundle_artifact("bundle_1", "v1")[1]


def test_restore_revalidates_complete_artifact_without_seed_files_and_rejects_tampering(tmp_path: Path) -> None:
    package = _package()
    repository = _MemoryM3Repository()
    service = M3KnowledgeBundleService(repository, None)
    built = _build(service, package, _paths(tmp_path, _roles(package)))
    restored = service.restore_knowledge_bundle(course_package=package, knowledge_bundle_id=built.knowledge_bundle_id, bundle_version=built.bundle_version)
    assert restored.model_dump(mode="json") == built.model_dump(mode="json")
    assert restored is not built
    bundle, report, snapshot = repository.load_bundle_artifact("bundle_1", "v1")

    class ForgedRepository(_MemoryM3Repository):
        def load_bundle_artifact(self, *args: Any) -> Any:
            return bundle.model_copy(update={"course_id": "forged"}), report, snapshot

    service = M3KnowledgeBundleService(ForgedRepository(), None)
    with pytest.raises(DomainError, match="knowledge artifact") as raised:
        service.restore_knowledge_bundle(course_package=package, knowledge_bundle_id="bundle_1", bundle_version="v1")
    assert raised.value.code == "KNOWLEDGE_ARTIFACT_INVALID"
    assert raised.value.details == {}


def test_restore_missing_and_memory_versions_are_isolated_and_legacy_save_fails_closed(tmp_path: Path) -> None:
    package = _package()
    repository = _MemoryM3Repository()
    service = M3KnowledgeBundleService(repository, None)
    first = _build(service, package, _paths(tmp_path / "v1", _roles(package)))
    second = _build(service, package, _paths(tmp_path / "v2", _roles(package, version="v2")))
    assert first.bundle_version == "v1" and second.bundle_version == "v2"
    loaded = repository.load_bundle_artifact("bundle_1", "v1")
    assert loaded is not None
    loaded[0].concepts[0].name = "mutated"
    assert repository.load_bundle_artifact("bundle_1", "v1")[0].concepts[0].name == "Linear equation"
    with pytest.raises(DomainError) as missing:
        service.restore_knowledge_bundle(course_package=package, knowledge_bundle_id="bundle_1", bundle_version="missing")
    assert missing.value.code == "KNOWLEDGE_NOT_READY"
    with pytest.raises(DomainError) as legacy:
        repository.save_knowledge_bundle(first)
    assert legacy.value.code == "KNOWLEDGE_ARTIFACT_INVALID"
