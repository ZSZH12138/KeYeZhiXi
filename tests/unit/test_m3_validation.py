from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from course_insight.contracts.course import (
    ContentChunk,
    CoursePackage,
    SourceAuthorization,
    SourceDocument,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import KnowledgeBundle
from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m3_knowledge_bundle.seed_snapshot import (
    M3SeedSnapshot,
    M3ValidationIssue,
    SeedRoleSnapshot,
    _snapshot_checksum,
    capture_seed_snapshot,
    validation_report_to_bytes,
)
from course_insight.modules.m3_knowledge_bundle.selection import (
    BlueprintSelectionError,
    select_blueprint_items,
)
from course_insight.modules.m3_knowledge_bundle.validation import (
    M3ValidationOutcome,
    _has_directed_cycle,
    validate_seed_snapshot,
)
from course_insight.modules.m8_assessment_scoring.paper_generator import PaperGenerator


NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)


def _course_package(*, status: str = "ready") -> CoursePackage:
    payload: dict[str, Any] = {
        "course_package_id": "package_1",
        "course_id": "course_1",
        "package_version": "v1",
        "source_documents": [
            SourceDocument(
                source_id="source_1",
                file_name="course.txt",
                media_type="text/plain",
                sha256="1" * 64,
                page_count=1,
                title="Governed course",
                version="v1",
            )
        ],
        "content_chunks": [
            ContentChunk(
                chunk_id="chunk_1",
                source_id="source_1",
                text="first governed fact",
                locator="line:1",
                concept_hints=["concept_1"],
                sha256="2" * 64,
            ),
            ContentChunk(
                chunk_id="chunk_2",
                source_id="source_1",
                text="second governed fact",
                locator="line:2",
                concept_hints=["concept_1"],
                sha256="3" * 64,
            ),
        ],
        "source_authorizations": [
            SourceAuthorization(
                source_id="source_1",
                authorized_by="teacher",
                license_note="course use",
                authorized_at=NOW,
            )
        ],
        "imported_at": NOW,
        "status": status,
        "checksum": "0" * 64,
    }
    provisional = CoursePackage.model_validate(payload)
    payload["checksum"] = provisional.recalculate_checksum()
    return CoursePackage.model_validate(payload)


def _roles(package: CoursePackage) -> dict[str, dict[str, Any]]:
    return {
        "concept": {
            "knowledge_bundle_id": "bundle_1",
            "bundle_version": "v1",
            "published_at": NOW.isoformat(),
            "course_id": package.course_id,
            "course_package_id": package.course_package_id,
            "course_package_checksum": package.checksum,
            "concepts": [
                {
                    "concept_id": "concept_1",
                    "name": "Linear equation",
                    "chapter_id": "chapter_1",
                    "description": "Solve one-variable equations.",
                    "aliases": ["equation"],
                    "status": "  PUBLISHED ",
                }
            ],
            "concept_evidence_ids": {"concept_1": ["evidence_chunk_1"]},
        },
        "item": {
            "items": [
                {
                    "item_id": "item_1",
                    "version": "v1",
                    "stem": "One plus one equals two.",
                    "item_type": "true_false",
                    "concept_ids": ["concept_1"],
                    "misconception_ids": [],
                    "difficulty_level": 1,
                    "cognitive_level": "remember",
                    "parameter_rules": [],
                    "answer_key": {"answer": True, "max_score": 1.0},
                    "rubric_id": None,
                    "source_evidence_ids": ["evidence_chunk_1"],
                    "status": " TEACHER_APPROVED ",
                },
                {
                    "item_id": "item_2",
                    "version": "v1",
                    "stem": "Explain the governed fact.",
                    "item_type": "short_answer",
                    "concept_ids": ["concept_1"],
                    "misconception_ids": [],
                    "difficulty_level": 2,
                    "cognitive_level": "explain",
                    "parameter_rules": [],
                    "answer_key": {},
                    "rubric_id": "rubric_1",
                    "source_evidence_ids": ["evidence_chunk_2"],
                    "status": "teacher_approved",
                },
            ],
            "q_matrix": [
                {
                    "item_id": "item_1",
                    "item_version": "v1",
                    "concept_id": "concept_1",
                    "weight": 1.0,
                },
                {
                    "item_id": "item_2",
                    "item_version": "v1",
                    "concept_id": "concept_1",
                    "weight": 1.0,
                },
            ],
        },
        "rubric": {
            "rubrics": [
                {
                    "rubric_id": "rubric_1",
                    "version": "v1",
                    "total_score": 2.0,
                    "criteria": [
                        {
                            "criterion_id": "criterion_1",
                            "description": "Explains the fact.",
                            "max_score": 2.0,
                            "expected_student_evidence": "A valid explanation.",
                            "course_evidence_ids": ["evidence_chunk_2"],
                        }
                    ],
                    "review_policy": {
                        "low_confidence_threshold": 0.5,
                        "require_evidence_for_positive_score": True,
                    },
                    "status": " published ",
                }
            ]
        },
        "blueprint": {
            "blueprints": [
                {
                    "blueprint_id": "blueprint_1",
                    "version": "v1",
                    "course_id": package.course_id,
                    "sections": [
                        {
                            "section_id": "section_1",
                            "name": "Mixed section",
                            "item_count": 2,
                            "score": 3.0,
                            "item_types": [],
                            "concept_weights": {"concept_1": 1.0},
                            "difficulty_range": [1, 2],
                            "anchor_item_ids": ["item_1"],
                            "anchor_item_versions": {"item_1": "v1"},
                        }
                    ],
                    "total_score": 3.0,
                    "duration_minutes": 30,
                    "status": " teacher_approved ",
                }
            ]
        },
        "prerequisite": {"prerequisite_relations": []},
        "misconception": {"misconception_tags": []},
    }


