from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from course_insight.contracts.knowledge import AssessmentBlueprint, KnowledgeBundle
from course_insight.contracts.tasking import TaskPlan
from course_insight.infrastructure.sqlite import connect_sqlite
from course_insight.infrastructure.sqlite.migrations import (
    SCHEMA_VERSION,
    current_schema_version,
    migrate,
)
from course_insight.infrastructure.sqlite import migrations as sqlite_migrations
from course_insight.modules.m4_task_orchestration.identity import (
    canonical_idempotency_key,
)
from course_insight.modules.m4_task_orchestration.intent import IntentStatus
from course_insight.modules.m4_task_orchestration.intent_service import (
    StoredIntentDecision,
)
from course_insight.modules.m4_task_orchestration.service import (
    M4TaskOrchestrationService,
)


NOW = datetime(2026, 7, 21, tzinfo=timezone.utc)


def _repository_type() -> type[Any]:
    try:
        module = importlib.import_module(
            "course_insight.infrastructure.sqlite.m4_repository"
        )
    except ImportError as error:
        pytest.fail(f"SQLiteM4Repository is unavailable: {error}")
    return module.SQLiteM4Repository


def _blueprint(blueprint_id: str) -> AssessmentBlueprint:
    return AssessmentBlueprint(
        blueprint_id=blueprint_id,
        version="1.0.0",
        course_id="course_1",
        sections=[],
        total_score=0.0,
        duration_minutes=30,
        status="teacher_approved",
    )


def _bundle(
    *,
    knowledge_bundle_id: str = "bundle_1",
    course_package_id: str = "package_1",
    blueprint_id: str = "blueprint_stage",
) -> KnowledgeBundle:
    return KnowledgeBundle(
        knowledge_bundle_id=knowledge_bundle_id,
        course_package_id=course_package_id,
        course_id="course_1",
        bundle_version="1.0.0",
        concepts=[],
        prerequisite_relations=[],
        misconception_tags=[],
        items=[],
        rubrics=[],
        blueprints=[_blueprint(blueprint_id)],
        q_matrix=[],
        status="published",
        published_at=NOW,
    )


def _service(database_path: Path) -> M4TaskOrchestrationService:
    repository = _repository_type()(database_path)
    repository.initialize()
    return M4TaskOrchestrationService(
        repository,
        canonical_idempotency_key,
    )


def _create(
    service: M4TaskOrchestrationService,
    *,
    student_text: str = "start stage assessment",
    task_type_hint: str = "stage_assessment",
    session_id: str = "session_1",
    knowledge_bundle: KnowledgeBundle | None = None,
) -> TaskPlan:
    return service.create_task_plan(
        student_text=student_text,
        task_type_hint=task_type_hint,
        course_id="course_1",
        class_id="class_1",
        learner_id="pseudonym_learner",
        session_id=session_id,
        knowledge_bundle=knowledge_bundle or _bundle(),
        learner_state_snapshot=None,
    )


def _row_count(database_path: Path) -> int:
    with connect_sqlite(database_path) as connection:
        return int(
            connection.execute("SELECT COUNT(*) FROM m4_task_plans").fetchone()[0]
        )


def _decision(
    *,
    request_key: str = "request-key",
    adapter_version: str = "adapter-v1",
    student_text: str = "原始文本",
    decision_status: IntentStatus = IntentStatus.ACCEPTED,
) -> StoredIntentDecision:
    accepted = decision_status is IntentStatus.ACCEPTED
    return StoredIntentDecision(
        request_key=request_key,
        resolved_task_type="practice" if accepted else None,
        decision_status=decision_status,
        decision_source="active_model" if accepted else "refusal",
        adapter_id="test-adapter",
        adapter_version=adapter_version,
        policy_version="intent-policy-v1",
        confidence=0.91 if accepted else None,
        margin=0.31 if accepted else None,
        input_checksum=hashlib.sha256(student_text.encode("utf-8")).hexdigest(),
        reason_codes=("model_accepted",) if accepted else ("unsupported_hint",),
        created_at=NOW,
        shadow_label="qa" if accepted else None,
        shadow_status=IntentStatus.ACCEPTED if accepted else None,
        shadow_adapter_id="shadow-adapter" if accepted else None,
        shadow_adapter_version="shadow-v1" if accepted else None,
        shadow_confidence=0.82 if accepted else None,
        shadow_margin=0.22 if accepted else None,
        shadow_reason_codes=("shadow_accepted",) if accepted else (),
        shadow_agrees=False if accepted else None,
        _generate_checksum=True,
    )


