"""Task 7 policy binding and decision concurrency regressions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from course_insight.contracts.errors import DomainError
from course_insight.contracts.tutoring import TutoringControlResult
from tests.integration.test_m6_persistence import (
    _database_counts,
    _inputs,
    _repository,
    _repository_type,
    _service,
    _snapshot,
)


def test_same_request_concurrency_freezes_one_new_execution_and_decision(
    tmp_path: Path,
) -> None:
    """Catch retries creating multiple policy identities or public decisions."""

    database_path = tmp_path / "same.sqlite3"
    _repository(database_path)

    def decide(_: int) -> tuple[str, str]:
        repository = _repository_type()(database_path)
        service = _service(repository)
        execution = service.prepare_policy_execution(*_inputs(), _snapshot(0))
        result = service.decide_next_action(*_inputs(), _snapshot(0))
        return execution.policy_execution_fingerprint, result.content_checksum()

    with ThreadPoolExecutor(max_workers=12) as executor:
        outcomes = tuple(executor.map(decide, range(12)))

    assert len({execution for execution, _ in outcomes}) == 1
    assert len({result for _, result in outcomes}) == 1
    assert _database_counts(database_path) == (2, 1)


def test_competing_requests_commit_only_one_authoritative_input(
    tmp_path: Path,
) -> None:
    """Catch competing cursors replacing the first authoritative M6 decision."""

    database_path = tmp_path / "competing.sqlite3"
    _repository(database_path)
    previous = _snapshot(0)

    def decide(index: int) -> TutoringControlResult | DomainError:
        repository = _repository_type()(database_path)
        try:
            return _service(repository).decide_next_action(
                *_inputs(1 if index % 2 == 0 else 2),
                previous.model_copy(deep=True),
            )
        except DomainError as error:
            return error

    with ThreadPoolExecutor(max_workers=12) as executor:
        outcomes = tuple(executor.map(decide, range(12)))

    accepted = tuple(
        outcome
        for outcome in outcomes
        if isinstance(outcome, TutoringControlResult)
    )
    rejected = tuple(
        outcome for outcome in outcomes if isinstance(outcome, DomainError)
    )
    assert len(accepted) == 6
    assert len(rejected) == 6
    assert len({result.content_checksum() for result in accepted}) == 1
    assert {error.code for error in rejected} == {"TUTORING_REFERENCE_MISMATCH"}
    assert _database_counts(database_path) == (2, 1)