def _snapshot(tmp_path: Path, roles: dict[str, dict[str, Any]]) -> M3SeedSnapshot:
    tmp_path.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for role, payload in roles.items():
        path = tmp_path / f"{role}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        paths[role] = path
    return capture_seed_snapshot(
        concept_seed_path=paths["concept"],
        item_seed_path=paths["item"],
        rubric_seed_path=paths["rubric"],
        blueprint_seed_path=paths["blueprint"],
        prerequisite_seed_path=paths["prerequisite"],
        misconception_seed_path=paths["misconception"],
    )


def _validate(
    tmp_path: Path,
    *,
    mutate: Callable[[dict[str, dict[str, Any]]], None] | None = None,
    package: CoursePackage | None = None,
    schema_validator: Callable[[KnowledgeBundle], bool] | None = None,
) -> M3ValidationOutcome:
    selected_package = package or _course_package()
    roles = copy.deepcopy(_roles(selected_package))
    if mutate is not None:
        mutate(roles)
    return validate_seed_snapshot(
        course_package=selected_package,
        snapshot=_snapshot(tmp_path, roles),
        schema_validator=schema_validator,
    )


def _codes(outcome: M3ValidationOutcome) -> set[str]:
    return {issue.code for issue in outcome.report.issues}


def test_valid_snapshot_publishes_complete_package_bound_bundle(tmp_path: Path) -> None:
    outcome = _validate(tmp_path)

    assert outcome.report.status == "approved"
    assert outcome.report.issues == ()
    assert outcome.bundle is not None
    assert outcome.bundle.course_package_checksum == _course_package().checksum
    assert outcome.bundle.concept_evidence_ids == {
        "concept_1": ["evidence_chunk_1"]
    }
    assert outcome.bundle.blueprints[0].sections[0].anchor_item_versions == {
        "item_1": "v1"
    }
    assert outcome.report.bundle_checksum == outcome.bundle.content_checksum()


@pytest.mark.parametrize(
    "field",
    ["difficulty_range", "item_count", "item_types", "misconception_ids"],
)
def test_task3_issue_field_extensions_are_private_and_strict(field: str) -> None:
    assert M3ValidationIssue("BLUEPRINT_INVALID", "blueprint", "blueprint[0]", field)
    with pytest.raises(ValueError):
        M3ValidationIssue(
            "BLUEPRINT_INVALID", "blueprint", "blueprint[0]", "teacher text"
        )


