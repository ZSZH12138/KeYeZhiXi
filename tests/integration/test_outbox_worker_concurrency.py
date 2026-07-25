from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from multiprocessing.synchronize import Event as ProcessEvent
from pathlib import Path
from queue import Empty

from course_insight.contracts.events import LearningEvent
from course_insight.infrastructure.config import OutboxSettings
from course_insight.infrastructure.sqlite import connect_sqlite
from course_insight.infrastructure.sqlite.m0_repository import SQLiteM0Repository
from course_insight.modules.m0_platform.outbox import IdempotentJsonlSink
from course_insight.modules.m0_platform.outbox_worker import OutboxWorker


NOW = datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)


def _append_in_subprocess(
    path: str,
    event_id: str,
    serialized_record: str,
    start: ProcessEvent,
    results: object,
) -> None:
    try:
        start.wait(timeout=10)
        appended = IdempotentJsonlSink(Path(path)).append_if_absent(
            event_id,
            serialized_record,
        )
        results.put(("ok", appended))
    except Exception as error:
        results.put(("error", type(error).__name__))


def _event(index: int) -> LearningEvent:
    return LearningEvent(
        event_id=f"event_{index:03d}",
        event_type="assessment_scored",
        course_id="course_1",
        class_id="class_1",
        learner_id="learner_1",
        attempt_id=f"attempt_{index}",
        payload={"score": index},
        occurred_at=NOW,
    )


def _settings() -> OutboxSettings:
    return OutboxSettings(
        batch_size=100,
        poll_interval_seconds=0.01,
        lease_seconds=30,
        max_retries=3,
        retry_base_seconds=1,
        retry_max_seconds=10,
        retry_jitter_ratio=0,
        heartbeat_interval_seconds=5,
    )


def test_two_workers_claim_each_event_once_and_share_idempotent_sink(
    tmp_path: Path,
) -> None:
    repository = SQLiteM0Repository(
        tmp_path / "runtime" / "app.sqlite3",
        outbox_clock=lambda: NOW,
    )
    repository.initialize()
    repository.append_events([_event(index) for index in range(20)])
    sink_path = tmp_path / "runtime" / "audit" / "events.jsonl"

    def run(worker_id: str):
        return OutboxWorker(
            repository=repository,
            sink=IdempotentJsonlSink(sink_path),
            settings=_settings(),
            status_path=tmp_path / f"{worker_id}.json",
            worker_id=worker_id,
            clock=lambda: NOW,
            jitter=lambda: 0.5,
        ).run(once=True)

    with ThreadPoolExecutor(max_workers=2) as executor:
        snapshots = list(executor.map(run, ("worker_a", "worker_b")))

    assert sum(item.claimed_count for item in snapshots) == 20
    assert sum(item.delivered_count for item in snapshots) == 20
    lines = sink_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 20
    assert len(set(lines)) == 20
    with connect_sqlite(repository._database_path) as connection:  # noqa: SLF001
        assert connection.execute(
            "SELECT COUNT(*) FROM m0_event_outbox"
        ).fetchone()[0] == 0


def test_concurrent_claim_transactions_do_not_double_claim(
    tmp_path: Path,
) -> None:
    repository = SQLiteM0Repository(
        tmp_path / "runtime" / "app.sqlite3",
        outbox_clock=lambda: NOW,
    )
    repository.initialize()
    repository.append_events([_event(index) for index in range(10)])

    def claim(worker_id: str):
        return repository.claim_outbox_batch(
            worker_id,
            now=NOW,
            lease_until=NOW + timedelta(seconds=30),
            batch_size=10,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        batches = list(executor.map(claim, ("worker_a", "worker_b")))

    claimed_ids = [
        record.event_id
        for batch in batches
        for record in batch
    ]
    assert len(claimed_ids) == 10
    assert len(set(claimed_ids)) == 10


def test_jsonl_sink_is_idempotent_across_processes(tmp_path: Path) -> None:
    import multiprocessing

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    path = tmp_path / "audit" / "events.jsonl"
    serialized = (
        '{"event_id":"event_cross_process","value":1}'
    )
    processes = [
        context.Process(
            target=_append_in_subprocess,
            args=(
                str(path),
                "event_cross_process",
                serialized,
                start,
                results,
            ),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    try:
        outcomes = [results.get(timeout=5) for _ in processes]
    except Empty:
        raise AssertionError("sink subprocess returned no result") from None

    assert sorted(outcomes) == [("ok", False), ("ok", True)]
    assert path.read_text(encoding="utf-8").splitlines() == [serialized]
