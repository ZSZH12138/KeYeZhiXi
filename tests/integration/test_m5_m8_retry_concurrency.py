"""Concurrent retries converge on one authoritative M5/M8 history row."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier

from course_insight.infrastructure.sqlite.m5_repository import SQLiteM5Repository
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
from tests.integration.test_m5_atomic_update import _state_result


def _m8_service(database_path: Path, instant: datetime) -> M8AssessmentService:
    clock = FixedClock(instant)
    return M8AssessmentService(
        SQLiteM8Repository(database_path),
        RuleScorer(clock),
        PaperGenerator(clock),
        clock,
    )


def test_concurrent_paper_retries_return_one_frozen_paper(tmp_path) -> None:
    database_path = tmp_path / "paper-concurrency.sqlite3"
    SQLiteM8Repository(database_path).initialize()
    knowledge = make_knowledge_bundle(subjective=False)
    barrier = Barrier(2)

    def generate(offset_minutes: int):
        service = _m8_service(
            database_path,
            UTC_TIME + timedelta(minutes=offset_minutes),
        )
        barrier.wait()
        return service.generate_paper(
            make_task_plan(),
            knowledge,
            None,
            None,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        papers = list(executor.map(generate, (0, 5)))

    assert papers[0] == papers[1]
    assert SQLiteM8Repository(database_path).get_paper(papers[0].paper_id) == papers[0]


def test_concurrent_scoring_retries_return_one_result(tmp_path) -> None:
    database_path = tmp_path / "scoring-concurrency.sqlite3"
    repository = SQLiteM8Repository(database_path)
    repository.initialize()
    knowledge = make_knowledge_bundle(subjective=False)
    setup_service = _m8_service(database_path, UTC_TIME)
    paper = setup_service.generate_paper(
        make_task_plan(),
        knowledge,
        None,
        None,
    )
    submission = make_submission(paper)
    barrier = Barrier(2)

    def score(offset_minutes: int):
        service = _m8_service(
            database_path,
            UTC_TIME + timedelta(minutes=offset_minutes),
        )
        recovered = service.get_paper(paper.paper_id)
        preparation = service.prepare_scoring(recovered, submission, knowledge)
        barrier.wait()
        return service.finalize_scoring(preparation, [])

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(score, (10, 15)))

    assert results[0] == results[1]
    assert repository.get_scoring_result(results[0].attempt_id) == results[0]


def test_concurrent_state_retries_do_not_advance_class_history(tmp_path) -> None:
    database_path = tmp_path / "state-concurrency.sqlite3"
    SQLiteM5Repository(database_path).initialize()
    result = _state_result()
    barrier = Barrier(2)

    def save(_: int):
        repository = SQLiteM5Repository(database_path)
        barrier.wait()
        return repository.insert_or_get_state_update(
            result,
            expected_previous_class_snapshot_id=None,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, (0, 1)))

    repository = SQLiteM5Repository(database_path)
    assert results == [result, result]
    assert repository.get_latest_class_state_version("course_1", "class_1") == 1