def _legacy_v1_integer_score_checksum(
    decision: StoredIntentDecision,
) -> str:
    payload = decision.canonical_payload()
    for field_name in ("confidence", "margin"):
        value = payload[field_name]
        if type(value) is float and value.is_integer():
            payload[field_name] = int(value)
    shadow = payload["shadow"]
    if type(shadow) is dict:
        for field_name in ("confidence", "margin"):
            value = shadow[field_name]
            if type(value) is float and value.is_integer():
                shadow[field_name] = int(value)
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _intent_row_count(database_path: Path) -> int:
    with connect_sqlite(database_path) as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM m4_intent_decisions"
            ).fetchone()[0]
        )


def test_sqlite_current_ledger_repairs_missing_m4_migrations_repeatably(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository_type()(database_path)
    repository.initialize()
    _create(M4TaskOrchestrationService(repository, canonical_idempotency_key))

    with connect_sqlite(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TABLE m4_intent_decisions")
        connection.execute(
            "DELETE FROM schema_migrations WHERE version IN (10, 11)"
        )
        connection.execute("COMMIT")
        assert current_schema_version(connection) == SCHEMA_VERSION

        migrate(connection)
        migrate(connection)

        assert current_schema_version(connection) == SCHEMA_VERSION
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 10"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 11"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM m4_task_plans"
        ).fetchone()[0] == 1
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info('m4_intent_decisions')"
            ).fetchall()
        }
        assert "student_text" not in columns
        assert columns == {
            "request_key",
            "resolved_task_type",
            "decision_status",
            "decision_source",
            "adapter_id",
            "adapter_version",
            "policy_version",
            "confidence",
            "margin",
            "input_checksum",
            "reason_codes_json",
            "shadow_json",
            "schema_version",
            "payload_checksum",
            "created_at",
        }
    assert SCHEMA_VERSION >= 10


def test_sqlite_current_ledger_repairs_v11_and_preserves_m4_rows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository_type()(database_path)
    repository.initialize()
    accepted = repository.insert_or_get_intent_decision(_decision())

    with connect_sqlite(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "ALTER TABLE m4_intent_decisions RENAME TO m4_intent_decisions_v11"
        )
        connection.execute(sqlite_migrations._M4_INTENT_DECISION_V10_SQL)
        connection.execute(
            """
            INSERT INTO m4_intent_decisions
            SELECT * FROM m4_intent_decisions_v11
            """
        )
        connection.execute("DROP TABLE m4_intent_decisions_v11")
        connection.execute("DELETE FROM schema_migrations WHERE version = 11")
        connection.execute("COMMIT")
        assert current_schema_version(connection) == SCHEMA_VERSION

        migrate(connection)
        migrate(connection)

        assert current_schema_version(connection) == SCHEMA_VERSION
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 11"
        ).fetchone()[0] == 1

    restarted = _repository_type()(database_path)
    assert restarted.get_intent_decision(accepted.request_key) == accepted
    for status in (IntentStatus.UNAVAILABLE, IntentStatus.FAILED):
        candidate = _decision(
            request_key=f"request-{status.value}",
            decision_status=status,
        )
        assert restarted.insert_or_get_intent_decision(candidate) == candidate

    assert SCHEMA_VERSION == 19


