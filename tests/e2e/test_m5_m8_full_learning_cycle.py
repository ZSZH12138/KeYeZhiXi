"""Complete M5/M8 learning cycle with independent service restarts."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from course_insight.contracts.assessment import (
    CriterionScore,
    RubricScoringResult,
)
from course_insight.contracts.knowledge import (
    AssessmentBlueprint,
    BlueprintSection,
    ItemCard,
    KnowledgeBundle,
    KnowledgeConcept,
    QMatrixEntry,
)
from course_insight.contracts.learning_models import (
    AdaptiveSelectionPolicy,
    CalibrationReviewDecision,
    ConceptResponse,
    ConceptResponseSequence,
    ItemExposureSnapshot,
    LearningObservation,
    LearningObservationBatch,
)
from course_insight.contracts.platform import (
    AssessmentSubmission,
    TeacherReviewSubmission,
)
from course_insight.contracts.tasking import TaskPlan
from course_insight.infrastructure.sqlite.m5_repository import SQLiteM5Repository
from course_insight.infrastructure.sqlite.m8_repository import SQLiteM8Repository
from course_insight.infrastructure.sqlite.m9_repository import SQLiteM9Repository
from course_insight.modules.m5_learner_class_state.aggregation import (
    DeterministicClassAggregationPolicy,
)
from course_insight.modules.m5_learner_class_state.bkt import BktEngine
from course_insight.modules.m5_learner_class_state.dina import DinaEngine
from course_insight.modules.m5_learner_class_state.service import M5StateService
from course_insight.modules.m5_learner_class_state.update_policy import (
    DeterministicStateUpdatePolicy,
)
from course_insight.modules.m8_assessment_scoring.irt_2pl import TwoPLCalibrator
from course_insight.modules.m8_assessment_scoring.paper_generator import (
    PaperGenerator,
)
from course_insight.modules.m8_assessment_scoring.rule_scorer import RuleScorer
from course_insight.modules.m8_assessment_scoring.service import (
    M8AssessmentService,
)
from course_insight.modules.m9_teacher_analytics.service import (
    M9TeacherAnalyticsService,
)
from tests.factories.m5_m8 import FixedClock, make_rubric


NOW = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
COURSE_ID = "course_full_cycle"
CLASS_ID = "class_full_cycle"
LEARNER_ID = "learner_full_cycle"


def _knowledge_bundle() -> KnowledgeBundle:
    rubric = make_rubric().model_copy(
        update={"rubric_id": "rubric_full_cycle"},
        deep=True,
    )
    objective_items = [
        ItemCard(
            item_id=f"item_{concept_id}_{index}",
            version="1.0.0",
            stem=f"Governed {concept_id} item {index}",
            item_type="multiple_choice",
            concept_ids=[concept_id],
            misconception_ids=[],
            difficulty_level=1 + index % 3,
            cognitive_level="apply",
            parameter_rules=[],
            answer_key={"answer": "yes", "max_score": 1.0},
            rubric_id=None,
            source_evidence_ids=[f"evidence_{concept_id}"],
            status="teacher_approved",
        )
        for concept_id in ("c1", "c2")
        for index in range(6)
    ]
    subjective = ItemCard(
        item_id="item_c2_subjective",
        version="1.0.0",
        stem="Explain the governed c2 reasoning.",
        item_type="short_answer",
        concept_ids=["c2"],
        misconception_ids=[],
        difficulty_level=2,
        cognitive_level="analyze",
        parameter_rules=[],
        answer_key={},
        rubric_id=rubric.rubric_id,
        source_evidence_ids=["evidence_c2"],
        status="teacher_approved",
    )
    items = [*objective_items, subjective]
    blueprint = AssessmentBlueprint(
        blueprint_id="blueprint_full_cycle",
        version="1.0.0",
        course_id=COURSE_ID,
        sections=[
            BlueprintSection(
                section_id="section_full_cycle",
                name="Weighted mixed assessment",
                item_count=4,
                score=4.0,
                item_types=[],
                concept_weights={"c1": 0.25, "c2": 0.75},
                difficulty_range=(1, 3),
                anchor_item_ids=[subjective.item_id],
            )
        ],
        total_score=4.0,
        duration_minutes=30,
        status="teacher_approved",
    )
    return KnowledgeBundle(
        knowledge_bundle_id="knowledge_full_cycle",
        course_package_id="package_full_cycle",
        course_id=COURSE_ID,
        bundle_version="1.0.0",
        concepts=[
            KnowledgeConcept(
                concept_id=concept_id,
                name=concept_id.upper(),
                chapter_id="chapter_full_cycle",
                description=f"Governed concept {concept_id}",
                aliases=[],
                status="published",
            )
            for concept_id in ("c1", "c2")
        ],
        prerequisite_relations=[],
        misconception_tags=[],
        items=items,
        rubrics=[rubric],
        blueprints=[blueprint],
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
        published_at=NOW,
    )


def _task() -> TaskPlan:
    return TaskPlan(
        task_id="task_full_cycle",
        task_type="stage_assessment",
        course_id=COURSE_ID,
        class_id=CLASS_ID,
        learner_id=LEARNER_ID,
        session_id="session_full_cycle",
        blueprint_id="blueprint_full_cycle",
        knowledge_bundle_id="knowledge_full_cycle",
        course_package_id="package_full_cycle",
        workflow=["M8", "M2", "M7", "M5", "M6", "M9"],
        next_module="M8",
        created_at=NOW,
    )


def _calibrator() -> TwoPLCalibrator:
    return TwoPLCalibrator(
        min_students=40,
        min_responses_per_item=40,
        min_items=10,
        max_iterations=80,
        likelihood_tolerance=1e-4,
        parameter_tolerance=1e-3,
    )


def _m8(database_path: Path, instant: datetime) -> M8AssessmentService:
    clock = FixedClock(instant)
    return M8AssessmentService(
        SQLiteM8Repository(database_path),
        RuleScorer(clock),
        PaperGenerator(clock),
        clock,
        irt_calibrator=_calibrator(),
    )


def _m5(database_path: Path) -> M5StateService:
    return M5StateService(
        SQLiteM5Repository(database_path),
        DeterministicStateUpdatePolicy(),
        DeterministicClassAggregationPolicy(),
        dina_engine=DinaEngine(
            min_students=40,
            min_responses_per_item=40,
            max_iterations=60,
        ),
        bkt_engine=BktEngine(
            min_students=40,
            min_observations_per_student=5,
            max_iterations=80,
        ),
    )


def _training_batches(bundle: KnowledgeBundle) -> list[LearningObservationBatch]:
    rng = np.random.default_rng(20260813)
    abilities = np.linspace(-2.5, 2.5, 60)
    item_count = len(bundle.items)
    batches = []
    for learner_index, theta in enumerate(abilities):
        observations = []
        for item_index, item in enumerate(bundle.items):
            discrimination = 0.9 + 0.8 * (item_index % 5) / 4.0
            difficulty = -1.5 + 3.0 * item_index / (item_count - 1)
            probability = 1.0 / (
                1.0 + math.exp(-discrimination * (float(theta) - difficulty))
            )
            correct = bool(rng.random() < probability)
            occurred_at = NOW - timedelta(days=1) + timedelta(
                seconds=learner_index * item_count + item_index
            )
            observations.append(
                LearningObservation(
                    observation_id=(
                        f"training_obs_{learner_index}_{item.item_id}"
                    ),
                    learner_id=f"training_learner_{learner_index}",
                    course_id=COURSE_ID,
                    class_id=CLASS_ID,
                    attempt_id=f"training_attempt_{learner_index}",
                    item_id=item.item_id,
                    item_version=item.version,
                    concept_ids=list(item.concept_ids),
                    score=1.0 if correct else 0.0,
                    max_score=1.0,
                    response_outcome="correct" if correct else "incorrect",
                    outcome_policy_version="binary-policy-1",
                    source_audit_id=(
                        f"training_audit_{learner_index}_{item.item_id}"
                    ),
                    source_audit_version=1,
                    occurred_at=occurred_at,
                )
            )
        batches.append(
            LearningObservationBatch(
                batch_id=f"training_batch_{learner_index}",
                learner_id=f"training_learner_{learner_index}",
                observations=observations,
                watermark=f"training-watermark-{learner_index}",
                created_at=max(item.occurred_at for item in observations),
            )
        )
    return batches


def _bkt_sequences(
    batches: list[LearningObservationBatch],
) -> list[ConceptResponseSequence]:
    sequences = []
    for batch in batches:
        for concept_id in ("c1", "c2"):
            source = [
                item
                for item in batch.observations
                if concept_id in item.concept_ids
            ]
            responses = [
                ConceptResponse(
                    observation_id=f"bkt_{item.observation_id}",
                    learner_id=item.learner_id,
                    course_id=item.course_id,
                    class_id=item.class_id,
                    attempt_id=item.attempt_id,
                    concept_id=concept_id,
                    is_correct=item.response_outcome == "correct",
                    source_audit_id=item.source_audit_id,
                    source_audit_version=item.source_audit_version,
                    occurred_at=item.occurred_at,
                )
                for item in source
            ]
            sequences.append(
                ConceptResponseSequence(
                    sequence_id=f"sequence_{batch.learner_id}_{concept_id}",
                    learner_id=batch.learner_id,
                    course_id=COURSE_ID,
                    class_id=CLASS_ID,
                    concept_id=concept_id,
                    responses=responses,
                    watermark=f"bkt-watermark-{batch.learner_id}-{concept_id}",
                    created_at=max(item.occurred_at for item in responses),
                )
            )
    return sequences


def _state_policy(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "aggregation_policy_version": "full-cycle-v1",
                "class_id": CLASS_ID,
                "class_size": 1,
                "consolidating_threshold": 0.4,
                "mastered_threshold": 0.8,
                "minimum_assessed_count": 1,
                "minimum_coverage": 1.0,
                "misconception_activation_threshold": 0.5,
            }
        ),
        encoding="utf-8",
    )
    return path


def _run_cycle(root: Path, *, restart_services: bool):
    root.mkdir()
    m5_path = root / "m5.sqlite3"
    m8_path = root / "m8.sqlite3"
    m9_path = root / "m9.sqlite3"
    m5_repository = SQLiteM5Repository(m5_path)
    m8_repository = SQLiteM8Repository(m8_path)
    m9_repository = SQLiteM9Repository(m9_path)
    m5_repository.initialize()
    m8_repository.initialize()
    m9_repository.initialize()
    bundle = _knowledge_bundle()
    training = _training_batches(bundle)

    m8 = _m8(m8_path, NOW)
    paper = m8.generate_paper(_task(), bundle, None, None)
    selected_concepts = [
        item.concept_ids[0] for item in paper.all_items()
    ]
    assert selected_concepts.count("c1") == 1
    assert selected_concepts.count("c2") == 3
    assert any(item.is_subjective() for item in paper.all_items())
    if restart_services:
        # Restarting a service must not change domain timestamps or results.
        m8 = _m8(m8_path, NOW)
        paper = m8.get_paper(paper.paper_id)

    submission = AssessmentSubmission(
        submission_id="submission_full_cycle",
        attempt_id="attempt_full_cycle",
        paper_id=paper.paper_id,
        learner_id=LEARNER_ID,
        answers={
            item.item_instance_id: (
                "governed working" if item.is_subjective() else "yes"
            )
            for item in paper.all_items()
        },
        submitted_at=NOW + timedelta(minutes=2),
    )
    preparation = m8.prepare_scoring(paper, submission, bundle)
    subjective_results = [
        RubricScoringResult(
            scoring_task_id=task.scoring_task_id,
            criterion_scores=[
                CriterionScore(
                    criterion_id=task.rubric.criteria[0].criterion_id,
                    score=1.0,
                    student_evidence=task.student_answer,
                    course_evidence_id=(
                        task.rubric.criteria[0].course_evidence_ids[0]
                    ),
                    reason="The governed reasoning satisfies the rubric.",
                )
            ],
            total_score=1.0,
            confidence=0.95,
            missing_concept_ids=[],
            review_flags=[],
            model_name="deterministic-local-scorer",
            model_version="1.0.0",
            scored_at=NOW + timedelta(minutes=3),
        )
        for task in preparation.rubric_scoring_tasks
    ]
    scoring = m8.finalize_scoring(preparation, subjective_results)
    subjective_audit = next(
        audit
        for audit in scoring.score_audit_records
        if audit.item_instance_id
        == preparation.rubric_scoring_tasks[0].item_instance.item_instance_id
    )
    review_submission = TeacherReviewSubmission(
        submission_id="review_full_cycle",
        audit_id=subjective_audit.audit_id,
        expected_audit_version=subjective_audit.audit_version,
        expected_audit_checksum=subjective_audit.content_checksum(),
        reviewer_id="teacher_full_cycle",
        decision="confirm",
        final_total_score=subjective_audit.total_score,
        criterion_overrides=[],
        teacher_comment="Confirmed against the governed rubric evidence.",
        submitted_at=NOW + timedelta(minutes=4),
    )
    m9 = M9TeacherAnalyticsService(m9_repository, object(), object())
    review = m9.record_teacher_review(review_submission, scoring)
    reviewed = m8.apply_teacher_review(scoring, review)
    observations = m8.build_observation_batch(paper.paper_id, reviewed)
    assert len(observations.observations) == len(paper.all_items())
    assert max(
        item.source_audit_version
        for item in observations.observations
        if item.source_audit_id == review.audit_id
    ) == 2

    m5 = _m5(m5_path)
    dina_model = m5.fit_dina_model(training, bundle.q_matrix)
    bkt_model = m5.fit_bkt_model(_bkt_sequences(training))
    if restart_services:
        m5 = _m5(m5_path)
    model_run = m5.run_learning_models(observations, bundle)
    state = m5.update_state(
        scoring_result_bundle=reviewed,
        knowledge_bundle=bundle,
        previous_learner_state_snapshot=None,
        previous_class_state_snapshot=None,
        state_policy_path=_state_policy(root / "state-policy.json"),
        learning_observation_batch=observations,
        learning_model_run=model_run,
    )
    assert model_run.status == "completed"
    assert state.learner_state_snapshot.model_run_id == model_run.run_id

    calibration = m8.calibrate_irt(
        training,
        NOW + timedelta(minutes=6),
    )
    assert calibration.status == "shadow"
    quality = m9.build_model_quality_report(
        calibration,
        NOW + timedelta(minutes=7),
    )
    assert quality.status == "ready"
    approval = CalibrationReviewDecision(
        decision_id="calibration_review_full_cycle",
        calibration_run_id=calibration.run_id,
        reviewer_id="teacher_full_cycle",
        decision="approve",
        target_parameter_version=calibration.parameter_set.version,
        reason="Quality evidence passed the governed release gate.",
        reviewed_at=NOW + timedelta(minutes=8),
    )
    approved = m8.apply_calibration_review(
        calibration.run_id,
        quality,
        approval,
    )
    if restart_services:
        m8 = _m8(m8_path, NOW + timedelta(minutes=9))
    approved = m8.get_active_parameter_set(approved.parameter_set_id)
    ability = m8.estimate_ability(
        approved.parameter_set_id,
        observations.observations,
        course_id=COURSE_ID,
    )
    selection = m8.select_adaptive_items(
        policy=AdaptiveSelectionPolicy(
            policy_id="adaptive_full_cycle",
            version="1.0.0",
            parameter_set_id=approved.parameter_set_id,
            max_items=2,
            concept_quotas={"c1": 1, "c2": 1},
            max_item_exposure_rate=0.20,
            difficulty_range=(-4.0, 4.0),
            status="configured",
        ),
        ability_estimate=ability,
        parameter_set=approved,
        candidate_items=bundle.items,
        administered_item_ids=frozenset(
            item.item_id for item in paper.all_items()
        ),
        exposure_snapshot=ItemExposureSnapshot(
            parameter_set_id=approved.parameter_set_id,
            total_sessions=100,
            item_administered_counts={},
        ),
        requested_at=NOW + timedelta(minutes=10),
    )
    assert selection.status == "selected"
    assert len(selection.item_ids) == 2

    recovered_m8 = SQLiteM8Repository(m8_path)
    recovered_m5 = SQLiteM5Repository(m5_path)
    assert recovered_m8.get_scoring_result(reviewed.attempt_id) == reviewed
    assert recovered_m8.get_parameter_set(approved.parameter_set_id) == approved
    assert recovered_m8.get_ability_estimate(ability.estimate_id) == ability
    assert recovered_m8.get_adaptive_selection(selection.selection_id) == selection
    assert recovered_m5.get_state_update(state.diagnosis_result.attempt_id) == state
    assert m9.get_review_decision(review.decision_id) == review
    return {
        "paper": paper,
        "reviewed": reviewed,
        "observations": observations,
        "dina_model": dina_model,
        "bkt_model": bkt_model,
        "model_run": model_run,
        "state": state,
        "calibration": calibration,
        "quality": quality,
        "approved": approved,
        "ability": ability,
        "selection": selection,
    }


def test_full_learning_cycle_is_identical_after_m5_and_m8_restarts(
    tmp_path: Path,
) -> None:
    uninterrupted = _run_cycle(
        tmp_path / "uninterrupted",
        restart_services=False,
    )
    restarted = _run_cycle(
        tmp_path / "restarted",
        restart_services=True,
    )

    assert restarted == uninterrupted
