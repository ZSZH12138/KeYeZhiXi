"""Compatibility boundaries for additive M3 package/evidence/version bindings."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import (
    AssessmentBlueprint,
    BlueprintSection,
    KnowledgeBundle,
    KnowledgeConcept,
)


NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)


def _legacy_section() -> BlueprintSection:
    return BlueprintSection(
        section_id="section_1",
        name="Legacy section",
        item_count=1,
        score=1.0,
        item_types=["true_false"],
        concept_weights={"concept_1": 1.0},
        difficulty_range=(1, 1),
        anchor_item_ids=["item_1"],
    )


def _legacy_blueprint() -> AssessmentBlueprint:
    return AssessmentBlueprint(
        blueprint_id="blueprint_1",
        version="1.0.0",
        course_id="course_1",
        sections=[_legacy_section()],
        total_score=1.0,
        duration_minutes=30,
        status="teacher_approved",
    )


def _legacy_bundle() -> KnowledgeBundle:
    return KnowledgeBundle(
        knowledge_bundle_id="bundle_1",
        course_package_id="package_1",
        course_id="course_1",
        bundle_version="1.0.0",
        concepts=[
            KnowledgeConcept(
                concept_id="concept_1",
                name="Concept one",
                chapter_id="chapter_1",
                description="Legacy concept.",
                aliases=[],
                status="published",
            )
        ],
        prerequisite_relations=[],
        misconception_tags=[],
        items=[],
        rubrics=[],
        blueprints=[],
        q_matrix=[],
        status="published",
        published_at=NOW,
    )


def test_legacy_knowledge_defaults_do_not_change_serialization_or_checksum() -> None:
    legacy = _legacy_bundle()

    assert legacy.to_dict() == {
        "knowledge_bundle_id": "bundle_1",
        "course_package_id": "package_1",
        "course_id": "course_1",
        "bundle_version": "1.0.0",
        "concepts": [
            {
                "concept_id": "concept_1",
                "name": "Concept one",
                "chapter_id": "chapter_1",
                "description": "Legacy concept.",
                "aliases": [],
                "status": "published",
                "schema_version": "1.0.0",
            }
        ],
        "prerequisite_relations": [],
        "misconception_tags": [],
        "items": [],
        "rubrics": [],
        "blueprints": [],
        "q_matrix": [],
        "status": "published",
        "published_at": "2026-08-03T00:00:00Z",
        "schema_version": "1.0.0",
    }
    assert (
        legacy.content_checksum()
        == "7a3641630659d8d32172952634bd7cd90e89e00f57d3b896a510b35e4197888c"
    )


def test_legacy_blueprint_defaults_do_not_change_serialization_or_checksum() -> None:
    legacy = _legacy_blueprint()

    assert legacy.to_dict() == {
        "blueprint_id": "blueprint_1",
        "version": "1.0.0",
        "course_id": "course_1",
        "sections": [
            {
                "section_id": "section_1",
                "name": "Legacy section",
                "item_count": 1,
                "score": 1.0,
                "item_types": ["true_false"],
                "concept_weights": {"concept_1": 1.0},
                "difficulty_range": [1, 1],
                "anchor_item_ids": ["item_1"],
                "schema_version": "1.0.0",
            }
        ],
        "total_score": 1.0,
        "duration_minutes": 30,
        "status": "teacher_approved",
        "schema_version": "1.0.0",
    }
    assert (
        legacy.content_checksum()
        == "25d6030123da06bec1af3564c0cbf550eaf7aa6bad66b28cfea5ee441bc88d21"
    )


def test_new_knowledge_bindings_are_serialized_and_checksummed() -> None:
    legacy = _legacy_bundle()
    bound = legacy.model_copy(
        update={
            "course_package_checksum": "a" * 64,
            "concept_evidence_ids": {"concept_1": ["evidence_chunk_1"]},
        }
    )

    assert bound.to_dict()["course_package_checksum"] == "a" * 64
    assert bound.to_dict()["concept_evidence_ids"] == {
        "concept_1": ["evidence_chunk_1"]
    }
    assert bound.content_checksum() != legacy.content_checksum()


def test_new_anchor_versions_are_serialized_and_checksummed() -> None:
    legacy = _legacy_blueprint()
    bound = legacy.model_copy(
        update={
            "sections": [
                _legacy_section().model_copy(
                    update={"anchor_item_versions": {"item_1": "1.0.0"}}
                )
            ]
        }
    )

    assert bound.to_dict()["sections"][0]["anchor_item_versions"] == {
        "item_1": "1.0.0"
    }
    assert bound.content_checksum() != legacy.content_checksum()


@pytest.mark.parametrize(
    ("anchor_item_versions", "reason"),
    [
        ({"other_item": "1.0.0"}, "keys must match"),
        ({"item_1": ""}, "versions must be nonblank"),
    ],
)
def test_anchor_versions_revalidate_model_copy_assignment_and_in_place_mutation(
    anchor_item_versions: dict[str, str],
    reason: str,
) -> None:
    corrupted = _legacy_section().model_copy(
        update={"anchor_item_versions": anchor_item_versions}
    )

    with pytest.raises(DomainError, match=reason):
        BlueprintSection.model_validate(corrupted)
    with pytest.raises(DomainError, match=reason):
        corrupted.content_checksum()
    with pytest.raises(DomainError, match=reason):
        corrupted.to_dict()

    corrupted_blueprint = _legacy_blueprint().model_copy(
        update={"sections": [corrupted]}
    )
    with pytest.raises(DomainError, match=reason):
        AssessmentBlueprint.model_validate(corrupted_blueprint)
    with pytest.raises(DomainError, match=reason):
        corrupted_blueprint.content_checksum()
    with pytest.raises(DomainError, match=reason):
        corrupted_blueprint.to_dict()

    in_place_blueprint = _legacy_blueprint()
    in_place_blueprint.sections[0].anchor_item_versions.update(
        anchor_item_versions
    )
    with pytest.raises(DomainError, match=reason):
        in_place_blueprint.to_dict()

    assigned = _legacy_section()
    with pytest.raises(DomainError, match=reason):
        assigned.anchor_item_versions = anchor_item_versions

    in_place = _legacy_section().model_copy(
        update={"anchor_item_versions": {"item_1": "1.0.0"}}
    )
    in_place.anchor_item_versions.clear()
    in_place.anchor_item_versions.update(anchor_item_versions)
    with pytest.raises(DomainError, match=reason):
        in_place.content_checksum()


@pytest.mark.parametrize(
    ("concept_evidence_ids", "reason"),
    [
        ({"other_concept": ["evidence_chunk_1"]}, "keys must match"),
        ({"concept_1": []}, "lists must be non-empty"),
        ({"concept_1": ["evidence_chunk_1", "evidence_chunk_1"]}, "unique"),
        ({"concept_1": [" "]}, "nonblank"),
    ],
)
def test_concept_evidence_revalidates_model_copy_assignment_and_in_place_mutation(
    concept_evidence_ids: dict[str, list[str]],
    reason: str,
) -> None:
    corrupted = _legacy_bundle().model_copy(
        update={"concept_evidence_ids": concept_evidence_ids}
    )

    with pytest.raises(DomainError, match=reason):
        KnowledgeBundle.model_validate(corrupted)
    with pytest.raises(DomainError, match=reason):
        corrupted.content_checksum()
    with pytest.raises(DomainError, match=reason):
        corrupted.to_dict()

    assigned = _legacy_bundle()
    with pytest.raises(DomainError, match=reason):
        assigned.concept_evidence_ids = concept_evidence_ids

    in_place = _legacy_bundle().model_copy(
        update={"concept_evidence_ids": {"concept_1": ["evidence_chunk_1"]}}
    )
    in_place.concept_evidence_ids.clear()
    in_place.concept_evidence_ids.update(concept_evidence_ids)
    with pytest.raises(DomainError, match=reason):
        in_place.content_checksum()
    with pytest.raises(DomainError, match=reason):
        in_place.to_dict()