def test_rejects_nonready_and_recalculated_checksum_invalid_m1(tmp_path: Path) -> None:
    draft = _course_package(status="draft")
    draft_outcome = _validate(tmp_path / "draft", package=draft)
    tampered = _course_package().model_copy(update={"checksum": "f" * 64})
    checksum_outcome = _validate(tmp_path / "checksum", package=tampered)

    assert "COURSE_PACKAGE_NOT_READY" in _codes(draft_outcome)
    assert "COURSE_PACKAGE_CHECKSUM_INVALID" in _codes(checksum_outcome)
    assert draft_outcome.bundle is checksum_outcome.bundle is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("course_id", "other_course"),
        ("course_package_id", "other_package"),
        ("course_package_checksum", "f" * 64),
    ],
)
def test_rejects_exact_course_package_binding_mismatch(
    tmp_path: Path, field: str, value: str
) -> None:
    outcome = _validate(
        tmp_path,
        mutate=lambda roles: roles["concept"].__setitem__(field, value),
    )

    assert "COURSE_BINDING_MISMATCH" in _codes(outcome)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda roles: roles["concept"]["concepts"].append(
            {
                **roles["concept"]["concepts"][0],
                "concept_id": "concept_2",
                "name": "Ｌｉｎｅａｒ   Equation",
                "aliases": ["other"],
            }
        ),
        lambda roles: roles["concept"]["concepts"][0].__setitem__("aliases", ["   "]),
        lambda roles: roles["concept"]["concepts"][0].__setitem__(
            "aliases", ["Equation", "ｅｑｕａｔｉｏｎ"]
        ),
    ],
)
def test_rejects_nfkc_name_alias_collisions_blank_and_duplicates(
    tmp_path: Path, mutation: Callable[[dict[str, dict[str, Any]]], None]
) -> None:
    def apply(roles: dict[str, dict[str, Any]]) -> None:
        mutation(roles)
        concepts = roles["concept"]["concepts"]
        roles["concept"]["concept_evidence_ids"] = {
            concept["concept_id"]: ["evidence_chunk_1"] for concept in concepts
        }

    outcome = _validate(tmp_path, mutate=apply)

    assert {"CONCEPT_NAME_COLLISION", "CONCEPT_ALIAS_INVALID"} & _codes(outcome)


def test_status_matrix_folds_whitespace_and_case_but_not_nfkc(tmp_path: Path) -> None:
    assert _validate(tmp_path / "allowed").report.status == "approved"
    mutations = {
        "concept": lambda roles: roles["concept"]["concepts"][0].__setitem__(
            "status", "ｐｕｂｌｉｓｈｅｄ"
        ),
        "item": lambda roles: roles["item"]["items"][0].__setitem__(
            "status", "ｔｅａｃｈｅｒ＿ａｐｐｒｏｖｅｄ"
        ),
        "rubric": lambda roles: roles["rubric"]["rubrics"][0].__setitem__(
            "status", "ｐｕｂｌｉｓｈｅｄ"
        ),
        "blueprint": lambda roles: roles["blueprint"]["blueprints"][0].__setitem__(
            "status", "ｔｅａｃｈｅｒ＿ａｐｐｒｏｖｅｄ"
        ),
    }
    for role, mutation in mutations.items():
        outcome = _validate(tmp_path / role, mutate=mutation)
        assert f"{role.upper()}_STATUS_INVALID" in _codes(outcome)


def test_prerequisite_cycle_checks_only_prerequisite_edges(tmp_path: Path) -> None:
    def concepts_two(roles: dict[str, dict[str, Any]]) -> None:
        second = {
            **roles["concept"]["concepts"][0],
            "concept_id": "concept_2",
            "name": "Second concept",
            "aliases": ["second"],
        }
        roles["concept"]["concepts"].append(second)
        roles["concept"]["concept_evidence_ids"]["concept_2"] = [
            "evidence_chunk_2"
        ]

    def cycle(roles: dict[str, dict[str, Any]], relation_type: str) -> None:
        concepts_two(roles)
        roles["prerequisite"]["prerequisite_relations"] = [
            {
                "from_concept_id": "concept_1",
                "to_concept_id": "concept_2",
                "relation_type": relation_type,
                "strength": 1.0,
            },
            {
                "from_concept_id": "concept_2",
                "to_concept_id": "concept_1",
                "relation_type": relation_type,
                "strength": 1.0,
            },
        ]

    rejected = _validate(
        tmp_path / "prerequisite",
        mutate=lambda roles: cycle(roles, "prerequisite"),
    )
    allowed = _validate(
        tmp_path / "related", mutate=lambda roles: cycle(roles, "related")
    )

    assert "PREREQUISITE_CYCLE" in _codes(rejected)
    assert allowed.report.status == "approved"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda roles: roles["prerequisite"].__setitem__(
            "prerequisite_relations",
            [
                {
                    "from_concept_id": "missing",
                    "to_concept_id": "concept_1",
                    "relation_type": "prerequisite",
                    "strength": 1.0,
                }
            ],
        ),
        lambda roles: roles["misconception"].__setitem__(
            "misconception_tags",
            [
                {
                    "misconception_id": "mis_1",
                    "name": "Bad path C:\\private\\teacher.json",
                    "description": "/private/teacher.json",
                    "concept_ids": ["missing"],
                    "evidence_rules": [],
                }
            ],
        ),
        lambda roles: roles["item"]["items"][0].__setitem__(
            "misconception_ids", ["missing"]
        ),
    ],
)
def test_rejects_missing_relation_misconception_and_item_endpoints(
    tmp_path: Path, mutation: Callable[[dict[str, dict[str, Any]]], None]
) -> None:
    outcome = _validate(tmp_path, mutate=mutation)

    assert outcome.report.status == "rejected"
    report_bytes = validation_report_to_bytes(outcome.report)
    assert b"teacher.json" not in report_bytes
    assert b"missing" not in report_bytes


