from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from course_insight.contracts.analytics import (
    ClassReport,
    IndividualReport,
    TeacherAnalyticsBundle,
    TeacherReviewDecision,
)
from course_insight.contracts.errors import DomainError
from course_insight.contracts.events import LearningEvent
from course_insight.contracts.knowledge import KnowledgeBundle, KnowledgeConcept
from course_insight.contracts.assessment import (
    AssessmentPaper,
    CriterionScore,
    ItemInstance,
    PaperSection,
    RemediationPlan,
    ScoreAuditRecord,
    ScoringPreparationResult,
    ScoringResultBundle,
)
from course_insight.contracts.state import (
    ClassStateSnapshot,
    ConceptState,
    DiagnosisResult,
    ItemDiagnosis,
    LearnerStateSnapshot,
    StateUpdateResult,
)
from course_insight.contracts.tasking import TaskPlan
from course_insight.contracts.tutoring import (
    EvidenceCitation,
    StudentFeedbackPackage,
)
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.m4_repository import SQLiteM4Repository
from course_insight.infrastructure.sqlite.m5_repository import SQLiteM5Repository
from course_insight.infrastructure.sqlite.m7_repository import SQLiteM7Repository
from course_insight.infrastructure.sqlite.m8_repository import SQLiteM8Repository
from course_insight.infrastructure.sqlite.m9_repository import SQLiteM9Repository
from course_insight.infrastructure.sqlite.migrations import (
    SCHEMA_VERSION,
    current_schema_version,
    migrate,
)
from course_insight.modules.m4_task_orchestration.service import (
    M4TaskOrchestrationService,
)
from course_insight.modules.m5_learner_class_state.service import M5StateService
from course_insight.modules.m7_local_model.service import M7LocalModelService
from course_insight.modules.m8_assessment_scoring.service import (
    M8AssessmentService,
)
from course_insight.modules.m9_teacher_analytics.service import (
    M9TeacherAnalyticsService,
)


NOW = datetime(2026, 7, 25, 8, 0, tzinfo=timezone.utc)


def _paper() -> AssessmentPaper:
    item = ItemInstance(
        item_instance_id="instance_1",
        item_id="item_1",
        item_version="1.0.0",
        stem="Choose the governed answer.",
        parameters={},
        concept_ids=["concept_1"],
        rubric_id=None,
        max_score=1.0,
        source_evidence_ids=["evidence_1"],
    )
    paper = AssessmentPaper(
        paper_id="paper_1",
        task_id="task_1",
        blueprint_id="blueprint_1",
        blueprint_version="1.0.0",
        learner_id="learner_1",
        sections=[
            PaperSection(
                section_id="section_1",
                name="Objective",
                items=[item],
                score=1.0,
            )
        ],
        generated_at=NOW,
        immutable_checksum="pending",
    )
    paper.immutable_checksum = paper.freeze()
    return paper


def _audit(*, attempt_id: str = "attempt_1") -> ScoreAuditRecord:
    return ScoreAuditRecord(
        audit_id=f"audit_{attempt_id}",
        audit_version=1,
        attempt_id=attempt_id,
        item_instance_id="instance_1",
        criterion_scores=[
            CriterionScore(
                criterion_id="objective_item_1",
                score=1.0,
                student_evidence="false",
                course_evidence_id="evidence_1",
                reason="Matches the governed answer.",
            )
        ],
        total_score=1.0,
        max_score=1.0,
        confidence=1.0,
        scoring_method="rule",
        review_status="not_required",
        review_reason=[],
        created_at=NOW,
    )


def _scoring_bundle(*, attempt_id: str = "attempt_1") -> ScoringResultBundle:
    return ScoringResultBundle(
        attempt_id=attempt_id,
        paper_id="paper_1",
        learner_id="learner_1",
        score_audit_records=[_audit(attempt_id=attempt_id)],
        learning_events=[],
        remediation_plan=RemediationPlan(
            plan_id=f"remediation_{attempt_id}",
            based_on_attempt_id=attempt_id,
            learner_id="learner_1",
            targets=[],
            created_at=NOW,
        ),
        total_score=1.0,
        max_score=1.0,
        finalized_at=NOW,
    )


