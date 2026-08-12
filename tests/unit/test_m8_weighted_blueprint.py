"""M8 paper generation must honor declared concept proportions."""

import math
from datetime import datetime, timezone

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import (
    AssessmentBlueprint,
    BlueprintSection,
    ItemCard,
    KnowledgeBundle,
    KnowledgeConcept,
    ParameterRule,
    QMatrixEntry,
)
from course_insight.contracts.state import LearnerStateSnapshot
from course_insight.modules.m8_assessment_scoring.stubs import FixedClock
from course_insight.modules.m8_assessment_scoring.paper_generator import (
    PaperGenerator,
    allocate_concept_targets,
)
from tests.factories.m5_m8 import (
    UTC_TIME,
    make_knowledge_bundle,
    make_task_plan,
)


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


@pytest.mark.parametrize(
    ("weights", "item_count", "message"),
    [
        ({"c1": 1.0}, -1, "item count"),
        ({"c1": 0.4, "c2": 0.4}, 10, "sum to one"),
        ({"c1": math.nan}, 1, "finite"),
    ],
)
def test_concept_quota_allocation_rejects_invalid_inputs(
    weights: dict[str, float],
    item_count: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        allocate_concept_targets(weights, item_count)


def test_empty_concept_weights_require_no_quotas() -> None:
    assert allocate_concept_targets({}, 3) == {}
    assert allocate_concept_targets({"c1": 0.5, "c2": 0.5}, 1) == {
        "c1": 1,
        "c2": 0,
    }


def _with_section(
    bundle: KnowledgeBundle,
    section: BlueprintSection,
    *,
    status: str = "teacher_approved",
) -> KnowledgeBundle:
    blueprint = bundle.blueprints[0].model_copy(
        update={
            "sections": [section],
            "total_score": section.score,
            "status": status,
        }
    )
    return bundle.model_copy(update={"blueprints": [blueprint]})


def test_generator_requires_blueprint_and_approved_matching_scope() -> None:
    generator = PaperGenerator(FixedClock())
    bundle = make_knowledge_bundle(subjective=False)

    with pytest.raises(DomainError, match="blueprint is missing"):
        generator.generate(
            make_task_plan().model_copy(update={"blueprint_id": None}),
            bundle,
            None,
            None,
        )
    with pytest.raises(DomainError, match="not approved"):
        generator.generate(
            make_task_plan(),
            _with_section(
                bundle,
                bundle.blueprints[0].sections[0],
                status="draft",
            ),
            None,
            None,
        )


def test_generator_rejects_mismatched_learner_state() -> None:
    learner_state = LearnerStateSnapshot(
        snapshot_id="state_1",
        course_id="course_1",
        class_id="class_1",
        learner_id="learner_2",
        state_version=1,
        concept_states=[],
        overall_mastery=0.0,
        evidence_count=0,
        updated_at=UTC_TIME,
    )

    with pytest.raises(DomainError, match="learner state"):
        PaperGenerator(FixedClock()).generate(
            make_task_plan(),
            make_knowledge_bundle(subjective=False),
            learner_state,
            None,
        )


def test_generator_rejects_missing_or_excess_anchor_items() -> None:
    bundle = make_knowledge_bundle(subjective=False)
    section = bundle.blueprints[0].sections[0]
    generator = PaperGenerator(FixedClock())

    with pytest.raises(DomainError, match="anchor item is unavailable"):
        generator.generate(
            make_task_plan(),
            _with_section(
                bundle,
                section.model_copy(update={"anchor_item_ids": ["missing"]}),
            ),
            None,
            None,
        )
    with pytest.raises(DomainError, match="anchor count"):
        generator.generate(
            make_task_plan(),
            _with_section(
                bundle,
                section.model_copy(
                    update={"anchor_item_ids": ["item_2", "item_2"]}
                ),
            ),
            None,
            None,
        )


def test_generator_rejects_short_pool_score_mismatch_and_impossible_quota() -> None:
    bundle = make_knowledge_bundle(subjective=False)
    base = bundle.blueprints[0].sections[0]
    generator = PaperGenerator(FixedClock())

    short_pool = base.model_copy(
        update={
            "item_count": 2,
            "score": 2.0,
            "concept_weights": {},
        }
    )
    with pytest.raises(DomainError, match="not enough"):
        generator.generate(
            make_task_plan(),
            _with_section(bundle, short_pool),
            None,
            None,
        )

    wrong_score = base.model_copy(
        update={"score": 2.0, "concept_weights": {}}
    )
    with pytest.raises(DomainError, match="maxima"):
        generator.generate(
            make_task_plan(),
            _with_section(bundle, wrong_score),
            None,
            None,
        )

    impossible_quota = base.model_copy(
        update={
            "item_count": 2,
            "score": 2.0,
            "concept_weights": {"concept_2": 0.5, "missing": 0.5},
        }
    )
    with pytest.raises(DomainError, match="concept quotas"):
        generator.generate(
            make_task_plan(),
            _with_section(bundle, impossible_quota),
            None,
            None,
        )


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        (
            ParameterRule(
                name="choice",
                value_type="string",
                minimum=None,
                maximum=None,
                choices=["A"],
                constraints=[],
            ),
            "A",
        ),
        (
            ParameterRule(
                name="integer",
                value_type="integer",
                minimum=2,
                maximum=4,
                choices=[],
                constraints=[],
            ),
            2,
        ),
        (
            ParameterRule(
                name="number",
                value_type="number",
                minimum=1.5,
                maximum=4.0,
                choices=[],
                constraints=[],
            ),
            1.5,
        ),
        (
            ParameterRule(
                name="flag",
                value_type="boolean",
                minimum=None,
                maximum=None,
                choices=[],
                constraints=[],
            ),
            False,
        ),
        (
            ParameterRule(
                name="text",
                value_type="string",
                minimum=None,
                maximum=None,
                choices=[],
                constraints=[],
            ),
            "fixed",
        ),
    ],
)
def test_fixed_parameters_follow_each_approved_rule_type(
    rule: ParameterRule,
    expected: object,
) -> None:
    assert PaperGenerator._fixed_parameter(rule) == expected


