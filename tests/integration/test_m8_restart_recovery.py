"""M8 restart recovery retains scope, rubric, and real-time behavior."""

from datetime import datetime, timedelta, timezone

from course_insight.infrastructure.sqlite.m8_repository import SQLiteM8Repository
from course_insight.modules.m8_assessment_scoring.paper_generator import PaperGenerator
from course_insight.modules.m8_assessment_scoring.rule_scorer import RuleScorer
from course_insight.modules.m8_assessment_scoring.service import M8AssessmentService
from tests.factories.m5_m8 import (
    FixedClock,
    UTC_TIME,
    make_knowledge_bundle,
    make_submission,
    make_task_plan,
)


def _service(repository: SQLiteM8Repository) -> M8AssessmentService:
    return M8AssessmentService(repository, RuleScorer(), PaperGenerator())


def _service_at(
    repository: SQLiteM8Repository,
    instant: datetime,
) -> M8AssessmentService:
    clock = FixedClock(instant)
    return M8AssessmentService(
        repository,
        RuleScorer(clock),
        PaperGenerator(clock),
        clock,
    )


def test_restart_recovers_frozen_rubric_and_scoring_scope(tmp_path) -> None:
    repository = SQLiteM8Repository(tmp_path / "m8.sqlite3")
    repository.initialize()
    original_bundle = make_knowledge_bundle(rubric_version="1.0.0")
    service_a = _service(repository)
    paper = service_a.generate_paper(
        make_task_plan(), original_bundle, None, None
    )

    service_b = _service(SQLiteM8Repository(tmp_path / "m8.sqlite3"))
    recovered = service_b.get_paper(paper.paper_id)
    preparation = service_b.prepare_scoring(
        recovered,
        make_submission(paper),
        make_knowledge_bundle(rubric_version="2.0.0"),
    )

    assert recovered == paper
    assert preparation.rubric_scoring_tasks[0].rubric.version == "1.0.0"
    assert repository.get_paper_record(paper.paper_id).course_id == "course_1"


def test_default_generator_clock_uses_current_utc() -> None:
    before = datetime.now(timezone.utc)
    paper = PaperGenerator().generate(
        make_task_plan(),
        make_knowledge_bundle(subjective=False),
        None,
        None,
    )
    after = datetime.now(timezone.utc)

    assert before <= paper.generated_at <= after
    assert paper.generated_at.utcoffset() == timezone.utc.utcoffset(paper.generated_at)


def test_repeated_generation_after_restart_returns_the_first_frozen_paper(
    tmp_path,
) -> None:
    database_path = tmp_path / "paper-retry.sqlite3"
    first_service = _service_at(SQLiteM8Repository(database_path), UTC_TIME)
    first_service._repository.initialize()
    knowledge = make_knowledge_bundle(subjective=False)

    first = first_service.generate_paper(
        make_task_plan(),
        knowledge,
        None,
        None,
    )
    retried = _service_at(
        SQLiteM8Repository(database_path),
        UTC_TIME + timedelta(minutes=5),
    ).generate_paper(
        make_task_plan(),
        knowledge,
        None,
        None,
    )

    assert retried == first
    assert retried.generated_at == UTC_TIME


def test_repeated_scoring_after_restart_returns_the_first_result(
    tmp_path,
) -> None:
    database_path = tmp_path / "scoring-retry.sqlite3"
    repository = SQLiteM8Repository(database_path)
    repository.initialize()
    knowledge = make_knowledge_bundle(subjective=False)
    first_service = _service_at(repository, UTC_TIME)
    paper = first_service.generate_paper(
        make_task_plan(),
        knowledge,
        None,
        None,
    )
    submission = make_submission(paper)
    first_preparation = first_service.prepare_scoring(
        paper,
        submission,
        knowledge,
    )
    first = first_service.finalize_scoring(first_preparation, [])

    retry_service = _service_at(
        SQLiteM8Repository(database_path),
        UTC_TIME + timedelta(minutes=5),
    )
    recovered = retry_service.get_paper(paper.paper_id)
    retry_preparation = retry_service.prepare_scoring(
        recovered,
        submission,
        knowledge,
    )
    retried = retry_service.finalize_scoring(retry_preparation, [])

    assert retried == first
    assert retried.finalized_at == UTC_TIME


def test_wrong_objective_answer_targets_its_frozen_concepts_for_feedback(
    tmp_path,
) -> None:
    repository = SQLiteM8Repository(tmp_path / "objective-remediation.sqlite3")
    repository.initialize()
    service = _service_at(repository, UTC_TIME)
    knowledge = make_knowledge_bundle(subjective=False)
    paper = service.generate_paper(
        make_task_plan(),
        knowledge,
        None,
        None,
    )
    instance = paper.all_items()[0]
    wrong_submission = make_submission(paper).model_copy(
        update={"answers": {instance.item_instance_id: "no"}},
        deep=True,
    )

    preparation = service.prepare_scoring(
        paper,
        wrong_submission,
        knowledge,
    )
    result = service.finalize_scoring(preparation, [])

    assert result.total_score == 0.0
    assert result.remediation_plan.target_concept_ids() == ["concept_2"]
    assert result.remediation_plan.targets[0].recommended_item_ids == ["item_2"]


def test_subjective_teacher_answer_is_rule_scored_without_model_or_review(
    tmp_path,
) -> None:
    repository = SQLiteM8Repository(tmp_path / "subjective-exact.sqlite3")
    repository.initialize()
    service = _service_at(repository, UTC_TIME)
    original = make_knowledge_bundle(subjective=True)
    item = original.items[0].model_copy(
        update={
            "answer_key": {
                "answers": ["TCP 提供端到端的可靠字节流服务。"],
                "max_score": 1.0,
            }
        },
        deep=True,
    )
    knowledge = original.model_copy(update={"items": [item]}, deep=True)
    paper = service.generate_paper(
        make_task_plan(),
        knowledge,
        None,
        None,
    )
    instance = paper.all_items()[0]
    submission = make_submission(paper).model_copy(
        update={
            "answers": {
                instance.item_instance_id: "TCP 提供端到端的可靠字节流服务。"
            }
        },
        deep=True,
    )

    preparation = service.prepare_scoring(paper, submission, knowledge)
    result = service.finalize_scoring(preparation, [])

    assert preparation.rubric_scoring_tasks == []
    assert preparation.evidence_queries == []
    assert len(preparation.objective_audit_records) == 1
    assert result.total_score == instance.max_score
    assert result.score_audit_records[0].scoring_method == "rule"
    assert result.score_audit_records[0].review_status == "not_required"
