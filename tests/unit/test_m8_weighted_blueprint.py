"""M8 paper generation must honor declared concept proportions."""

from datetime import datetime, timezone

from course_insight.contracts.knowledge import (
    AssessmentBlueprint,
    BlueprintSection,
    ItemCard,
    KnowledgeBundle,
    KnowledgeConcept,
    QMatrixEntry,
)
from course_insight.modules.m8_assessment_scoring.paper_generator import (
    PaperGenerator,
    allocate_concept_targets,
)
from tests.factories.m5_m8 import make_task_plan


def _weighted_bundle() -> KnowledgeBundle:
    items = [
        ItemCard(
            item_id=f"item_c1_{index}",
            version="1.0.0",
            stem=f"C1 question {index}",
            item_type="multiple_choice",
            concept_ids=["c1"],
            misconception_ids=[],
            difficulty_level=2,
            cognitive_level="apply",
            parameter_rules=[],
            answer_key={"answer": "yes", "max_score": 1.0},
            rubric_id=None,
            source_evidence_ids=["evidence_1"],
            status="teacher_approved",
        )
        for index in range(9)
    ] + [
        ItemCard(
            item_id=f"item_c2_{index}",
            version="1.0.0",
            stem=f"C2 question {index}",
            item_type="multiple_choice",
            concept_ids=["c2"],
            misconception_ids=[],
            difficulty_level=2,
            cognitive_level="apply",
            parameter_rules=[],
            answer_key={"answer": "yes", "max_score": 1.0},
            rubric_id=None,
            source_evidence_ids=["evidence_1"],
            status="teacher_approved",
        )
        for index in range(9)
    ]
    section = BlueprintSection(
        section_id="section_1",
        name="Weighted section",
        item_count=10,
        score=10.0,
        item_types=["multiple_choice"],
        concept_weights={"c1": 0.1, "c2": 0.9},
        difficulty_range=(1, 3),
        anchor_item_ids=[],
    )
    return KnowledgeBundle(
        knowledge_bundle_id="kb_1",
        course_package_id="cp_1",
        course_id="course_1",
        bundle_version="1.0.0",
        concepts=[
            KnowledgeConcept(
                concept_id=concept_id,
                name=concept_id.upper(),
                chapter_id="chapter_1",
                description="Description",
                aliases=[],
                status="published",
            )
            for concept_id in ("c1", "c2")
        ],
        prerequisite_relations=[],
        misconception_tags=[],
        items=items,
        rubrics=[],
        blueprints=[
            AssessmentBlueprint(
                blueprint_id="blueprint_1",
                version="1.0.0",
                course_id="course_1",
                sections=[section],
                total_score=10.0,
                duration_minutes=30,
                status="teacher_approved",
            )
        ],
        q_matrix=[
            QMatrixEntry(
                item_id=item.item_id,
                item_version=item.version,
                concept_id=item.concept_ids[0],
                weight=1.0,
            )
            for item in items
        ],
        status="published",
        published_at=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )


def test_largest_remainder_allocates_ten_ninety_quota() -> None:
    assert allocate_concept_targets({"c1": 0.1, "c2": 0.9}, 10) == {
        "c1": 1,
        "c2": 9,
    }


def test_ten_items_follow_ten_ninety_concept_weights() -> None:
    paper = PaperGenerator().generate(
        make_task_plan(),
        _weighted_bundle(),
        None,
        None,
    )
    counts = {"c1": 0, "c2": 0}
    for item in paper.all_items():
        counts[item.concept_ids[0]] += 1

    assert counts == {"c1": 1, "c2": 9}
