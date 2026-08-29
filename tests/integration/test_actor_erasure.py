from __future__ import annotations

from course_insight.application.actor_erasure import ActorErasureCoordinator
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