def test_rejects_item_version_approval_and_subjective_rubric_rules(tmp_path: Path) -> None:
    def bad_version(roles: dict[str, dict[str, Any]]) -> None:
        roles["item"]["q_matrix"][0]["item_version"] = "missing"

    def double_approved(roles: dict[str, dict[str, Any]]) -> None:
        duplicate = copy.deepcopy(roles["item"]["items"][0])
        duplicate["version"] = "v2"
        roles["item"]["items"].append(duplicate)
        roles["item"]["q_matrix"].append(
            {
                "item_id": "item_1",
                "item_version": "v2",
                "concept_id": "concept_1",
                "weight": 1.0,
            }
        )

    def subjective_without_rubric(roles: dict[str, dict[str, Any]]) -> None:
        roles["item"]["items"][1]["rubric_id"] = None

    assert "Q_MATRIX_CONFLICT" in _codes(
        _validate(tmp_path / "version", mutate=bad_version)
    )
    assert "ITEM_MULTIPLE_APPROVED_VERSIONS" in _codes(
        _validate(tmp_path / "approved", mutate=double_approved)
    )
    assert "SUBJECTIVE_RUBRIC_REQUIRED" in _codes(
        _validate(tmp_path / "rubric", mutate=subjective_without_rubric)
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda roles: roles["rubric"]["rubrics"][0].__setitem__(
            "total_score", 3.0
        ),
        lambda roles: roles["rubric"]["rubrics"][0].__setitem__("criteria", []),
        lambda roles: roles["rubric"]["rubrics"][0]["criteria"][0].__setitem__(
            "course_evidence_ids", ["evidence_random"]
        ),
    ],
)
def test_rejects_rubric_total_criterion_and_evidence_rules(
    tmp_path: Path, mutation: Callable[[dict[str, dict[str, Any]]], None]
) -> None:
    assert _validate(tmp_path, mutate=mutation).report.status == "rejected"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda roles: roles["item"]["q_matrix"].pop(),
        lambda roles: roles["item"]["q_matrix"][0].__setitem__("weight", 0.0),
        lambda roles: roles["item"]["q_matrix"].append(
            {
                "item_id": "item_1",
                "item_version": "v1",
                "concept_id": "concept_1",
                "weight": 0.5,
            }
        ),
    ],
)
def test_q_matrix_requires_exact_positive_declared_triples(
    tmp_path: Path, mutation: Callable[[dict[str, dict[str, Any]]], None]
) -> None:
    assert "Q_MATRIX_CONFLICT" in _codes(_validate(tmp_path, mutate=mutation))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda roles: roles["blueprint"]["blueprints"][0].__setitem__(
            "course_id", "other_course"
        ),
        lambda roles: roles["blueprint"]["blueprints"][0]["sections"][0].__setitem__(
            "concept_weights", {"missing": 1.0}
        ),
        lambda roles: roles["blueprint"]["blueprints"][0]["sections"][0].__setitem__(
            "difficulty_range", [3, 1]
        ),
        lambda roles: roles["blueprint"]["blueprints"][0]["sections"][0].__setitem__(
            "anchor_item_versions", {}
        ),
        lambda roles: roles["blueprint"]["blueprints"][0]["sections"][0][
            "anchor_item_versions"
        ].__setitem__("item_1", "missing"),
        lambda roles: roles["blueprint"]["blueprints"][0]["sections"][0].__setitem__(
            "item_count", 3
        ),
        lambda roles: roles["blueprint"]["blueprints"][0]["sections"][0].__setitem__(
            "score", 4.0
        ),
    ],
)
def test_rejects_blueprint_binding_constraints_and_selection_mismatch(
    tmp_path: Path, mutation: Callable[[dict[str, dict[str, Any]]], None]
) -> None:
    assert _validate(tmp_path, mutate=mutation).report.status == "rejected"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda roles: roles["concept"].__setitem__("concept_evidence_ids", {}),
        lambda roles: roles["concept"]["concept_evidence_ids"].__setitem__(
            "extra", ["evidence_chunk_1"]
        ),
        lambda roles: roles["item"]["items"][0].__setitem__(
            "source_evidence_ids", []
        ),
        lambda roles: roles["item"]["items"][0].__setitem__(
            "source_evidence_ids", ["evidence_chunk_1", "evidence_chunk_1"]
        ),
        lambda roles: roles["item"]["items"][0].__setitem__(
            "source_evidence_ids", ["chunk_1"]
        ),
        lambda roles: roles["item"]["items"][0].__setitem__(
            "source_evidence_ids", ["evidence_chunk_1\x00query"]
        ),
        lambda roles: roles["item"]["items"][0].__setitem__(
            "source_evidence_ids", ["evidence_chunk_404"]
        ),
    ],
)
def test_all_publication_evidence_is_complete_unique_canonical_and_in_m1(
    tmp_path: Path, mutation: Callable[[dict[str, dict[str, Any]]], None]
) -> None:
    assert _validate(tmp_path, mutate=mutation).report.status == "rejected"