def test_invalid_parameter_vocabulary_fails_closed() -> None:
    bundle = make_knowledge_bundle(subjective=False)
    invalid_rule = ParameterRule(
        name="unsupported",
        value_type="opaque",
        minimum=None,
        maximum=None,
        choices=[],
        constraints=[],
    )
    item = bundle.items[0].model_copy(
        update={"parameter_rules": [invalid_rule]}
    )
    invalid_bundle = bundle.model_copy(update={"items": [item]})

    with pytest.raises(DomainError, match="fixed parameter"):
        PaperGenerator(FixedClock()).generate(
            make_task_plan(),
            invalid_bundle,
            None,
            None,
        )


def test_explicit_clock_and_non_numeric_item_identity_are_preserved() -> None:
    bundle = make_knowledge_bundle(subjective=False)
    item = bundle.items[0].model_copy(update={"item_id": "item_alpha"})
    section = bundle.blueprints[0].sections[0].model_copy(
        update={"concept_weights": {}}
    )
    blueprint = bundle.blueprints[0].model_copy(update={"sections": [section]})
    q_entry = bundle.q_matrix[0].model_copy(update={"item_id": "item_alpha"})
    renamed = bundle.model_copy(
        update={
            "items": [item],
            "blueprints": [blueprint],
            "q_matrix": [q_entry],
        }
    )

    paper = PaperGenerator(FixedClock(UTC_TIME)).generate(
        make_task_plan(),
        renamed,
        None,
        None,
    )

    assert paper.generated_at == UTC_TIME
    assert paper.all_items()[0].item_instance_id == "item_alpha_instance"