def _audit_history(
    audit_id: str,
    item_instance_id: str,
    latest_version: int,
) -> list[ScoreAuditRecord]:
    return [
        ScoreAuditRecord(
            audit_id=audit_id,
            audit_version=version,
            attempt_id="attempt_vector",
            item_instance_id=item_instance_id,
            criterion_scores=[
                CriterionScore(
                    criterion_id=f"criterion_{item_instance_id}",
                    score=1.0,
                    student_evidence="supported",
                    course_evidence_id="evidence_1",
                    reason="Versioned governed score.",
                )
            ],
            total_score=1.0,
            max_score=1.0,
            confidence=1.0,
            scoring_method="rule" if version == 1 else "teacher_override",
            review_status="not_required" if version == 1 else "approved",
            review_reason=[],
            created_at=NOW + timedelta(minutes=version),
        )
        for version in range(1, latest_version + 1)
    ]


def _vector_scoring_bundle(
    *,
    first_version: int,
    second_version: int,
    finalized_at: datetime,
) -> ScoringResultBundle:
    return ScoringResultBundle(
        attempt_id="attempt_vector",
        paper_id="paper_1",
        learner_id="learner_1",
        score_audit_records=[
            *_audit_history("audit_a", "instance_a", first_version),
            *_audit_history("audit_b", "instance_b", second_version),
        ],
        learning_events=[],
        remediation_plan=RemediationPlan(
            plan_id=f"remediation_{first_version}_{second_version}",
            based_on_attempt_id="attempt_vector",
            learner_id="learner_1",
            targets=[],
            created_at=finalized_at,
        ),
        total_score=2.0,
        max_score=2.0,
        finalized_at=finalized_at,
    )


def _state_result(
    *,
    attempt_id: str,
    course_id: str,
    class_id: str,
    state_version: int,
) -> StateUpdateResult:
    learner = LearnerStateSnapshot(
        snapshot_id=f"learner_{course_id}_{state_version}",
        course_id=course_id,
        class_id=class_id,
        learner_id="learner_1",
        state_version=state_version,
        concept_states=[
            ConceptState(
                concept_id="concept_1",
                mastery_probability=1.0,
                mastery_confidence=1.0,
                misconceptions=[],
                hint_dependency=0.0,
                recent_correction_rate=1.0,
                evidence_count=1,
                updated_at=NOW,
            )
        ],
        overall_mastery=1.0,
        evidence_count=1,
        updated_at=NOW,
    )
    class_state = ClassStateSnapshot(
        snapshot_id=f"class_{course_id}_{state_version}",
        course_id=course_id,
        class_id=class_id,
        aggregation_policy_version="1.0.0",
        scope={"course_id": course_id, "class_id": class_id},
        class_size=1,
        assessed_count=1,
        coverage_rate=1.0,
        concept_status=[],
        misconception_summary=[],
        evidence_status="sufficient",
        updated_at=NOW,
    )
    audit_id = f"audit_{attempt_id}:1"
    return StateUpdateResult(
        diagnosis_result=DiagnosisResult(
            diagnosis_id=f"diagnosis_{attempt_id}",
            attempt_id=attempt_id,
            learner_id="learner_1",
            item_diagnoses=[
                ItemDiagnosis(
                    item_instance_id="instance_1",
                    concept_ids=["concept_1"],
                    misconception_ids=[],
                    error_type="none",
                    confidence=1.0,
                    evidence_audit_ids=[audit_id],
                    prerequisite_gap_ids=[],
                )
            ],
            priority_concept_ids=["concept_1"],
            priority_misconception_ids=[],
            generated_at=NOW,
        ),
        learner_state_snapshot=learner,
        class_state_snapshot=class_state,
        processed_audit_ids=[audit_id],
        updated_at=NOW,
    )


def _m5_knowledge(course_id: str) -> KnowledgeBundle:
    return KnowledgeBundle.model_construct(
        course_id=course_id,
        concepts=[
            KnowledgeConcept(
                concept_id="concept_1",
                name="Concept",
                chapter_id="chapter_1",
                description="Governed concept.",
                aliases=[],
                status="published",
            )
        ],
        misconception_tags=[],
        prerequisite_relations=[],
    )