def test_evidence_validation_uses_shared_reverse_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from course_insight.contracts.evidence import (
        chunk_id_for_evidence_id as shared_reverse,
    )
    import course_insight.modules.m3_knowledge_bundle.validation as validation

    observed: list[str] = []

    def recording_reverse(evidence_id: str) -> str:
        observed.append(evidence_id)
        return shared_reverse(evidence_id)

    monkeypatch.setattr(validation, "chunk_id_for_evidence_id", recording_reverse)
    outcome = _validate(tmp_path)

    assert outcome.report.status == "approved"
    assert set(observed) == {"evidence_chunk_1", "evidence_chunk_2"}


@pytest.mark.parametrize("result", [False, 1, "true", None])
def test_schema_validator_false_and_nonbool_are_stable(
    tmp_path: Path, result: object
) -> None:
    outcome = _validate(
        tmp_path,
        schema_validator=lambda bundle: result,  # type: ignore[return-value]
    )

    expected = "SCHEMA_VALIDATOR_REJECTED" if result is False else "SCHEMA_VALIDATOR_ERROR"
    assert expected in _codes(outcome)
    assert outcome.bundle is None


def test_schema_validator_exception_and_state_change_do_not_leak(tmp_path: Path) -> None:
    secret = "HOST_SECRET C:\\teachers\\seed.json /private/seed.json"

    def exploding(_: KnowledgeBundle) -> bool:
        raise RuntimeError(secret)

    rejected = _validate(tmp_path / "exception", schema_validator=exploding)
    calls = 0

    def stateful(_: KnowledgeBundle) -> bool:
        nonlocal calls
        calls += 1
        return calls == 1

    first = _validate(tmp_path / "first", schema_validator=stateful)
    second = _validate(tmp_path / "second", schema_validator=stateful)

    assert "SCHEMA_VALIDATOR_ERROR" in _codes(rejected)
    assert secret.encode() not in validation_report_to_bytes(rejected.report)
    assert first.report.status == "approved"
    assert "SCHEMA_VALIDATOR_REJECTED" in _codes(second)


def test_issues_are_unique_sorted_ordinal_only_and_nonleaking(tmp_path: Path) -> None:
    def malicious(roles: dict[str, dict[str, Any]]) -> None:
        roles["concept"]["concepts"][0].update(
            {
                "concept_id": "C:\\teachers\\secret.json",
                "name": "/private/teacher label",
                "aliases": ["   "],
                "status": "draft",
            }
        )
        roles["concept"]["concept_evidence_ids"] = {
            "C:\\teachers\\secret.json": ["evidence_missing"]
        }

    outcome = _validate(tmp_path, mutate=malicious)
    payload = validation_report_to_bytes(outcome.report)

    assert outcome.report.issues == tuple(sorted(set(outcome.report.issues)))
    assert all(
        not issue.entity_key or "[" in issue.entity_key for issue in outcome.report.issues
    )
    assert b"teachers" not in payload
    assert b"private" not in payload
    assert b"secret" not in payload


