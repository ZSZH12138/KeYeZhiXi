"""A one-item follow-up must not mark unassessed concepts as mastered."""

from course_insight.contracts.state import (
    ConceptState,
    DiagnosisResult,
    ItemDiagnosis,
    LearnerStateSnapshot,
)
from course_insight.modules.m5_learner_class_state.update_policy import (
    DeterministicStateUpdatePolicy,
    StatePolicy,
)
from tests.factories.m5_m8 import UTC_TIME, make_knowledge_bundle, make_paper, make_scoring_bundle


def _policy() -> StatePolicy:
    return StatePolicy(
        aggregation_policy_version="1.0.0",
        class_id="class_1",
        class_size=1,
        consolidating_threshold=0.4,
        mastered_threshold=0.8,
        minimum_assessed_count=1,
        minimum_coverage=0.0,
        misconception_activation_threshold=0.5,
    )


def test_perfect_follow_up_keeps_previous_mastery_for_other_concepts() -> None:
    knowledge = make_knowledge_bundle(subjective=False)
    extra = knowledge.concepts[0].model_copy(
        update={"concept_id": "concept_untouched", "name": "Untouched"}
    )
    knowledge = knowledge.model_copy(
        update={"concepts": [extra, *knowledge.concepts]}
    )
    previous = LearnerStateSnapshot(
        snapshot_id="learner_1_state_v1",
        course_id="course_1",
        class_id="class_1",
        learner_id="learner_1",
        state_version=1,
        concept_states=[
            ConceptState(
                concept_id="concept_untouched",
                mastery_probability=0.25,
                mastery_confidence=0.7,
                misconceptions=[],
                hint_dependency=0.0,
                recent_correction_rate=0.0,
                evidence_count=3,
                updated_at=UTC_TIME,
            ),
            ConceptState(
                concept_id="concept_2",
                mastery_probability=0.25,
                mastery_confidence=0.7,
                misconceptions=[],
                hint_dependency=0.0,
                recent_correction_rate=0.0,
                evidence_count=3,
                updated_at=UTC_TIME,
            ),
        ],
        overall_mastery=0.25,
        evidence_count=3,
        updated_at=UTC_TIME,
    )
    paper = make_paper(subjective=False)
    bundle = make_scoring_bundle(paper, score=1.0)
    diagnosis = DiagnosisResult(
        diagnosis_id="attempt_1_diagnosis_v1",
        attempt_id=bundle.attempt_id,
        learner_id=bundle.learner_id,
        item_diagnoses=[
            ItemDiagnosis(
                item_instance_id=paper.all_items()[0].item_instance_id,
                concept_ids=["concept_2"],
                misconception_ids=[],
                error_type="correct",
                confidence=1.0,
                evidence_audit_ids=["audit_attempt_1_random-instance-88:1"],
                prerequisite_gap_ids=[],
            )
        ],
        priority_concept_ids=["concept_2"],
        priority_misconception_ids=[],
        generated_at=bundle.finalized_at,
    )

    updated = DeterministicStateUpdatePolicy().build_learner_state(
        bundle,
        knowledge,
        diagnosis,
        previous,
        _policy(),
        None,
    )
    by_id = {item.concept_id: item for item in updated.concept_states}

    assert by_id["concept_2"].mastery_probability == 1.0
    assert by_id["concept_untouched"].mastery_probability == 0.25
    assert by_id["concept_untouched"].evidence_count == 3
    assert by_id["concept_untouched"].recent_correction_rate == 0.0
