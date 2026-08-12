"""M8 restart recovery retains scope, rubric, and real-time behavior."""

from datetime import datetime, timezone

from course_insight.infrastructure.sqlite.m8_repository import SQLiteM8Repository
from course_insight.modules.m8_assessment_scoring.paper_generator import PaperGenerator
from course_insight.modules.m8_assessment_scoring.rule_scorer import RuleScorer
from course_insight.modules.m8_assessment_scoring.service import M8AssessmentService
from tests.factories.m5_m8 import (
    make_knowledge_bundle,
    make_submission,
    make_task_plan,
)


def _service(repository: SQLiteM8Repository) -> M8AssessmentService:
    return M8AssessmentService(repository, RuleScorer(), PaperGenerator())


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