def test_selector_is_version_aware_ordered_and_legacy_fail_closed(tmp_path: Path) -> None:
    bundle = _validate(tmp_path).bundle
    assert bundle is not None
    blueprint = bundle.blueprints[0]
    selected = select_blueprint_items(bundle, blueprint)
    assert [(item.item_id, item.version) for item in selected[0]] == [
        ("item_1", "v1"),
        ("item_2", "v1"),
    ]

    payload = bundle.to_dict()
    duplicate = copy.deepcopy(payload["items"][0])
    duplicate["version"] = "v2"
    payload["items"].insert(0, duplicate)
    payload["q_matrix"].append(
        {
            "item_id": "item_1",
            "item_version": "v2",
            "concept_id": "concept_1",
            "weight": 1.0,
        }
    )
    versioned = KnowledgeBundle.model_validate(payload)
    selected = select_blueprint_items(versioned, versioned.blueprints[0])
    assert selected[0][0].version == "v1"

    legacy_payload = versioned.to_dict()
    legacy_payload["blueprints"][0]["sections"][0].pop("anchor_item_versions")
    legacy = KnowledgeBundle.model_validate(legacy_payload)
    with pytest.raises(BlueprintSelectionError):
        select_blueprint_items(legacy, legacy.blueprints[0])


def test_selector_rejects_cross_section_reuse_and_score_drift(tmp_path: Path) -> None:
    bundle = _validate(tmp_path).bundle
    assert bundle is not None
    payload = bundle.to_dict()
    first = payload["blueprints"][0]["sections"][0]
    first.update({"item_count": 1, "score": 1.0})
    second = copy.deepcopy(first)
    second["section_id"] = "section_2"
    payload["blueprints"][0]["sections"].append(second)
    payload["blueprints"][0]["total_score"] = 2.0
    reused = KnowledgeBundle.model_validate(payload)
    with pytest.raises(BlueprintSelectionError):
        select_blueprint_items(reused, reused.blueprints[0])

    drift_payload = bundle.to_dict()
    drift_payload["blueprints"][0]["sections"][0]["score"] = 3.00001
    drift_payload["blueprints"][0]["total_score"] = 3.00001
    drift = KnowledgeBundle.model_validate(drift_payload)
    with pytest.raises(BlueprintSelectionError):
        select_blueprint_items(drift, drift.blueprints[0])


def test_m3_published_item_and_blueprint_are_accepted_by_m8(tmp_path: Path) -> None:
    outcome = _validate(tmp_path)
    bundle = outcome.bundle
    assert bundle is not None
    assert all(item.is_approved() for item in bundle.items)
    task = TaskPlan(
        task_id="task_1",
        task_type="stage_assessment",
        course_id=bundle.course_id,
        class_id="class_1",
        learner_id="learner_1",
        session_id="session_1",
        blueprint_id="blueprint_1",
        knowledge_bundle_id=bundle.knowledge_bundle_id,
        course_package_id=bundle.course_package_id,
        workflow=["M8", "M2", "M7", "M5", "M6", "M9"],
        next_module="M8",
        created_at=NOW,
    )

    paper = PaperGenerator().generate(task, bundle, None, None)

    assert paper.blueprint_id == "blueprint_1"
    assert [(item.item_id, item.item_version) for item in paper.sections[0].items] == [
        ("item_1", "v1"),
        ("item_2", "v1"),
    ]


def test_m8_maps_shared_selector_failure_to_existing_domain_error(tmp_path: Path) -> None:
    bundle = _validate(tmp_path).bundle
    assert bundle is not None
    payload = bundle.to_dict()
    payload["blueprints"][0]["sections"][0]["score"] = 4.0
    payload["blueprints"][0]["total_score"] = 4.0
    invalid = KnowledgeBundle.model_validate(payload)
    task = TaskPlan(
        task_id="task_1",
        task_type="stage_assessment",
        course_id=invalid.course_id,
        class_id="class_1",
        learner_id="learner_1",
        session_id="session_1",
        blueprint_id="blueprint_1",
        knowledge_bundle_id=invalid.knowledge_bundle_id,
        course_package_id=invalid.course_package_id,
        workflow=["M8", "M2", "M7", "M5", "M6", "M9"],
        next_module="M8",
        created_at=NOW,
    )

    with pytest.raises(DomainError) as raised:
        PaperGenerator().generate(task, invalid, None, None)

    assert raised.value.code == "BLUEPRINT_UNSATISFIABLE"
    assert raised.value.module == "m8"


