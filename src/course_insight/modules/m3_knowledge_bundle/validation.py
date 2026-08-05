"""Deterministic M3 publication validation over one captured seed snapshot."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from heapq import heapify, heappop, heappush
from typing import Any, TypeVar, cast

from course_insight.contracts.course import CoursePackage
from course_insight.contracts.evidence import chunk_id_for_evidence_id
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import (
    AssessmentBlueprint,
    ItemCard,
    KnowledgeBundle,
    KnowledgeConcept,
    MisconceptionTag,
    PrerequisiteRelation,
    QMatrixEntry,
    Rubric,
)
from course_insight.modules.m3_knowledge_bundle.seed_snapshot import (
    M3SeedSnapshot,
    M3ValidationIssue,
    M3ValidationReport,
    ROLE_ORDER,
    create_validation_report,
    role_payload,
    seed_snapshot_to_bytes,
)
from course_insight.modules.m3_knowledge_bundle.selection import (
    BlueprintSelectionError,
    select_blueprint_items,
)


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_SNAPSHOT_ISSUE_CODES = frozenset(
    {
        "SEED_READ_FAILED",
        "SEED_TOO_LARGE",
        "SEED_SCHEMA_INVALID",
        "SEED_JSON_INVALID",
    }
)
_ROLE_ROOT_FIELD = {
    "concept": "concepts",
    "item": "items",
    "rubric": "rubrics",
    "blueprint": "blueprints",
    "prerequisite": "prerequisite_relations",
    "misconception": "misconception_tags",
}
_MODEL_INVALID_CODE = {
    "concept": "CONCEPT_INVALID",
    "item": "ITEM_INVALID",
    "rubric": "RUBRIC_INVALID",
    "blueprint": "BLUEPRINT_INVALID",
    "prerequisite": "PREREQUISITE_INVALID",
    "misconception": "MISCONCEPTION_INVALID",
}
_MODEL_KEYS = {
    "concept": "concepts",
    "item": "items",
    "rubric": "rubrics",
    "blueprint": "blueprints",
    "prerequisite": "prerequisite_relations",
    "misconception": "misconception_tags",
}


@dataclass(frozen=True, slots=True)
class M3ValidationOutcome:
    bundle: KnowledgeBundle | None
    report: M3ValidationReport


@dataclass(frozen=True, slots=True)
class _ParsedEntry:
    source_index: int
    value: Any


@dataclass(frozen=True, slots=True)
class _ParsedCollection:
    entries: tuple[_ParsedEntry, ...]
    complete: bool

    @property
    def values(self) -> tuple[Any, ...]:
        return tuple(entry.value for entry in self.entries)


_ModelT = TypeVar("_ModelT")


def validate_seed_snapshot(
    *,
    course_package: CoursePackage,
    snapshot: M3SeedSnapshot,
    schema_validator: Callable[[KnowledgeBundle], bool] | None,
) -> M3ValidationOutcome:
    """Revalidate M1 and every captured semantic role, then publish or reject."""

    try:
        seed_snapshot_to_bytes(snapshot)
    except Exception:
        raise ValueError("M3 seed snapshot is invalid") from None

    issues: set[M3ValidationIssue] = set()
    validated_package, package_checksum = _validate_course_package(
        course_package, issues
    )
    payloads = _load_role_payloads(snapshot, issues)

    concept_payload = payloads.get("concept")
    if concept_payload is not None and validated_package is not None:
        _validate_package_binding(concept_payload, validated_package, issues)

    parsed_concepts = _parse_models(payloads, "concept", KnowledgeConcept, issues)
    parsed_items = _parse_models(payloads, "item", ItemCard, issues)
    parsed_rubrics = _parse_models(payloads, "rubric", Rubric, issues)
    parsed_blueprints = _parse_models(
        payloads, "blueprint", AssessmentBlueprint, issues
    )
    parsed_prerequisites = _parse_models(
        payloads, "prerequisite", PrerequisiteRelation, issues
    )
    parsed_misconceptions = _parse_models(
        payloads, "misconception", MisconceptionTag, issues
    )
    parsed_q_matrix = _parse_q_matrix(payloads, issues)

    concepts = cast(list[KnowledgeConcept], list(parsed_concepts.values))
    items = cast(list[ItemCard], list(parsed_items.values))
    rubrics = cast(list[Rubric], list(parsed_rubrics.values))
    blueprints = cast(list[AssessmentBlueprint], list(parsed_blueprints.values))
    prerequisites = cast(
        list[PrerequisiteRelation], list(parsed_prerequisites.values)
    )
    misconceptions = cast(
        list[MisconceptionTag], list(parsed_misconceptions.values)
    )
    q_matrix = cast(list[QMatrixEntry], list(parsed_q_matrix.values))

    _validate_statuses(
        parsed_concepts,
        parsed_items,
        parsed_rubrics,
        parsed_blueprints,
        issues,
    )
    _validate_concept_names(parsed_concepts, issues)
    _validate_identifiers(
        concepts,
        items,
        rubrics,
        blueprints,
        misconceptions,
        issues,
    )
    if parsed_concepts.complete:
        _validate_relations(concepts, parsed_prerequisites, issues)
    _validate_misconceptions(
        concepts,
        parsed_misconceptions,
        parsed_items,
        issues,
        concept_complete=parsed_concepts.complete,
        misconception_complete=parsed_misconceptions.complete,
    )
    _validate_items(
        parsed_items,
        rubrics,
        issues,
        rubric_complete=parsed_rubrics.complete,
    )
    _validate_rubrics(parsed_rubrics, issues)
    if (
        parsed_concepts.complete
        and parsed_items.complete
        and parsed_q_matrix.complete
    ):
        _validate_q_matrix(concepts, items, q_matrix, issues)

    chunk_ids = (
        {chunk.chunk_id for chunk in validated_package.content_chunks}
        if validated_package is not None
        else set()
    )
    concept_evidence = _validate_evidence(
        concept_payload=concept_payload,
        concepts=parsed_concepts,
        items=parsed_items,
        rubrics=parsed_rubrics,
        chunk_ids=chunk_ids,
        issues=issues,
    )
    _validate_blueprint_semantics(
        course_package=validated_package,
        concepts=concepts,
        blueprints=parsed_blueprints,
        concept_complete=parsed_concepts.complete,
        issues=issues,
    )

    candidate: KnowledgeBundle | None = None
    roles_complete = all(
        result.complete
        for result in (
            parsed_concepts,
            parsed_items,
            parsed_rubrics,
            parsed_blueprints,
            parsed_prerequisites,
            parsed_misconceptions,
            parsed_q_matrix,
        )
    )
    if (
        not issues
        and roles_complete
        and concept_payload is not None
        and validated_package is not None
    ):
        candidate = _build_candidate(
            concept_payload=concept_payload,
            package=validated_package,
            concepts=concepts,
            prerequisites=prerequisites,
            misconceptions=misconceptions,
            items=items,
            rubrics=rubrics,
            blueprints=blueprints,
            q_matrix=q_matrix,
            concept_evidence=concept_evidence,
            issues=issues,
        )

    if candidate is not None and not issues:
        for blueprint_index, blueprint in enumerate(candidate.blueprints):
            try:
                select_blueprint_items(candidate, blueprint)
            except BlueprintSelectionError:
                _add_issue(
                    issues,
                    "BLUEPRINT_UNSATISFIABLE",
                    "blueprint",
                    f"blueprint[{blueprint_index}]",
                    "sections",
                )

    if candidate is not None and not issues:
        _run_schema_validator(candidate, schema_validator, issues)

    normalized_issues = tuple(sorted(issues))
    if normalized_issues:
        candidate = None
        report = create_validation_report(
            course_package_id=_safe_package_id(course_package),
            course_package_checksum=package_checksum,
            seed_snapshot=snapshot,
            issues=normalized_issues,
        )
    else:
        assert candidate is not None
        bundle_checksum = candidate.content_checksum()
        report = create_validation_report(
            course_package_id=candidate.course_package_id,
            course_package_checksum=candidate.course_package_checksum or package_checksum,
            seed_snapshot=snapshot,
            issues=(),
            knowledge_bundle_id=candidate.knowledge_bundle_id,
            bundle_version=candidate.bundle_version,
            bundle_checksum=bundle_checksum,
        )
    return M3ValidationOutcome(bundle=candidate, report=report)


def _validate_course_package(
    course_package: CoursePackage,
    issues: set[M3ValidationIssue],
) -> tuple[CoursePackage | None, str]:
    checksum = _fallback_package_checksum(course_package)
    try:
        rebuilt = CoursePackage.model_validate(
            course_package.model_dump(mode="python")
        )
        recalculated = rebuilt.recalculate_checksum()
        checksum = rebuilt.checksum if _SHA256_RE.fullmatch(rebuilt.checksum) else recalculated
    except Exception:
        _add_issue(
            issues,
            "COURSE_PACKAGE_INVALID",
            "concept",
            "",
            "course_package_id",
        )
        return None, checksum
    if rebuilt.status != "ready":
        _add_issue(
            issues,
            "COURSE_PACKAGE_NOT_READY",
            "concept",
            "",
            "status",
        )
    if rebuilt.checksum != recalculated:
        _add_issue(
            issues,
            "COURSE_PACKAGE_CHECKSUM_INVALID",
            "concept",
            "",
            "course_package_checksum",
        )
    return rebuilt, checksum


def _fallback_package_checksum(course_package: object) -> str:
    value = getattr(course_package, "checksum", None)
    if isinstance(value, str) and _SHA256_RE.fullmatch(value):
        return value
    try:
        recalculated = course_package.recalculate_checksum()  # type: ignore[attr-defined]
    except Exception:
        return "0" * 64
    return recalculated if _SHA256_RE.fullmatch(recalculated) else "0" * 64


def _safe_package_id(course_package: object) -> str:
    value = getattr(course_package, "course_package_id", None)
    if (
        isinstance(value, str)
        and value
        and "\x00" not in value
        and not _has_lone_surrogate(value)
    ):
        return value
    return "invalid_course_package"


def _load_role_payloads(
    snapshot: M3SeedSnapshot,
    issues: set[M3ValidationIssue],
) -> dict[str, dict[str, Any] | None]:
    payloads: dict[str, dict[str, Any] | None] = {}
    try:
        roles = snapshot.roles
    except Exception:
        roles = ()
    by_role = {role.role: role for role in roles if getattr(role, "role", None) in ROLE_ORDER}
    for role_name in ROLE_ORDER:
        role = by_role.get(role_name)
        if role is None or role.state == "invalid":
            raw_code = role.issue_code if role is not None else None
            code = (
                raw_code
                if raw_code in _SAFE_SNAPSHOT_ISSUE_CODES
                else "SEED_SCHEMA_INVALID"
            )
            _add_issue(
                issues,
                code,
                role_name,
                "",
                _ROLE_ROOT_FIELD[role_name],
            )
            payloads[role_name] = None
            continue
        try:
            payloads[role_name] = role_payload(snapshot, role_name)
        except Exception:
            _add_issue(
                issues,
                "SEED_SCHEMA_INVALID",
                role_name,
                "",
                _ROLE_ROOT_FIELD[role_name],
            )
            payloads[role_name] = None
    return payloads


def _validate_package_binding(
    concept_payload: Mapping[str, Any],
    package: CoursePackage,
    issues: set[M3ValidationIssue],
) -> None:
    expected = {
        "course_id": package.course_id,
        "course_package_id": package.course_package_id,
        "course_package_checksum": package.checksum,
    }
    for field, value in expected.items():
        if concept_payload.get(field) != value:
            _add_issue(
                issues,
                "COURSE_BINDING_MISMATCH",
                "concept",
                "",
                field,
            )


def _parse_models(
    payloads: Mapping[str, dict[str, Any] | None],
    role: str,
    model_type: type[_ModelT],
    issues: set[M3ValidationIssue],
) -> _ParsedCollection:
    payload = payloads.get(role)
    if payload is None:
        return _ParsedCollection(entries=(), complete=False)
    key = _MODEL_KEYS[role]
    values = payload.get(key)
    if type(values) is not list:
        _add_issue(issues, "SEED_SCHEMA_INVALID", role, "", key)
        return _ParsedCollection(entries=(), complete=False)
    parsed: list[_ParsedEntry] = []
    complete = True
    for index, value in enumerate(values):
        entity_key = f"{role}[{index}]"
        if type(value) is not dict:
            _add_issue(issues, _MODEL_INVALID_CODE[role], role, entity_key, key)
            complete = False
            continue
        try:
            parsed.append(
                _ParsedEntry(
                    source_index=index,
                    value=model_type.model_validate(value),  # type: ignore[attr-defined]
                )
            )
        except DomainError as error:
            code = error.code if error.code in {
                "SUBJECTIVE_RUBRIC_REQUIRED",
                "RUBRIC_TOTAL_MISMATCH",
            } else _MODEL_INVALID_CODE[role]
            field = "rubric_id" if code == "SUBJECTIVE_RUBRIC_REQUIRED" else key
            _add_issue(issues, code, role, entity_key, field)
            complete = False
        except Exception:
            _add_issue(issues, _MODEL_INVALID_CODE[role], role, entity_key, key)
            complete = False
    return _ParsedCollection(entries=tuple(parsed), complete=complete)


def _parse_q_matrix(
    payloads: Mapping[str, dict[str, Any] | None],
    issues: set[M3ValidationIssue],
) -> _ParsedCollection:
    payload = payloads.get("item")
    if payload is None:
        return _ParsedCollection(entries=(), complete=False)
    values = payload.get("q_matrix")
    if type(values) is not list:
        _add_issue(issues, "SEED_SCHEMA_INVALID", "item", "", "q_matrix")
        return _ParsedCollection(entries=(), complete=False)
    parsed: list[_ParsedEntry] = []
    complete = True
    for index, value in enumerate(values):
        if type(value) is not dict:
            _add_issue(issues, "Q_MATRIX_CONFLICT", "item", "", "q_matrix")
            complete = False
            continue
        try:
            parsed.append(
                _ParsedEntry(
                    source_index=index,
                    value=QMatrixEntry.model_validate(value),
                )
            )
        except Exception:
            _add_issue(issues, "Q_MATRIX_CONFLICT", "item", "", "q_matrix")
            complete = False
    return _ParsedCollection(entries=tuple(parsed), complete=complete)


def _validate_statuses(
    concepts: _ParsedCollection,
    items: _ParsedCollection,
    rubrics: _ParsedCollection,
    blueprints: _ParsedCollection,
    issues: set[M3ValidationIssue],
) -> None:
    rows = (
        ("concept", concepts, "published"),
        ("item", items, "teacher_approved"),
        ("rubric", rubrics, "published"),
        ("blueprint", blueprints, "teacher_approved"),
    )
    for role, parsed, expected in rows:
        for entry in parsed.entries:
            entity = entry.value
            if _fold_status(entity.status) != expected:
                _add_issue(
                    issues,
                    f"{role.upper()}_STATUS_INVALID",
                    role,
                    f"{role}[{entry.source_index}]",
                    "status",
                )


def _fold_status(value: str) -> str:
    return " ".join(value.split()).casefold()


def _comparison_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _validate_concept_names(
    concepts: _ParsedCollection,
    issues: set[M3ValidationIssue],
) -> None:
    seen: set[str] = set()
    for entry in concepts.entries:
        concept = cast(KnowledgeConcept, entry.value)
        entity = f"concept[{entry.source_index}]"
        name_key = _comparison_key(concept.name)
        if not name_key or name_key in seen:
            _add_issue(
                issues, "CONCEPT_NAME_COLLISION", "concept", entity, "name"
            )
        if name_key:
            seen.add(name_key)
        for alias in concept.aliases:
            alias_key = _comparison_key(alias)
            if not alias_key:
                _add_issue(
                    issues, "CONCEPT_ALIAS_INVALID", "concept", entity, "aliases"
                )
                continue
            if alias_key in seen:
                _add_issue(
                    issues,
                    "CONCEPT_NAME_COLLISION",
                    "concept",
                    entity,
                    "aliases",
                )
            seen.add(alias_key)


def _validate_identifiers(
    concepts: list[KnowledgeConcept],
    items: list[ItemCard],
    rubrics: list[Rubric],
    blueprints: list[AssessmentBlueprint],
    misconceptions: list[MisconceptionTag],
    issues: set[M3ValidationIssue],
) -> None:
    duplicate_rows = (
        (
            [concept.concept_id for concept in concepts],
            "CONCEPT_IDENTIFIER_DUPLICATE",
            "concept",
            "concept_id",
        ),
        (
            [(item.item_id, item.version) for item in items],
            "ITEM_VERSION_DUPLICATE",
            "item",
            "version",
        ),
        (
            [rubric.rubric_id for rubric in rubrics],
            "RUBRIC_IDENTIFIER_DUPLICATE",
            "rubric",
            "rubric_id",
        ),
        (
            [blueprint.blueprint_id for blueprint in blueprints],
            "BLUEPRINT_IDENTIFIER_DUPLICATE",
            "blueprint",
            "blueprint_id",
        ),
        (
            [tag.misconception_id for tag in misconceptions],
            "MISCONCEPTION_IDENTIFIER_DUPLICATE",
            "misconception",
            "misconception_id",
        ),
    )
    for values, code, role, field in duplicate_rows:
        _reject_duplicate_values(values, code, role, field, issues)

    approved_by_id: dict[str, int] = {}
    for item in items:
        if _fold_status(item.status) == "teacher_approved":
            approved_by_id[item.item_id] = approved_by_id.get(item.item_id, 0) + 1
    if any(count > 1 for count in approved_by_id.values()):
        _add_issue(
            issues,
            "ITEM_MULTIPLE_APPROVED_VERSIONS",
            "item",
            "",
            "version",
        )


def _reject_duplicate_values(
    values: list[Any],
    code: str,
    role: str,
    field: str,
    issues: set[M3ValidationIssue],
) -> None:
    try:
        duplicated = len(values) != len(set(values))
    except TypeError:
        duplicated = True
    if duplicated:
        _add_issue(issues, code, role, "", field)


def _validate_relations(
    concepts: list[KnowledgeConcept],
    relations: _ParsedCollection,
    issues: set[M3ValidationIssue],
) -> None:
    concept_ids = {concept.concept_id for concept in concepts}
    graph: dict[str, set[str]] = {}
    for entry in relations.entries:
        relation = cast(PrerequisiteRelation, entry.value)
        entity = f"prerequisite[{entry.source_index}]"
        if relation.from_concept_id not in concept_ids:
            _add_issue(
                issues,
                "PREREQUISITE_REFERENCE_MISSING",
                "prerequisite",
                entity,
                "from_concept_id",
            )
        if relation.to_concept_id not in concept_ids:
            _add_issue(
                issues,
                "PREREQUISITE_REFERENCE_MISSING",
                "prerequisite",
                entity,
                "to_concept_id",
            )
        if (
            relation.relation_type == "prerequisite"
            and relation.from_concept_id in concept_ids
            and relation.to_concept_id in concept_ids
        ):
            graph.setdefault(relation.from_concept_id, set()).add(
                relation.to_concept_id
            )
    if _has_directed_cycle(graph):
        _add_issue(
            issues,
            "PREREQUISITE_CYCLE",
            "prerequisite",
            "",
            "prerequisite_relations",
        )


def _has_directed_cycle(graph: Mapping[str, set[str]]) -> bool:
    nodes = set(graph)
    for neighbors in graph.values():
        nodes.update(neighbors)
    indegree = {node: 0 for node in nodes}
    for neighbors in graph.values():
        for neighbor in neighbors:
            indegree[neighbor] += 1

    ready = [node for node, degree in indegree.items() if degree == 0]
    heapify(ready)
    processed = 0
    while ready:
        node = heappop(ready)
        processed += 1
        for neighbor in sorted(graph.get(node, set())):
            indegree[neighbor] -= 1
            if indegree[neighbor] == 0:
                heappush(ready, neighbor)
    return processed != len(nodes)


def _validate_misconceptions(
    concepts: list[KnowledgeConcept],
    misconceptions: _ParsedCollection,
    items: _ParsedCollection,
    issues: set[M3ValidationIssue],
    *,
    concept_complete: bool,
    misconception_complete: bool,
) -> None:
    concept_ids = {concept.concept_id for concept in concepts}
    misconception_ids = {
        cast(MisconceptionTag, entry.value).misconception_id
        for entry in misconceptions.entries
    }
    for entry in misconceptions.entries:
        tag = cast(MisconceptionTag, entry.value)
        if concept_complete and not set(tag.concept_ids) <= concept_ids:
            _add_issue(
                issues,
                "MISCONCEPTION_REFERENCE_MISSING",
                "misconception",
                f"misconception[{entry.source_index}]",
                "concept_ids",
            )
    for entry in items.entries:
        item = cast(ItemCard, entry.value)
        entity = f"item[{entry.source_index}]"
        if (
            misconception_complete
            and not set(item.misconception_ids) <= misconception_ids
        ):
            _add_issue(
                issues,
                "ITEM_MISCONCEPTION_REFERENCE_MISSING",
                "item",
                entity,
                "misconception_ids",
            )
        if concept_complete and not set(item.concept_ids) <= concept_ids:
            _add_issue(
                issues,
                "ITEM_CONCEPT_REFERENCE_MISSING",
                "item",
                entity,
                "concept_ids",
            )


def _validate_items(
    items: _ParsedCollection,
    rubrics: list[Rubric],
    issues: set[M3ValidationIssue],
    *,
    rubric_complete: bool,
) -> None:
    rubric_ids = {rubric.rubric_id for rubric in rubrics}
    for entry in items.entries:
        item = cast(ItemCard, entry.value)
        entity = f"item[{entry.source_index}]"
        if not item.is_objective() and item.rubric_id is None:
            _add_issue(
                issues,
                "SUBJECTIVE_RUBRIC_REQUIRED",
                "item",
                entity,
                "rubric_id",
            )
        if (
            rubric_complete
            and item.rubric_id is not None
            and item.rubric_id not in rubric_ids
        ):
            _add_issue(
                issues,
                "ITEM_RUBRIC_REFERENCE_MISSING",
                "item",
                entity,
                "rubric_id",
            )


def _validate_rubrics(
    rubrics: _ParsedCollection,
    issues: set[M3ValidationIssue],
) -> None:
    for entry in rubrics.entries:
        rubric = cast(Rubric, entry.value)
        rubric_index = entry.source_index
        if not rubric.criteria or rubric.total_score <= 0.0:
            _add_issue(
                issues,
                "RUBRIC_CRITERIA_INVALID",
                "rubric",
                f"rubric[{rubric_index}]",
                "criteria",
            )
        for criterion_index, criterion in enumerate(rubric.criteria):
            if criterion.max_score <= 0.0:
                _add_issue(
                    issues,
                    "RUBRIC_CRITERIA_INVALID",
                    "rubric",
                    f"rubric[{rubric_index}].criteria[{criterion_index}]",
                    "max_score",
                )


def _validate_q_matrix(
    concepts: list[KnowledgeConcept],
    items: list[ItemCard],
    q_matrix: list[QMatrixEntry],
    issues: set[M3ValidationIssue],
) -> None:
    concept_ids = {concept.concept_id for concept in concepts}
    declared = {
        (item.item_id, item.version, concept_id)
        for item in items
        for concept_id in item.concept_ids
    }
    stored = [
        (entry.item_id, entry.item_version, entry.concept_id)
        for entry in q_matrix
    ]
    actual = {
        triple
        for triple, entry in zip(stored, q_matrix, strict=True)
        if entry.weight > 0.0
    }
    invalid = (
        any(entry.weight <= 0.0 for entry in q_matrix)
        or len(stored) != len(set(stored))
        or actual != declared
        or any(entry.concept_id not in concept_ids for entry in q_matrix)
    )
    if invalid:
        _add_issue(issues, "Q_MATRIX_CONFLICT", "item", "", "q_matrix")


def _validate_evidence(
    *,
    concept_payload: Mapping[str, Any] | None,
    concepts: _ParsedCollection,
    items: _ParsedCollection,
    rubrics: _ParsedCollection,
    chunk_ids: set[str],
    issues: set[M3ValidationIssue],
) -> dict[str, list[str]]:
    concept_map_value = (
        concept_payload.get("concept_evidence_ids")
        if concept_payload is not None
        else None
    )
    concept_map = concept_map_value if type(concept_map_value) is dict else {}
    concept_ids = {
        cast(KnowledgeConcept, entry.value).concept_id
        for entry in concepts.entries
    }
    if concepts.complete and set(concept_map) != concept_ids:
        _add_issue(
            issues,
            "CONCEPT_EVIDENCE_INVALID",
            "concept",
            "",
            "concept_evidence_ids",
        )
    normalized_map: dict[str, list[str]] = {}
    for entry in concepts.entries:
        concept = cast(KnowledgeConcept, entry.value)
        value = concept_map.get(concept.concept_id)
        if not _evidence_list_is_valid(value, chunk_ids):
            _add_issue(
                issues,
                "CONCEPT_EVIDENCE_INVALID",
                "concept",
                f"concept[{entry.source_index}]",
                "concept_evidence_ids",
            )
        elif type(value) is list:
            normalized_map[concept.concept_id] = list(value)
    for entry in items.entries:
        item = cast(ItemCard, entry.value)
        if not _evidence_list_is_valid(item.source_evidence_ids, chunk_ids):
            _add_issue(
                issues,
                "ITEM_EVIDENCE_INVALID",
                "item",
                f"item[{entry.source_index}]",
                "source_evidence_ids",
            )
    for entry in rubrics.entries:
        rubric = cast(Rubric, entry.value)
        for criterion_index, criterion in enumerate(rubric.criteria):
            if not _evidence_list_is_valid(criterion.course_evidence_ids, chunk_ids):
                _add_issue(
                    issues,
                    "RUBRIC_EVIDENCE_INVALID",
                    "rubric",
                    f"rubric[{entry.source_index}].criteria[{criterion_index}]",
                    "course_evidence_ids",
                )
    return normalized_map


def _evidence_list_is_valid(value: object, chunk_ids: set[str]) -> bool:
    if type(value) is not list or not value:
        return False
    if any(not isinstance(item, str) for item in value):
        return False
    if len(value) != len(set(value)):
        return False
    for evidence_id in value:
        try:
            chunk_id = chunk_id_for_evidence_id(evidence_id)
        except Exception:
            return False
        if chunk_id not in chunk_ids:
            return False
    return True


def _validate_blueprint_semantics(
    *,
    course_package: CoursePackage | None,
    concepts: list[KnowledgeConcept],
    blueprints: _ParsedCollection,
    concept_complete: bool,
    issues: set[M3ValidationIssue],
) -> None:
    if course_package is None:
        return
    concept_ids = {concept.concept_id for concept in concepts}
    for entry in blueprints.entries:
        blueprint = cast(AssessmentBlueprint, entry.value)
        entity = f"blueprint[{entry.source_index}]"
        if blueprint.course_id != course_package.course_id:
            _add_issue(
                issues, "BLUEPRINT_COURSE_INVALID", "blueprint", entity, "course_id"
            )
        for section in blueprint.sections:
            if concept_complete and not set(section.concept_weights) <= concept_ids:
                _add_issue(
                    issues,
                    "BLUEPRINT_CONCEPT_INVALID",
                    "blueprint",
                    entity,
                    "concept_weights",
                )
            if (
                section.anchor_item_ids
                and (
                    not section.anchor_item_versions
                    or set(section.anchor_item_versions)
                    != set(section.anchor_item_ids)
                )
            ):
                _add_issue(
                    issues,
                    "ANCHOR_ITEM_VERSION_INVALID",
                    "blueprint",
                    entity,
                    "anchor_item_versions",
                )


def _build_candidate(
    *,
    concept_payload: Mapping[str, Any],
    package: CoursePackage,
    concepts: list[KnowledgeConcept],
    prerequisites: list[PrerequisiteRelation],
    misconceptions: list[MisconceptionTag],
    items: list[ItemCard],
    rubrics: list[Rubric],
    blueprints: list[AssessmentBlueprint],
    q_matrix: list[QMatrixEntry],
    concept_evidence: dict[str, list[str]],
    issues: set[M3ValidationIssue],
) -> KnowledgeBundle | None:
    try:
        return KnowledgeBundle.model_validate(
            {
                "knowledge_bundle_id": concept_payload.get("knowledge_bundle_id"),
                "course_package_id": package.course_package_id,
                "course_id": package.course_id,
                "bundle_version": concept_payload.get("bundle_version"),
                "concepts": concepts,
                "prerequisite_relations": prerequisites,
                "misconception_tags": misconceptions,
                "items": items,
                "rubrics": rubrics,
                "blueprints": blueprints,
                "q_matrix": q_matrix,
                "status": "published",
                "published_at": concept_payload.get("published_at"),
                "course_package_checksum": package.checksum,
                "concept_evidence_ids": concept_evidence,
            }
        )
    except Exception:
        _add_issue(
            issues, "KNOWLEDGE_BUNDLE_INVALID", "concept", "", "concepts"
        )
        return None


def _run_schema_validator(
    bundle: KnowledgeBundle,
    schema_validator: Callable[[KnowledgeBundle], bool] | None,
    issues: set[M3ValidationIssue],
) -> None:
    if schema_validator is None:
        return
    if not callable(schema_validator):
        _add_issue(
            issues, "SCHEMA_VALIDATOR_ERROR", "concept", "", "concepts"
        )
        return
    try:
        result = schema_validator(bundle.model_copy(deep=True))
    except Exception:
        _add_issue(
            issues, "SCHEMA_VALIDATOR_ERROR", "concept", "", "concepts"
        )
        return
    if type(result) is not bool:
        _add_issue(
            issues, "SCHEMA_VALIDATOR_ERROR", "concept", "", "concepts"
        )
    elif result is False:
        _add_issue(
            issues, "SCHEMA_VALIDATOR_REJECTED", "concept", "", "concepts"
        )


def _add_issue(
    issues: set[M3ValidationIssue],
    code: str,
    seed_role: str,
    entity_key: str,
    field: str,
) -> None:
    issues.add(
        M3ValidationIssue(
            code=code,
            seed_role=seed_role,
            entity_key=entity_key,
            field=field,
        )
    )


def _has_lone_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


__all__ = ["M3ValidationOutcome", "validate_seed_snapshot"]
