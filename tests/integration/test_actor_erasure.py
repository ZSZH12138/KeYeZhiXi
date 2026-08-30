from __future__ import annotations

from course_insight.application.actor_erasure import ActorErasureCoordinator
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.m0_repository import SQLiteM0Repository
from course_insight.infrastructure.sqlite.m5_repository import SQLiteM5Repository
from tests.unit.test_m5_class_replacement import _learner


class _EmptyPurge:
    def purge_actor(self, actor_id: str) -> int:
        assert actor_id.startswith("pseudonym_")
        return 0


def test_sqlite_actor_erasure_removes_only_target_actor(tmp_path) -> None:
    repository = SQLiteM5Repository(tmp_path / "actor-erasure.sqlite3")
    repository.initialize()
    target = _learner("pseudonym_delete_me", 0.6)
    survivor = _learner("pseudonym_keep_me", 0.7)
    repository.save_learner_state(target)
    repository.save_learner_state(survivor)
    repositories = {name: _EmptyPurge() for name in ("m0", "m4", "m6", "m7", "m8", "m9")}
    repositories["m5"] = repository

    result = ActorErasureCoordinator(repositories).purge("pseudonym_delete_me")

    assert result.total_deleted >= 1
    assert repository.get_latest_learner_state(
        target.course_id,
        target.class_id,
        target.learner_id,
    ) is None
    assert repository.get_latest_learner_state(
        survivor.course_id,
        survivor.class_id,
        survivor.learner_id,
    ) == survivor


def test_m0_actor_erasure_never_deletes_django_identity_rows(tmp_path) -> None:
    """The runtime M0 cleaner must not bypass Django's relation collector."""

    database_path = tmp_path / "actor-erasure.sqlite3"
    repository = SQLiteM0Repository(database_path)
    repository.initialize()
    connection = connect_sqlite(database_path)
    try:
        connection.execute(
            """
            CREATE TABLE m0_platform_web_user (
                id INTEGER PRIMARY KEY,
                actor_id TEXT NOT NULL UNIQUE
            )
            """
        )
        connection.execute(
            "INSERT INTO m0_platform_web_user(id, actor_id) VALUES (?, ?)",
            (1, "pseudonym_django_identity"),
        )
    finally:
        connection.close()

    repository.purge_actor("pseudonym_django_identity")

    connection = connect_sqlite(database_path)
    try:
        row = connection.execute(
            "SELECT actor_id FROM m0_platform_web_user WHERE id = ?",
            (1,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    assert row["actor_id"] == "pseudonym_django_identity"