def test_cycle_detector_handles_more_than_two_thousand_nodes_without_recursion() -> None:
    graph = {str(index): {str(index + 1)} for index in range(2001)}
    graph["2001"] = set()

    assert _has_directed_cycle(graph) is False
    graph["2001"] = {"0"}
    assert _has_directed_cycle(graph) is True


def test_anchorless_section_with_empty_version_map_is_publishable(tmp_path: Path) -> None:
    def anchorless(roles: dict[str, dict[str, Any]]) -> None:
        section = roles["blueprint"]["blueprints"][0]["sections"][0]
        section["anchor_item_ids"] = []
        section["anchor_item_versions"] = {}

    outcome = _validate(tmp_path, mutate=anchorless)

    assert outcome.report.status == "approved"
    assert outcome.bundle is not None


def test_incomplete_item_role_keeps_local_checks_without_downstream_cascade(
    tmp_path: Path,
) -> None:
    def invalid_then_draft(roles: dict[str, dict[str, Any]]) -> None:
        del roles["item"]["items"][0]["stem"]
        roles["item"]["items"][1]["status"] = "draft"

    outcome = _validate(tmp_path, mutate=invalid_then_draft)

    assert outcome.bundle is None
    assert outcome.report.issues == (
        M3ValidationIssue("ITEM_INVALID", "item", "item[0]", "items"),
        M3ValidationIssue(
            "ITEM_STATUS_INVALID",
            "item",
            "item[1]",
            "status",
        ),
    )


def test_complete_item_role_preserves_later_source_ordinal(tmp_path: Path) -> None:
    def second_is_draft(roles: dict[str, dict[str, Any]]) -> None:
        roles["item"]["items"][1]["status"] = "draft"

    outcome = _validate(tmp_path, mutate=second_is_draft)

    assert outcome.report.issues == (
        M3ValidationIssue(
            "ITEM_STATUS_INVALID",
            "item",
            "item[1]",
            "status",
        ),
    )


def test_q_matrix_parse_completeness_is_independent_and_non_cascading(
    tmp_path: Path,
) -> None:
    def invalid_q_row(roles: dict[str, dict[str, Any]]) -> None:
        del roles["item"]["q_matrix"][0]["weight"]

    outcome = _validate(tmp_path, mutate=invalid_q_row)

    assert outcome.report.issues == (
        M3ValidationIssue("Q_MATRIX_CONFLICT", "item", "", "q_matrix"),
    )


def test_incomplete_item_role_keeps_later_local_evidence_source_ordinal(
    tmp_path: Path,
) -> None:
    def invalid_then_bad_evidence(roles: dict[str, dict[str, Any]]) -> None:
        del roles["item"]["items"][0]["stem"]
        roles["item"]["items"][1]["source_evidence_ids"] = [
            "evidence_missing"
        ]

    outcome = _validate(tmp_path, mutate=invalid_then_bad_evidence)

    assert outcome.report.issues == tuple(
        sorted(
            (
                M3ValidationIssue("ITEM_INVALID", "item", "item[0]", "items"),
                M3ValidationIssue(
                    "ITEM_EVIDENCE_INVALID",
                    "item",
                    "item[1]",
                    "source_evidence_ids",
                ),
            )
        )
    )


def test_incomplete_rubric_role_keeps_later_local_evidence_source_ordinal(
    tmp_path: Path,
) -> None:
    def invalid_then_bad_evidence(roles: dict[str, dict[str, Any]]) -> None:
        del roles["rubric"]["rubrics"][0]["total_score"]
        second = copy.deepcopy(roles["rubric"]["rubrics"][0])
        second["rubric_id"] = "rubric_2"
        second["total_score"] = 2.0
        second["criteria"][0]["course_evidence_ids"] = ["evidence_missing"]
        roles["rubric"]["rubrics"].append(second)

    outcome = _validate(tmp_path, mutate=invalid_then_bad_evidence)

    assert outcome.report.issues == tuple(
        sorted(
            (
                M3ValidationIssue(
                    "RUBRIC_INVALID", "rubric", "rubric[0]", "rubrics"
                ),
                M3ValidationIssue(
                    "RUBRIC_EVIDENCE_INVALID",
                    "rubric",
                    "rubric[1].criteria[0]",
                    "course_evidence_ids",
                ),
            )
        )
    )