@pytest.mark.parametrize(
    ("update_sql", "parameters"),
    [
        ("request_key = ?", (" ",)),
        ("decision_status = ?", ("unknown",)),
        ("resolved_task_type = ?", ("essay",)),
        ("decision_source = ?", (" ",)),
        ("adapter_id = ?", (" ",)),
        ("adapter_version = ?", (" ",)),
        ("policy_version = ?", (" ",)),
        ("confidence = ?", (1.1,)),
        ("margin = ?", (-0.1,)),
        ("input_checksum = ?", ("f" * 63,)),
        ("input_checksum = ?", ("A" * 64,)),
        ("reason_codes_json = ?", ("{}",)),
        ("reason_codes_json = ?", ('[ "noncanonical" ]',)),
        ("shadow_json = ?", ("[]",)),
        ("schema_version = ?", (2,)),
        ("payload_checksum = ?", ("f" * 63,)),
        ("payload_checksum = ?", ("A" * 64,)),
        ("created_at = ?", ("2026-07-21T00:00:00",)),
        ("created_at = ?", ("not-a-date+00:00",)),
        (
            "decision_status = ?, resolved_task_type = ?",
            ("invalid", None),
        ),
        ("resolved_task_type = ?", (None,)),
        ("decision_source = ?", ("refusal",)),
        ("confidence = ?", (None,)),
    ],
)
def test_sqlite_intent_decision_check_constraints(
    tmp_path: Path,
    update_sql: str,
    parameters: tuple[object, ...],
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository_type()(database_path)
    repository.initialize()
    stored = repository.insert_or_get_intent_decision(_decision())

    with connect_sqlite(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"UPDATE m4_intent_decisions SET {update_sql}",
                parameters,
            )
        row = connection.execute(
            "SELECT payload_checksum FROM m4_intent_decisions"
        ).fetchone()
        assert row["payload_checksum"] == stored.payload_checksum


def test_sqlite_repository_rejects_semantically_equal_noncanonical_reason_json(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository_type()(database_path)
    repository.initialize()
    repository.insert_or_get_intent_decision(_decision())
    with connect_sqlite(database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            """
            UPDATE m4_intent_decisions
            SET reason_codes_json = ?
            WHERE request_key = ?
            """,
            ('[ "model_accepted" ]', "request-key"),
        )

    with pytest.raises(RuntimeError, match="corrupt"):
        repository.get_intent_decision("request-key")


def test_sqlite_repository_rejects_canonical_non_string_reason_codes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository_type()(database_path)
    repository.initialize()
    repository.insert_or_get_intent_decision(_decision())
    with connect_sqlite(database_path) as connection:
        connection.execute(
            """
            UPDATE m4_intent_decisions
            SET reason_codes_json = ?
            WHERE request_key = ?
            """,
            ("[1]", "request-key"),
        )

    with pytest.raises(RuntimeError, match="corrupt"):
        repository.get_intent_decision("request-key")


def test_sqlite_repository_rejects_canonical_semantically_invalid_shadow_json(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository_type()(database_path)
    repository.initialize()
    repository.insert_or_get_intent_decision(_decision())
    invalid_shadow = (
        '{"adapter_id":"shadow-adapter","adapter_version":"shadow-v1",'
        '"agrees":false,"confidence":0.82,"label":"qa","margin":0.22,'
        '"reason_codes":[1],"status":"accepted"}'
    )
    with connect_sqlite(database_path) as connection:
        connection.execute(
            """
            UPDATE m4_intent_decisions
            SET shadow_json = ?
            WHERE request_key = ?
            """,
            (invalid_shadow, "request-key"),
        )

    with pytest.raises(RuntimeError, match="corrupt"):
        repository.get_intent_decision("request-key")


def test_sqlite_driver_binds_nan_as_null_for_nullable_intent_scores(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository_type()(database_path)
    repository.initialize()
    repository.insert_or_get_intent_decision(
        _decision(decision_status=IntentStatus.INVALID)
    )

    with connect_sqlite(database_path) as connection:
        connection.execute(
            """
            UPDATE m4_intent_decisions
            SET confidence = ?
            WHERE request_key = ?
            """,
            (float("nan"), "request-key"),
        )
        row = connection.execute(
            """
            SELECT confidence, typeof(confidence) AS confidence_type
            FROM m4_intent_decisions
            WHERE request_key = ?
            """,
            ("request-key",),
        ).fetchone()

    assert row["confidence"] is None
    assert row["confidence_type"] == "null"


def test_stored_intent_decision_rejects_nan_before_repository_write() -> None:
    with pytest.raises(ValueError, match="finite probability"):
        replace(
            _decision(),
            confidence=float("nan"),
            payload_checksum=None,
            _generate_checksum=True,
        )


def test_sqlite_repository_rejects_a_corrupted_nan_before_connecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    module = importlib.import_module(
        "course_insight.infrastructure.sqlite.m4_repository"
    )
    repository = module.SQLiteM4Repository(database_path)
    repository.initialize()
    corrupted = _decision()
    object.__setattr__(corrupted, "confidence", float("nan"))
    connection_attempts = 0

    def fail_if_connected(path: Path) -> Any:
        del path
        nonlocal connection_attempts
        connection_attempts += 1
        raise AssertionError("non-finite candidate reached SQLite")

    monkeypatch.setattr(module, "connect_sqlite", fail_if_connected)

    with pytest.raises(ValueError, match="intent decision is invalid"):
        repository.insert_or_get_intent_decision(corrupted)

    assert connection_attempts == 0


def test_sqlite_intent_decision_round_trip_preserves_private_metadata(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository_type()(database_path)
    repository.initialize()
    candidate = _decision()

    inserted = repository.insert_or_get_intent_decision(candidate)
    restored = _repository_type()(database_path).get_intent_decision(
        candidate.request_key
    )

    assert inserted == candidate
    assert inserted is not candidate
    assert restored == candidate
    assert restored is not inserted
    assert restored.created_at.isoformat().endswith("+00:00")
    assert restored.shadow_payload() == candidate.shadow_payload()
    assert _intent_row_count(database_path) == 1
    with connect_sqlite(database_path) as connection:
        database_dump = "\n".join(connection.iterdump())
    assert "原始文本" not in database_dump


def test_sqlite_reads_raw_legacy_v1_integer_score_checksum(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository_type()(database_path)
    repository.initialize()
    baseline = _decision()
    candidate = StoredIntentDecision(
        request_key=baseline.request_key,
        resolved_task_type=baseline.resolved_task_type,
        decision_status=baseline.decision_status,
        decision_source=baseline.decision_source,
        adapter_id=baseline.adapter_id,
        adapter_version=baseline.adapter_version,
        policy_version=baseline.policy_version,
        confidence=1,
        margin=0,
        input_checksum=baseline.input_checksum,
        reason_codes=baseline.reason_codes,
        created_at=baseline.created_at,
        shadow_label=baseline.shadow_label,
        shadow_status=baseline.shadow_status,
        shadow_adapter_id=baseline.shadow_adapter_id,
        shadow_adapter_version=baseline.shadow_adapter_version,
        shadow_confidence=1,
        shadow_margin=0,
        shadow_reason_codes=baseline.shadow_reason_codes,
        shadow_agrees=baseline.shadow_agrees,
        _generate_checksum=True,
    )
    repository.insert_or_get_intent_decision(candidate)
    legacy_checksum = _legacy_v1_integer_score_checksum(candidate)
    assert legacy_checksum != candidate.payload_checksum
    with connect_sqlite(database_path) as connection:
        connection.execute(
            """
            UPDATE m4_intent_decisions
            SET payload_checksum = ?
            WHERE request_key = ?
            """,
            (legacy_checksum, candidate.request_key),
        )

    restored = _repository_type()(database_path).get_intent_decision(
        candidate.request_key
    )

    assert restored is not None
    assert restored.payload_checksum == legacy_checksum
    assert restored.confidence == 1.0
    assert restored.margin == 0.0


def test_sqlite_rejects_fresh_legacy_checksum_before_opening_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    module = importlib.import_module(
        "course_insight.infrastructure.sqlite.m4_repository"
    )
    repository = module.SQLiteM4Repository(database_path)
    repository.initialize()
    baseline = _decision()
    current = StoredIntentDecision(
        request_key=baseline.request_key,
        resolved_task_type=baseline.resolved_task_type,
        decision_status=baseline.decision_status,
        decision_source=baseline.decision_source,
        adapter_id=baseline.adapter_id,
        adapter_version=baseline.adapter_version,
        policy_version=baseline.policy_version,
        confidence=1,
        margin=0,
        input_checksum=baseline.input_checksum,
        reason_codes=baseline.reason_codes,
        created_at=baseline.created_at,
        shadow_label=baseline.shadow_label,
        shadow_status=baseline.shadow_status,
        shadow_adapter_id=baseline.shadow_adapter_id,
        shadow_adapter_version=baseline.shadow_adapter_version,
        shadow_confidence=1,
        shadow_margin=0,
        shadow_reason_codes=baseline.shadow_reason_codes,
        shadow_agrees=baseline.shadow_agrees,
        _generate_checksum=True,
    )
    legacy = replace(
        current,
        payload_checksum=_legacy_v1_integer_score_checksum(current),
    )
    connection_attempts = 0

    def fail_if_connected(path: Path) -> Any:
        del path
        nonlocal connection_attempts
        connection_attempts += 1
        raise AssertionError("fresh invalid candidate reached SQLite")

    monkeypatch.setattr(module, "connect_sqlite", fail_if_connected)

    with pytest.raises(ValueError, match="intent decision is invalid"):
        repository.insert_or_get_intent_decision(legacy)

    assert connection_attempts == 0


def test_sqlite_intent_decision_first_writer_wins_across_adapter_versions(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository_type()(database_path)
    repository.initialize()
    first = _decision(adapter_version="adapter-v1")
    later = _decision(adapter_version="adapter-v2")

    inserted = repository.insert_or_get_intent_decision(first)
    replayed = repository.insert_or_get_intent_decision(later)

    assert inserted == first
    assert replayed == first
    assert replayed.payload_checksum != later.payload_checksum
    assert _intent_row_count(database_path) == 1


def test_sqlite_refusal_intent_decision_is_replayed(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository_type()(database_path)
    repository.initialize()
    refusal = _decision(decision_status=IntentStatus.INVALID)

    inserted = repository.insert_or_get_intent_decision(refusal)
    replayed = _repository_type()(database_path).insert_or_get_intent_decision(
        _decision(
            adapter_version="adapter-v2",
            decision_status=IntentStatus.INVALID,
        )
    )

    assert inserted == refusal
    assert replayed == refusal
    assert replayed.decision_status is IntentStatus.INVALID
    assert replayed.resolved_task_type is None
    assert _intent_row_count(database_path) == 1


def test_sqlite_rejects_tampered_intent_payload_checksum(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository_type()(database_path)
    repository.initialize()
    repository.insert_or_get_intent_decision(_decision())

    with connect_sqlite(database_path) as connection:
        connection.execute(
            """
            UPDATE m4_intent_decisions
            SET resolved_task_type = 'qa'
            """
        )

    with pytest.raises(RuntimeError, match="corrupt"):
        repository.get_intent_decision("request-key")


def test_sqlite_v10_intent_decision_replay_is_atomic(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository_type()(database_path)
    repository.initialize()

    def insert_once(index: int) -> StoredIntentDecision:
        return _repository_type()(database_path).insert_or_get_intent_decision(
            _decision(
                request_key="same-key",
                adapter_version=f"adapter-v{index}",
            )
        )

    with ThreadPoolExecutor(max_workers=12) as executor:
        decisions = list(executor.map(insert_once, range(12)))

    assert len({item.payload_checksum for item in decisions}) == 1
    assert len({item.adapter_version for item in decisions}) == 1
    assert _intent_row_count(database_path) == 1
    with connect_sqlite(database_path) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS row_count, MAX(input_checksum) AS input_checksum
            FROM m4_intent_decisions
            """
        ).fetchone()
        database_dump = "\n".join(connection.iterdump())
    assert row["row_count"] == 1
    assert row["input_checksum"] == _decision(
        request_key="same-key"
    ).input_checksum
    assert "原始文本" not in database_dump


def test_sqlite_repository_persists_and_restores_task_plan(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    service = _service(database_path)

    plan = _create(service)
    restored = _repository_type()(database_path).get_task_plan(plan.task_id)

    assert restored == plan
    assert restored is not plan
    assert restored.content_checksum() == plan.content_checksum()
    assert _row_count(database_path) == 1


def test_replay_returns_first_task_id_timestamp_and_payload(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    service = _service(database_path)

    first = _create(service, student_text="start stage assessment")
    replay = _create(service, student_text="please begin the assessment")

    assert replay.task_id == first.task_id
    assert replay.created_at == first.created_at
    assert replay.content_checksum() == first.content_checksum()
    assert _row_count(database_path) == 1


def test_new_service_instance_reuses_persisted_task(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    first = _create(_service(database_path))

    replay = _create(_service(database_path), student_text="begin assessment again")

    assert replay == first
    assert _row_count(database_path) == 1


def test_authoritative_identity_changes_create_distinct_tasks(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    service = _service(database_path)

    plans = [
        _create(service),
        _create(service, session_id="session_2"),
        _create(service, task_type_hint="practice"),
        _create(
            service,
            knowledge_bundle=_bundle(knowledge_bundle_id="bundle_2"),
        ),
        _create(
            service,
            knowledge_bundle=_bundle(course_package_id="package_2"),
        ),
        _create(
            service,
            knowledge_bundle=_bundle(blueprint_id="blueprint_other"),
        ),
    ]

    assert len({plan.task_id for plan in plans}) == len(plans)
    assert _row_count(database_path) == len(plans)


def test_insert_or_get_never_overwrites_first_payload(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    service = _service(database_path)
    first = _create(service)
    repository = _repository_type()(database_path)
    changed_candidate = first.model_copy(
        update={"created_at": first.created_at + timedelta(days=1)},
        deep=True,
    )
    key = first.task_id.removeprefix("task_")

    winner = repository.insert_or_get_task_plan(changed_candidate, key)

    assert winner == first
    assert winner.created_at != changed_candidate.created_at
    assert _row_count(database_path) == 1


def test_repository_rejects_task_id_and_idempotency_key_mismatch(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository_type()(database_path)
    repository.initialize()
    service = M4TaskOrchestrationService(repository, canonical_idempotency_key)
    candidate = _create(service).model_copy(
        update={"task_id": "task_not_derived_from_the_supplied_key"},
        deep=True,
    )

    with pytest.raises(ValueError, match="task identity"):
        repository.insert_or_get_task_plan(candidate, "b" * 64)

    assert _row_count(database_path) == 1


def test_twenty_concurrent_creates_return_one_authoritative_plan(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime" / "course_insight.sqlite3"
    repository = _repository_type()(database_path)
    repository.initialize()

    def create_once(_: int) -> TaskPlan:
        service = M4TaskOrchestrationService(
            _repository_type()(database_path),
            canonical_idempotency_key,
        )
        return _create(service)

    with ThreadPoolExecutor(max_workers=20) as executor:
        plans = list(executor.map(create_once, range(20)))

    assert len({plan.task_id for plan in plans}) == 1
    assert len({plan.created_at for plan in plans}) == 1
    assert len({plan.content_checksum() for plan in plans}) == 1
    assert _row_count(database_path) == 1
