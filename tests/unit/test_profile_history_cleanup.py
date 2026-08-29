from __future__ import annotations

import sqlite3
from pathlib import Path

from course_insight.modules.m0_platform.django_app.profile_history_cleanup import (
    cleanup_runtime_history,
)


def test_cleanup_keeps_only_completed_profile_papers(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE m8_assessment_papers(paper_id TEXT, task_id TEXT);
            CREATE TABLE m8_frozen_assessment_records(paper_id TEXT);
            CREATE TABLE m8_scoring_results(
                attempt_id TEXT, paper_id TEXT, payload TEXT
            );
            CREATE TABLE m8_score_audits(payload TEXT);
            CREATE TABLE m0_assessment_runs(
                paper_id TEXT, task_id TEXT, attempt_id TEXT
            );
            CREATE TABLE m7_student_feedback(task_id TEXT);
            CREATE TABLE m4_task_plans(task_id TEXT);
            """
        )
        for paper_id, task_id, attempt_id in (
            ("paper_profile", "task_profile", "attempt_profile"),
            ("paper_practice", "task_practice", "attempt_practice"),
            ("paper_unfinished", "task_unfinished", None),
        ):
            connection.execute(
                "INSERT INTO m8_assessment_papers VALUES (?, ?)",
                (paper_id, task_id),
            )
            connection.execute(
                "INSERT INTO m8_frozen_assessment_records VALUES (?)",
                (paper_id,),
            )
            connection.execute(
                "INSERT INTO m4_task_plans VALUES (?)",
                (task_id,),
            )
            connection.execute(
                "INSERT INTO m0_assessment_runs VALUES (?, ?, ?)",
                (paper_id, task_id, attempt_id),
            )
            connection.execute(
                "INSERT INTO m7_student_feedback VALUES (?)",
                (task_id,),
            )
            if attempt_id is not None:
                connection.execute(
                    "INSERT INTO m8_scoring_results VALUES (?, ?, ?)",
                    (attempt_id, paper_id, '{}'),
                )
                connection.execute(
                    "INSERT INTO m8_score_audits VALUES (?)",
                    ('{"attempt_id":"' + attempt_id + '"}',),
                )

    preview = cleanup_runtime_history(
        database_path=database_path,
        keep_paper_ids=frozenset({"paper_profile"}),
        apply=False,
    )
    assert preview["removed_papers"] == 2

    result = cleanup_runtime_history(
        database_path=database_path,
        keep_paper_ids=frozenset({"paper_profile"}),
        apply=True,
    )

    assert result["removed_papers"] == 2
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT paper_id FROM m8_assessment_papers"
        ).fetchall() == [("paper_profile",)]
        assert connection.execute(
            "SELECT paper_id FROM m8_frozen_assessment_records"
        ).fetchall() == [("paper_profile",)]
        assert connection.execute(
            "SELECT task_id FROM m4_task_plans"
        ).fetchall() == [("task_profile",)]
        assert connection.execute(
            "SELECT json_extract(payload, '$.attempt_id') FROM m8_score_audits"
        ).fetchall() == [("attempt_profile",)]