def test_incomplete_concept_role_suppresses_prerequisite_graph_cascade(
    tmp_path: Path,
) -> None:
    def invalid_concept_with_cycle(roles: dict[str, dict[str, Any]]) -> None:
        del roles["concept"]["concepts"][0]["name"]
        roles["prerequisite"]["prerequisite_relations"] = [
            {
                "from_concept_id": "a",
                "to_concept_id": "b",
                "relation_type": "prerequisite",
                "strength": 1.0,
            },
            {
                "from_concept_id": "b",
                "to_concept_id": "a",
                "relation_type": "prerequisite",
                "strength": 1.0,
            },
        ]

    outcome = _validate(tmp_path, mutate=invalid_concept_with_cycle)

    assert outcome.report.issues == (
        M3ValidationIssue(
            "CONCEPT_INVALID",
            "concept",
            "concept[0]",
            "concepts",
        ),
    )


def test_missing_prerequisite_endpoints_do_not_form_a_cycle(tmp_path: Path) -> None:
    def missing_endpoint_cycle(roles: dict[str, dict[str, Any]]) -> None:
        roles["prerequisite"]["prerequisite_relations"] = [
            {
                "from_concept_id": "concept_1",
                "to_concept_id": "missing",
                "relation_type": "prerequisite",
                "strength": 1.0,
            },
            {
                "from_concept_id": "missing",
                "to_concept_id": "concept_1",
                "relation_type": "prerequisite",
                "strength": 1.0,
            },
        ]

    outcome = _validate(tmp_path, mutate=missing_endpoint_cycle)

    assert outcome.report.issues == (
        M3ValidationIssue(
            "PREREQUISITE_REFERENCE_MISSING",
            "prerequisite",
            "prerequisite[0]",
            "to_concept_id",
        ),
        M3ValidationIssue(
            "PREREQUISITE_REFERENCE_MISSING",
            "prerequisite",
            "prerequisite[1]",
            "from_concept_id",
        ),
    )


def test_untrusted_invalid_role_code_is_safely_mapped_in_report(
    tmp_path: Path,
) -> None:
    package = _course_package()
    snapshot = _snapshot(tmp_path, _roles(package))
    roles = list(snapshot.roles)
    roles[1] = SeedRoleSnapshot(
        role="item",
        state="invalid",
        canonical_json=None,
        content_sha256=None,
        issue_code="TEACHER_SECRET_LABEL",
    )
    resigned_roles = tuple(roles)
    resigned = M3SeedSnapshot(
        roles=resigned_roles,
        checksum=_snapshot_checksum(resigned_roles),
    )

    outcome = validate_seed_snapshot(
        course_package=package,
        snapshot=resigned,
        schema_validator=None,
    )
    report_bytes = validation_report_to_bytes(outcome.report)

    assert outcome.report.issues == (
        M3ValidationIssue("SEED_SCHEMA_INVALID", "item", "", "items"),
    )
    assert b"TEACHER_SECRET_LABEL" not in report_bytes
    assert b"SECRET" not in report_bytes


def test_selector_rejects_wrong_course_and_external_blueprint(tmp_path: Path) -> None:
    bundle = _validate(tmp_path).bundle
    assert bundle is not None
    internal = bundle.blueprints[0]

    wrong_course = internal.model_copy(update={"course_id": "other_course"})
    with pytest.raises(BlueprintSelectionError):
        select_blueprint_items(bundle, wrong_course)

    external = internal.model_copy(update={"duration_minutes": 31})
    with pytest.raises(BlueprintSelectionError):
        select_blueprint_items(bundle, external)

    copied_internal = bundle.get_blueprint(internal.blueprint_id)
    assert select_blueprint_items(bundle, copied_internal)


@pytest.mark.parametrize("damage", ["checksum", "role_order"])
def test_snapshot_damage_fails_once_with_fixed_nonleaking_error(
    tmp_path: Path,
    damage: str,
) -> None:
    package = _course_package()
    snapshot = _snapshot(tmp_path, _roles(package))
    if damage == "checksum":
        damaged = replace(snapshot, checksum="0" * 64)
    else:
        damaged = replace(snapshot, roles=tuple(reversed(snapshot.roles)))

    with pytest.raises(ValueError) as raised:
        validate_seed_snapshot(
            course_package=package,
            snapshot=damaged,
            schema_validator=None,
        )

    assert str(raised.value) == "M3 seed snapshot is invalid"
    assert raised.value.__cause__ is None
