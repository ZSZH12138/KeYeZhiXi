from __future__ import annotations

import json

from course_insight.infrastructure.sqlite.class_erasure import (
    purge_sqlite_class_scope,
)
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.migrations import migrate


def _payload(course_id: str, class_id: str) -> str:
    return json.dumps(
        {"course_id": course_id, "class_id": class_id},
        separators=(",", ":"),
    )


def test_class_scope_erasure_removes_runtime_data_without_touching_another_class(
    tmp_path,
) -> None:
    database_path = tmp_path / "class-erasure.sqlite3"
    connection = connect_sqlite(database_path)
    target_course = "course_target"
    target_class = "class_target"
    other_course = "course_other"
    other_class = "class_other"
    target_payload = _payload(target_course, target_class)
    other_payload = _payload(other_course, other_class)
    try:
        migrate(connection)
        connection.executemany(
            """
            INSERT INTO m5_class_states(
                snapshot_id, course_id, class_id, state_version,
                aggregation_policy_version, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ("m5-target", target_course, target_class, 1, "policy", target_payload),
                ("m5-other", other_course, other_class, 1, "policy", other_payload),
            ),
        )
        connection.executemany(
            """
            INSERT INTO m8_assessment_papers(
                paper_id, task_id, course_id, class_id, learner_id, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ("paper-target", "task-target", target_course, target_class, "pseudonym_target", target_payload),
                ("paper-other", "task-other", other_course, other_class, "pseudonym_other", other_payload),
            ),
        )
        connection.executemany(
            """
            INSERT INTO m7_student_feedback(feedback_id, task_id, learner_id, payload)
            VALUES (?, ?, ?, ?)
            """,
            (
                ("feedback-target", "task-target", "pseudonym_target", target_payload),
                ("feedback-other", "task-other", "pseudonym_other", other_payload),
            ),
        )
        connection.executemany(
            """
            INSERT INTO m4_task_plans(task_id, idempotency_key, payload)
            VALUES (?, ?, ?)
            """,
            (
                ("task-target", "request-target", target_payload),
                ("task-other", "request-other", other_payload),
            ),
        )
        connection.executemany(
            """
            INSERT INTO m6_session_states(session_id, turn_count, payload)
            VALUES (?, ?, ?)
            """,
            (
                ("session-target", 0, target_payload),
                ("session-other", 0, other_payload),
            ),
        )
        connection.executemany(
            """
            INSERT INTO m9_teacher_analytics(
                report_id, course_id, class_id, generated_at, learner_ids, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ("report-target", target_course, target_class, "2026-08-30T00:00:00+00:00", '["pseudonym_target"]', target_payload),
                ("report-other", other_course, other_class, "2026-08-30T00:00:00+00:00", '["pseudonym_other"]', other_payload),
            ),
        )
        connection.executemany(
            """
            INSERT INTO m9_model_invocation_audits(
                invocation_id, request_id, source_report_id,
                source_report_checksum, scope, provider, model_name,
                provider_status, validation_status, created_at, payload,
                payload_checksum
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ("audit-target", "audit-request-target", "report-target", "a" * 64, "class_aggregate", "deepseek", "model", "not_run", "not_run", "2026-08-30T00:00:00+00:00", target_payload, "b" * 64),
                ("audit-other", "audit-request-other", "report-other", "c" * 64, "class_aggregate", "deepseek", "model", "not_run", "not_run", "2026-08-30T00:00:00+00:00", other_payload, "d" * 64),
            ),
        )
        connection.execute("BEGIN IMMEDIATE")
        result = purge_sqlite_class_scope(
            connection,
            course_id=target_course,
            class_id=target_class,
        )
        connection.execute("COMMIT")

        assert result.total_deleted >= 6
        for table, target_id, other_id in (
            ("m5_class_states", "m5-target", "m5-other"),
            ("m8_assessment_papers", "paper-target", "paper-other"),
            ("m7_student_feedback", "feedback-target", "feedback-other"),
            ("m4_task_plans", "task-target", "task-other"),
            ("m6_session_states", "session-target", "session-other"),
            ("m9_teacher_analytics", "report-target", "report-other"),
            ("m9_model_invocation_audits", "audit-target", "audit-other"),
        ):
            key = {
                "m5_class_states": "snapshot_id",
                "m8_assessment_papers": "paper_id",
                "m7_student_feedback": "feedback_id",
                "m4_task_plans": "task_id",
                "m6_session_states": "session_id",
                "m9_teacher_analytics": "report_id",
                "m9_model_invocation_audits": "invocation_id",
            }[table]
            assert connection.execute(
                f"SELECT 1 FROM {table} WHERE {key} = ?",
                (target_id,),
            ).fetchone() is None
            assert connection.execute(
                f"SELECT 1 FROM {table} WHERE {key} = ?",
                (other_id,),
            ).fetchone() is not None
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()
