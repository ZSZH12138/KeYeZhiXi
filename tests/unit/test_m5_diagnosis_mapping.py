"""M5 item diagnoses must use frozen item/Q-matrix identities."""

from course_insight.contracts.assessment import (
    AssessmentPaper,
    ItemInstance,
    PaperSection,
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.knowledge import (
    ItemCard,
    KnowledgeBundle,
    KnowledgeConcept,
    QMatrixEntry,
)
from course_insight.modules.m5_learner_class_state.update_policy import (
    DeterministicStateUpdatePolicy,
)
from course_insight.modules.m8_assessment_scoring.observation_builder import (
    build_observation_batch,
)
from course_insight.modules.m8_assessment_scoring.paper_record import (
    FrozenAssessmentRecord,
)
from tests.factories.m5_m8 import make_knowledge_bundle, make_scoring_bundle


def _two_item_case() -> tuple[
    ScoringResultBundle,
    KnowledgeBundle,
    object,
]:
    base = make_knowledge_bundle(subjective=False)
    first_item = ItemCard(
        **{
            **base.items[0].model_dump(mode="python"),
            "item_id": "item_1",
            "concept_ids": ["concept_1"],
        }
    )
    second_item = base.items[0]
    knowledge = KnowledgeBundle(
        **{
            **base.model_dump(mode="python"),
            "concepts": [
                KnowledgeConcept(
                    concept_id="concept_1",
                    name="Concept 1",
                    chapter_id="chapter_1",
                    description="Description",
                    aliases=[],
                    status="published",
                ),
                *base.concepts,
            ],
            "items": [first_item, second_item],
            "q_matrix": [
                QMatrixEntry(
                    item_id="item_1",
                    item_version="1.0.0",
                    concept_id="concept_1",
                    weight=1.0,
                ),
                QMatrixEntry(
                    item_id="item_2",
                    item_version="1.0.0",
                    concept_id="concept_2",
                    weight=1.0,
                ),
            ],
        }
    )
    instances = [
        ItemInstance(
            item_instance_id="opaque-A",
            item_id="item_1",
            item_version="1.0.0",
            stem="First",
            parameters={},
            concept_ids=["concept_1"],
            rubric_id=None,
            max_score=1.0,
            source_evidence_ids=["evidence_1"],
        ),
        ItemInstance(
            item_instance_id="opaque-B",
            item_id="item_2",
            item_version="1.0.0",
            stem="Second",
            parameters={},
            concept_ids=["concept_2"],
            rubric_id=None,
            max_score=1.0,
            source_evidence_ids=["evidence_1"],
        ),
    ]
    payload = {
        "paper_id": "paper_1",
        "task_id": "task_1",
        "blueprint_id": "blueprint_1",
        "blueprint_version": "1.0.0",
        "learner_id": "learner_1",
        "sections": [
            PaperSection(
                section_id="section_1",
                name="Section",
                items=instances,
                score=2.0,
            )
        ],
        "generated_at": base.published_at,
        "immutable_checksum": "pending",
    }
    candidate = AssessmentPaper(**payload)
    paper = AssessmentPaper(
        **{**payload, "immutable_checksum": candidate.freeze()}
    )
    first_bundle = make_scoring_bundle(paper)
    first_audit = first_bundle.score_audit_records[0]
    second_audit = ScoreAuditRecord(
        **{
            **first_audit.model_dump(mode="python"),
            "audit_id": "audit_attempt_1_opaque-B",
            "item_instance_id": "opaque-B",
            "criterion_scores": [
                first_audit.criterion_scores[0].model_copy(
                    update={"criterion_id": "objective_item_2"}
                )
            ],
        }
    )
    bundle = ScoringResultBundle(
        **{
            **first_bundle.model_dump(mode="python"),
            "score_audit_records": [first_audit, second_audit],
            "total_score": 2.0,
            "max_score": 2.0,
        }
    )
    observations = build_observation_batch(
        FrozenAssessmentRecord(
            paper=paper,
            course_id="course_1",
            class_id="class_1",
            frozen_rubrics=[],
        ),
        bundle,
    )
    return bundle, knowledge, observations


def test_each_item_uses_its_own_q_matrix_concepts() -> None:
    bundle, knowledge, observations = _two_item_case()

    result = DeterministicStateUpdatePolicy().build_diagnosis(
        bundle,
        knowledge,
        observations,
    )

    actual = {
        item.item_instance_id: item.concept_ids
        for item in result.item_diagnoses
    }
    assert actual == {"opaque-A": ["concept_1"], "opaque-B": ["concept_2"]}