def _m5_policy(
    tmp_path: Path,
    *,
    class_id: str,
    name: str,
) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(
        dumps_json(
            {
                "aggregation_policy_version": "1.0.0",
                "class_id": class_id,
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
    return path


def _teacher_policy(tmp_path: Path, *, name: str) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(
        dumps_json(
            {
                "minimum_coverage": 1.0,
                "minimum_assessed_count": 1,
                "minimum_confidence": 0.0,
                "weak_mastery_threshold": 0.5,
                "misconception_threshold": 0.5,
                "priority_support_threshold": 0.5,
            }
        ),
        encoding="utf-8",
    )
    return path


def _m5_scoring_bundle(
    *,
    attempt_id: str,
    course_id: str,
    class_id: str,
    latest_audit_version: int,
) -> ScoringResultBundle:
    audit_id = f"audit_{attempt_id}"
    audits = [
        ScoreAuditRecord(
            audit_id=audit_id,
            audit_version=version,
            attempt_id=attempt_id,
            item_instance_id="instance_1",
            criterion_scores=[
                CriterionScore(
                    criterion_id="objective_item_1",
                    score=1.0,
                    student_evidence="supported",
                    course_evidence_id="evidence_1",
                    reason="Governed score.",
                )
            ],
            total_score=1.0,
            max_score=1.0,
            confidence=1.0,
            scoring_method="rule" if version == 1 else "teacher_override",
            review_status="not_required" if version == 1 else "approved",
            review_reason=[],
            created_at=NOW + timedelta(minutes=version),
        )
        for version in range(1, latest_audit_version + 1)
    ]
    finalized_at = NOW + timedelta(minutes=latest_audit_version)
    return ScoringResultBundle(
        attempt_id=attempt_id,
        paper_id=f"paper_{attempt_id}",
        learner_id="learner_1",
        score_audit_records=audits,
        learning_events=[
            LearningEvent(
                event_id=f"event_{attempt_id}_{latest_audit_version}",
                event_type="assessment_scored",
                course_id=course_id,
                class_id=class_id,
                learner_id="learner_1",
                attempt_id=attempt_id,
                payload={},
                occurred_at=finalized_at,
            )
        ],
        remediation_plan=RemediationPlan(
            plan_id=f"remediation_{attempt_id}_{latest_audit_version}",
            based_on_attempt_id=attempt_id,
            learner_id="learner_1",
            targets=[],
            created_at=finalized_at,
        ),
        total_score=1.0,
        max_score=1.0,
        finalized_at=finalized_at,
    )
def _feedback() -> StudentFeedbackPackage:
    return StudentFeedbackPackage(
        feedback_id="feedback_1",
        task_id="task_1",
        learner_id="learner_1",
        message="Review the cited course evidence.",
        rubric_feedback=[],
        missing_concept_ids=[],
        evidence_citations=[
            EvidenceCitation(
                evidence_id="evidence_1",
                source_id="source_1",
                locator="p.1",
                quote="Governed evidence.",
            )
        ],
        next_practice_item_ids=[],
        confidence=1.0,
        generated_at=NOW,
    )


def _analytics(
    *,
    report_id: str = "report_1",
    generated_at: datetime = NOW,
) -> TeacherAnalyticsBundle:
    return TeacherAnalyticsBundle(
        report_id=report_id,
        class_report=ClassReport(
            class_id="class_1",
            coverage_rate=1.0,
            concept_summaries=[],
            misconception_summaries=[],
            score_statistics={"mean": 1.0},
            evidence_status="sufficient",
        ),
        individual_reports=[
            IndividualReport(
                learner_id="learner_1",
                overall_mastery=1.0,
                weak_concept_ids=[],
                active_misconception_ids=[],
                recent_score=1.0,
                review_required_count=0,
            )
        ],
        review_queue=[],
        teaching_suggestions=[],
        generated_at=generated_at,
    )


def _review() -> TeacherReviewDecision:
    return TeacherReviewDecision(
        decision_id="decision_1",
        audit_id="audit_attempt_1",
        expected_audit_version=1,
        decision="confirm",
        final_total_score=1.0,
        criterion_overrides=[],
        teacher_comment="Confirmed against the governed evidence.",
        reviewer_id="teacher_1",
        reviewed_at=NOW,
    )


def _repository(repository_type: type[object], database_path: Path) -> object:
    repository = repository_type(database_path)
    repository.initialize()
    return repository


def test_m4_service_can_reload_a_persisted_task_plan(tmp_path: Path) -> None:
    repository = _repository(SQLiteM4Repository, tmp_path / "workflow.db")
    plan = TaskPlan(
        task_id="task_key_1",
        task_type="stage_assessment",
        course_id="course_1",
        class_id="class_1",
        learner_id="learner_1",
        session_id="session_1",
        blueprint_id="blueprint_1",
        knowledge_bundle_id="bundle_1",
        course_package_id="package_1",
        workflow=["M8", "M2", "M7", "M5", "M6", "M9"],
        next_module="M8",
        created_at=NOW,
    )
    repository.insert_or_get_task_plan(plan, "key_1")

    service = M4TaskOrchestrationService(repository, lambda _: "unused")

    assert service.get_task_plan("task_key_1") == plan
    assert service.get_task_plan("missing") is None


def test_m5_persists_complete_results_and_scopes_actor_by_course(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    repository = _repository(SQLiteM5Repository, database_path)
    course_1 = _state_result(
        attempt_id="attempt_1",
        course_id="course_1",
        class_id="class_1",
        state_version=1,
    )
    course_2 = _state_result(
        attempt_id="attempt_2",
        course_id="course_2",
        class_id="class_2",
        state_version=1,
    )

    assert repository.insert_or_get_state_update(course_1) == course_1
    assert repository.insert_or_get_state_update(course_2) == course_2

    reloaded = SQLiteM5Repository(database_path)
    assert reloaded.get_state_update("attempt_1") == course_1
    assert (
        reloaded.get_latest_learner_state(
            "course_1",
            "class_1",
            "learner_1",
        )
        == course_1.learner_state_snapshot
    )
    assert (
        reloaded.get_latest_learner_state(
            "course_2",
            "class_2",
            "learner_1",
        )
        == course_2.learner_state_snapshot
    )
    assert reloaded.get_processed_audit_ids(
        "course_1",
        "class_1",
        "learner_1",
    ) == frozenset(course_1.processed_audit_ids)

    conflict = course_1.model_copy(update={"updated_at": NOW.replace(hour=9)})
    with pytest.raises(RuntimeError, match="conflict"):
        reloaded.insert_or_get_state_update(conflict)


def test_m5_forward_migration_preserves_all_legacy_v3_state_versions(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    legacy_results = [
        _state_result(
            attempt_id=f"attempt_legacy_{state_version}",
            course_id="course_legacy",
            class_id="class_legacy",
            state_version=state_version,
        )
        for state_version in (1, 2)
    ]
    legacy_results = [
        legacy_results[0].model_copy(
            update={
                "class_state_snapshot": legacy_results[
                    0
                ].class_state_snapshot.model_copy(
                    update={
                        "updated_at": datetime(
                            2025,
                            1,
                            1,
                            23,
                            tzinfo=timezone(timedelta(hours=8)),
                        )
                    }
                )
            }
        ),
        legacy_results[1].model_copy(
            update={
                "class_state_snapshot": legacy_results[
                    1
                ].class_state_snapshot.model_copy(
                    update={
                        "updated_at": datetime(
                            2025,
                            1,
                            1,
                            10,
                            tzinfo=timezone(timedelta(hours=-8)),
                        )
                    }
                )
            }
        ),
    ]
    with connect_sqlite(database_path) as connection:
        migrate(connection)
        connection.execute("DROP TABLE m5_learner_states")
        connection.execute(
            """
            CREATE TABLE m5_learner_states (
                snapshot_id TEXT PRIMARY KEY,
                learner_id TEXT NOT NULL,
                state_version INTEGER NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE (learner_id, state_version)
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO m5_learner_states(
                snapshot_id,
                learner_id,
                state_version,
                payload
            )
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    result.learner_state_snapshot.snapshot_id,
                    result.learner_state_snapshot.learner_id,
                    result.learner_state_snapshot.state_version,
                    dumps_json(result.learner_state_snapshot.to_dict()),
                )
                for result in legacy_results
            ],
        )
        connection.execute("DROP TABLE m5_class_states")
        connection.execute(
            """
            CREATE TABLE m5_class_states (
                snapshot_id TEXT PRIMARY KEY,
                class_id TEXT NOT NULL,
                aggregation_policy_version TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO m5_class_states(
                snapshot_id,
                class_id,
                aggregation_policy_version,
                payload
            )
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    result.class_state_snapshot.snapshot_id,
                    result.class_state_snapshot.class_id,
                    result.class_state_snapshot.aggregation_policy_version,
                    dumps_json(result.class_state_snapshot.to_dict()),
                )
                for result in legacy_results
            ],
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 4")

        migrate(connection)
        migrate(connection)
        assert SCHEMA_VERSION == 15
        assert current_schema_version(connection) == SCHEMA_VERSION
        assert (
            connection.execute(
                """
                SELECT COUNT(*)
                FROM schema_migrations
                WHERE version = 4 AND name = 'module_owned_recovery'
                """
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                """
                SELECT COUNT(*)
                FROM schema_migrations
                WHERE version = 6 AND name = 'm0_assessment_workflow_refs'
                """
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                """
                SELECT COUNT(*)
                FROM schema_migrations
                WHERE version = 5 AND name = 'm0_assessment_workflow'
                """
            ).fetchone()[0]
            == 1
        )

    repository = SQLiteM5Repository(database_path)
    for result in legacy_results:
        learner = result.learner_state_snapshot
        class_state = result.class_state_snapshot
        assert (
            repository.get_learner_state(
                learner.learner_id,
                learner.state_version,
            )
            == learner
        )
        assert repository.get_class_state(class_state.snapshot_id) == class_state
    assert (
        repository.get_latest_learner_state(
            "course_legacy",
            "class_legacy",
            "learner_1",
        )
        == legacy_results[-1].learner_state_snapshot
    )
    assert (
        repository.get_latest_class_state("course_legacy", "class_legacy")
        == legacy_results[-1].class_state_snapshot
    )


def test_m5_rejects_scope_that_disagrees_with_the_policy_class() -> None:
    previous = _state_result(
        attempt_id="attempt_1",
        course_id="course_1",
        class_id="class_1",
        state_version=1,
    )
    knowledge = KnowledgeBundle.model_construct(course_id="course_1")

    with pytest.raises(DomainError, match="authoritative policy scope"):
        M5StateService._state_scope(
            _scoring_bundle(),
            knowledge,
            previous.learner_state_snapshot,
            previous.class_state_snapshot,
            authoritative_class_id="class_2",
        )


def test_m5_latest_class_uses_numeric_state_version_when_times_tie(
    tmp_path: Path,
) -> None:
    repository = _repository(SQLiteM5Repository, tmp_path / "workflow.db")
    version_9 = _state_result(
        attempt_id="attempt_9",
        course_id="course_1",
        class_id="class_1",
        state_version=9,
    )
    version_10 = _state_result(
        attempt_id="attempt_10",
        course_id="course_1",
        class_id="class_1",
        state_version=10,
    )
    repository.insert_or_get_state_update(version_9)
    repository.insert_or_get_state_update(version_10)

    assert (
        repository.get_latest_class_state("course_1", "class_1")
        == version_10.class_state_snapshot
    )


def test_m5_real_service_accepts_second_attempt_audit_v1_after_restart(
    tmp_path: Path,
) -> None:
    repository = _repository(SQLiteM5Repository, tmp_path / "workflow.db")
    policy = _m5_policy(tmp_path, class_id="class_1", name="policy")
    knowledge = _m5_knowledge("course_1")
    first = M5StateService(repository, object(), object()).update_state(
        _m5_scoring_bundle(
            attempt_id="attempt_1",
            course_id="course_1",
            class_id="class_1",
            latest_audit_version=1,
        ),
        knowledge,
        None,
        None,
        policy,
    )

    second = M5StateService(
        SQLiteM5Repository(tmp_path / "workflow.db"),
        object(),
        object(),
    ).update_state(
        _m5_scoring_bundle(
            attempt_id="attempt_2",
            course_id="course_1",
            class_id="class_1",
            latest_audit_version=1,
        ),
        knowledge,
        first.learner_state_snapshot,
        first.class_state_snapshot,
        policy,
    )

    assert second.learner_state_snapshot.state_version == 2
    assert repository.get_state_update("attempt_2") == second


def test_m5_real_service_persists_teacher_review_version_for_same_attempt(
    tmp_path: Path,
) -> None:
    repository = _repository(SQLiteM5Repository, tmp_path / "workflow.db")
    policy = _m5_policy(tmp_path, class_id="class_1", name="policy")
    knowledge = _m5_knowledge("course_1")
    service = M5StateService(repository, object(), object())
    first = service.update_state(
        _m5_scoring_bundle(
            attempt_id="attempt_review",
            course_id="course_1",
            class_id="class_1",
            latest_audit_version=1,
        ),
        knowledge,
        None,
        None,
        policy,
    )

    reviewed = M5StateService(
        SQLiteM5Repository(tmp_path / "workflow.db"),
        object(),
        object(),
    ).update_state(
        _m5_scoring_bundle(
            attempt_id="attempt_review",
            course_id="course_1",
            class_id="class_1",
            latest_audit_version=2,
        ),
        knowledge,
        first.learner_state_snapshot,
        first.class_state_snapshot,
        policy,
    )

    assert reviewed.learner_state_snapshot.state_version == 2
    assert repository.get_state_update("attempt_review") == reviewed


def test_m5_real_service_allows_same_actor_and_class_id_across_courses(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    repository = _repository(SQLiteM5Repository, database_path)
    policy = _m5_policy(tmp_path, class_id="shared_class", name="policy")

    course_1 = M5StateService(repository, object(), object()).update_state(
        _m5_scoring_bundle(
            attempt_id="attempt_course_1",
            course_id="course_1",
            class_id="shared_class",
            latest_audit_version=1,
        ),
        _m5_knowledge("course_1"),
        None,
        None,
        policy,
    )
    course_2 = M5StateService(repository, object(), object()).update_state(
        _m5_scoring_bundle(
            attempt_id="attempt_course_2",
            course_id="course_2",
            class_id="shared_class",
            latest_audit_version=1,
        ),
        _m5_knowledge("course_2"),
        None,
        None,
        policy,
    )

    assert course_1.learner_state_snapshot.snapshot_id == (
        course_2.learner_state_snapshot.snapshot_id
    )
    assert course_1.class_state_snapshot.snapshot_id == (
        course_2.class_state_snapshot.snapshot_id
    )
    assert (
        repository.get_latest_learner_state(
            "course_1",
            "shared_class",
            "learner_1",
        )
        == course_1.learner_state_snapshot
    )
    assert (
        repository.get_latest_learner_state(
            "course_2",
            "shared_class",
            "learner_1",
        )
        == course_2.learner_state_snapshot
    )
    assert (
        repository.get_latest_class_state("course_1", "shared_class")
        == course_1.class_state_snapshot
    )
    assert (
        repository.get_latest_class_state("course_2", "shared_class")
        == course_2.class_state_snapshot
    )
    with pytest.raises(RuntimeError, match="ambiguous across courses"):
        repository.get_learner_state("learner_1", 1)
    with pytest.raises(RuntimeError, match="ambiguous across courses"):
        repository.get_class_state(course_1.class_state_snapshot.snapshot_id)


def test_m7_feedback_is_idempotent_and_supports_both_recovery_keys(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    repository = _repository(SQLiteM7Repository, database_path)
    feedback = _feedback()

    assert repository.insert_or_get_feedback(feedback) == feedback
    assert repository.insert_or_get_feedback(feedback.model_copy(deep=True)) == feedback

    reloaded = SQLiteM7Repository(database_path)
    assert reloaded.get_feedback("feedback_1") == feedback
    assert reloaded.get_feedback_for_task("task_1", "learner_1") == feedback

    conflict = feedback.model_copy(update={"message": "Different content."})
    with pytest.raises(RuntimeError, match="conflict"):
        reloaded.insert_or_get_feedback(conflict)


def test_m8_recovers_paper_scope_and_complete_scoring_bundle(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    repository = _repository(SQLiteM8Repository, database_path)
    paper = _paper()
    assert repository.insert_or_get_paper(
        paper,
        course_id="course_1",
        class_id="class_1",
    ) == paper

    restarted_repository = SQLiteM8Repository(database_path)
    service = M8AssessmentService(restarted_repository, object(), object())
    assert service.get_paper("paper_1") == paper

    preparation = ScoringPreparationResult(
        attempt_id="attempt_1",
        paper_id="paper_1",
        learner_id="learner_1",
        objective_audit_records=[_audit()],
        rubric_scoring_tasks=[],
        evidence_queries=[],
        raw_answer_checksum="checksum_1",
        prepared_at=NOW,
    )
    bundle = service.finalize_scoring(preparation, [])

    assert bundle.learning_events[0].course_id == "course_1"
    assert bundle.learning_events[0].class_id == "class_1"
    assert "unavailable" not in bundle.learning_events[0].course_id
    assert restarted_repository.get_scoring_result("attempt_1") == bundle

    conflict = bundle.model_copy(
        update={
            "learning_events": [
                bundle.learning_events[0].model_copy(
                    update={"payload": {"paper_id": "different_paper"}}
                )
            ]
        }
    )
    with pytest.raises(RuntimeError, match="conflict"):
        restarted_repository.insert_or_get_scoring_result(conflict)


def test_m8_refuses_to_emit_an_event_without_paper_scope() -> None:
    preparation = ScoringPreparationResult(
        attempt_id="attempt_1",
        paper_id="paper_1",
        learner_id="learner_1",
        objective_audit_records=[_audit()],
        rubric_scoring_tasks=[],
        evidence_queries=[],
        raw_answer_checksum="checksum_1",
        prepared_at=NOW,
    )

    with pytest.raises(DomainError, match="paper execution scope"):
        M8AssessmentService(object(), object(), object()).finalize_scoring(
            preparation,
            [],
        )


def test_m8_scoring_result_keys_do_not_collapse_distinct_audit_vectors(
    tmp_path: Path,
) -> None:
    repository = _repository(SQLiteM8Repository, tmp_path / "workflow.db")
    first = _vector_scoring_bundle(
        first_version=4,
        second_version=3,
        finalized_at=NOW + timedelta(hours=1),
    )
    second = _vector_scoring_bundle(
        first_version=5,
        second_version=1,
        finalized_at=NOW + timedelta(hours=2),
    )

    assert repository.insert_or_get_scoring_result(first) == first
    assert repository.insert_or_get_scoring_result(second) == second
    assert repository.get_scoring_result("attempt_vector") == second
    assert (
        repository.get_scoring_result_for_audit(
            "attempt_vector",
            "audit_b",
            1,
        )
        == first
    )


def test_m9_persists_analytics_scope_and_idempotent_review(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "workflow.db"
    repository = _repository(SQLiteM9Repository, database_path)
    analytics = _analytics()
    review = _review()

    assert repository.insert_or_get_analytics(
        analytics,
        course_id="course_1",
    ) == analytics
    assert repository.insert_or_get_review_decision(review) == review

    reloaded = SQLiteM9Repository(database_path)
    assert reloaded.get_analytics("report_1") == analytics
    assert (
        reloaded.get_latest_analytics(
            course_id="course_1",
            class_id="class_1",
            learner_id="learner_1",
        )
        == analytics
    )
    assert reloaded.get_review_decision("decision_1") == review

    conflict = review.model_copy(update={"teacher_comment": "Different decision."})
    with pytest.raises(RuntimeError, match="conflict"):
        reloaded.insert_or_get_review_decision(conflict)


def test_m9_latest_analytics_orders_mixed_offsets_by_actual_time(
    tmp_path: Path,
) -> None:
    repository = _repository(SQLiteM9Repository, tmp_path / "workflow.db")
    older = _analytics(
        report_id="report_older",
        generated_at=datetime(
            2026,
            7,
            25,
            8,
            30,
            tzinfo=timezone(timedelta(hours=8)),
        ),
    )
    newer = _analytics(
        report_id="report_newer",
        generated_at=datetime(2026, 7, 25, 1, 0, tzinfo=timezone.utc),
    )
    repository.insert_or_get_analytics(older, course_id="course_1")
    repository.insert_or_get_analytics(newer, course_id="course_1")

    assert (
        repository.get_latest_analytics(
            course_id="course_1",
            class_id="class_1",
        )
        == newer
    )


def test_m9_service_builds_course_scoped_report_ids_for_shared_class_versions(
    tmp_path: Path,
) -> None:
    repository = _repository(SQLiteM9Repository, tmp_path / "workflow.db")
    service = M9TeacherAnalyticsService(repository, object(), object())
    policy_path = _teacher_policy(tmp_path, name="teacher")
    shared_snapshot_id = "shared_class_state_v1"
    shared_class_id = "shared_class"

    course_1_state = _state_result(
        attempt_id="attempt_course_1",
        course_id="course_1",
        class_id=shared_class_id,
        state_version=1,
    ).model_copy(
        update={
            "class_state_snapshot": _state_result(
                attempt_id="attempt_course_1",
                course_id="course_1",
                class_id=shared_class_id,
                state_version=1,
            ).class_state_snapshot.model_copy(
                update={"snapshot_id": shared_snapshot_id}
            )
        }
    )
    course_2_state = _state_result(
        attempt_id="attempt_course_2",
        course_id="course_2",
        class_id=shared_class_id,
        state_version=1,
    ).model_copy(
        update={
            "class_state_snapshot": _state_result(
                attempt_id="attempt_course_2",
                course_id="course_2",
                class_id=shared_class_id,
                state_version=1,
            ).class_state_snapshot.model_copy(
                update={"snapshot_id": shared_snapshot_id}
            )
        }
    )

    first = service.build_teacher_analytics(
        knowledge_bundle=_m5_knowledge("course_1"),
        scoring_result_bundle=_m5_scoring_bundle(
            attempt_id="attempt_course_1",
            course_id="course_1",
            class_id=shared_class_id,
            latest_audit_version=1,
        ),
        state_update_result=course_1_state,
        teacher_threshold_policy_path=policy_path,
    )
    second = service.build_teacher_analytics(
        knowledge_bundle=_m5_knowledge("course_2"),
        scoring_result_bundle=_m5_scoring_bundle(
            attempt_id="attempt_course_2",
            course_id="course_2",
            class_id=shared_class_id,
            latest_audit_version=1,
        ),
        state_update_result=course_2_state,
        teacher_threshold_policy_path=policy_path,
    )

    assert first.report_id != second.report_id
    assert "course_1" in first.report_id
    assert "course_2" in second.report_id
    assert repository.get_analytics(first.report_id) == first
    assert repository.get_analytics(second.report_id) == second


def test_recovery_getters_remain_compatible_with_legacy_object_repositories() -> None:
    assert M4TaskOrchestrationService(object(), lambda _: "unused").get_task_plan(
        "missing"
    ) is None
    m5 = M5StateService(object(), object(), object())
    assert m5.get_state_update("missing") is None
    assert (
        m5.get_latest_learner_state(
            "course_1",
            "class_1",
            "learner_1",
        )
        is None
    )
    m7 = M7LocalModelService(object(), object(), object())
    assert m7.get_feedback("missing") is None
    assert m7.get_feedback_for_task("task_1", "learner_1") is None
    m8 = M8AssessmentService(object(), object(), object())
    assert m8.get_paper("missing") is None
    assert m8.get_scoring_result("missing") is None
    m9 = M9TeacherAnalyticsService(object(), object(), object())
    assert m9.get_analytics("missing") is None
    assert (
        m9.get_latest_analytics(
            course_id="course_1",
            class_id="class_1",
        )
        is None
    )
    assert m9.get_review_decision("missing") is None
