from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from course_insight.infrastructure.sqlite.m5_repository import (
    SQLiteM5Repository,
)
from course_insight.modules.m5_learner_class_state.service import (
    M5StateService,
)
from tests.integration.test_web_workflow_persistence import _state_result


def test_sqlite_m5_exact_reads_disambiguate_legacy_keys_by_scope(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "m5-exact-state.db"
    repository = SQLiteM5Repository(database_path)
    repository.initialize()
    first = _state_result(
        attempt_id="attempt_course_1",
        course_id="course_1",
        class_id="shared_class",
        state_version=1,
    )
    second = _state_result(
        attempt_id="attempt_course_2",
        course_id="course_2",
        class_id="shared_class",
        state_version=1,
    )
    shared_class_snapshot_id = "shared_class_state_v1"
    first_class = first.class_state_snapshot.model_copy(
        update={"snapshot_id": shared_class_snapshot_id},
        deep=True,
    )
    second_class = second.class_state_snapshot.model_copy(
        update={"snapshot_id": shared_class_snapshot_id},
        deep=True,
    )
    repository.save_learner_state(first.learner_state_snapshot)
    repository.save_learner_state(second.learner_state_snapshot)
    repository.save_class_state(first_class)
    repository.save_class_state(second_class)

    with pytest.raises(RuntimeError, match="ambiguous across courses"):
        repository.get_learner_state("learner_1", 1)
    with pytest.raises(RuntimeError, match="ambiguous across courses"):
        repository.get_class_state(shared_class_snapshot_id)

    learner = repository.get_learner_state_exact(
        "course_1",
        "shared_class",
        "learner_1",
        1,
    )
    class_state = repository.get_class_state_exact(
        "course_1",
        "shared_class",
        1,
    )
    class_state_by_identity = repository.get_class_state_by_identity(
        "course_1",
        "shared_class",
        shared_class_snapshot_id,
    )
    assert learner == first.learner_state_snapshot
    assert class_state == first_class
    assert class_state_by_identity == first_class
    assert repository.get_class_state_by_identity(
        "course_2",
        "shared_class",
        shared_class_snapshot_id,
    ) == second_class
    assert repository.get_learner_state_exact(
        "course_missing",
        "shared_class",
        "learner_1",
        1,
    ) is None
    assert repository.get_class_state_exact(
        "course_missing",
        "shared_class",
        1,
    ) is None
    assert repository.get_class_state_by_identity(
        "course_missing",
        "shared_class",
        shared_class_snapshot_id,
    ) is None

    learner_again = repository.get_learner_state_exact(
        "course_1",
        "shared_class",
        "learner_1",
        1,
    )
    class_again = repository.get_class_state_exact(
        "course_1",
        "shared_class",
        1,
    )
    assert learner_again is not learner
    assert learner_again is not None
    assert learner is not None
    assert learner_again.concept_states is not learner.concept_states
    assert class_again is not class_state
    assert class_again is not None
    assert class_state is not None
    assert class_again.scope is not class_state.scope


class _ExactStateRepository:
    def __init__(self, learner: Any, class_state: Any) -> None:
        self.learner = learner
        self.class_state = class_state

    def get_learner_state_exact(
        self,
        course_id: str,
        class_id: str,
        learner_id: str,
        state_version: int,
    ) -> Any:
        if (
            course_id,
            class_id,
            learner_id,
            state_version,
        ) != ("course_1", "class_1", "learner_1", 1):
            return None
        return self.learner

    def get_class_state_exact(
        self,
        course_id: str,
        class_id: str,
        state_version: int,
    ) -> Any:
        if (course_id, class_id, state_version) != (
            "course_1",
            "class_1",
            1,
        ):
            return None
        return self.class_state

    def get_class_state_by_identity(
        self,
        course_id: str,
        class_id: str,
        snapshot_id: str,
    ) -> Any:
        if (course_id, class_id, snapshot_id) != (
            "course_1",
            "class_1",
            self.class_state.snapshot_id,
        ):
            return None
        return self.class_state


def test_m5_service_exact_reads_isolate_repository_contracts() -> None:
    result = _state_result(
        attempt_id="attempt_1",
        course_id="course_1",
        class_id="class_1",
        state_version=1,
    )
    service = M5StateService(
        _ExactStateRepository(
            result.learner_state_snapshot,
            result.class_state_snapshot,
        ),
        object(),
        object(),
    )

    learner = service.get_learner_state_exact(
        "course_1",
        "class_1",
        "learner_1",
        1,
    )
    class_state = service.get_class_state_exact(
        "course_1",
        "class_1",
        1,
    )
    class_state_by_identity = service.get_class_state_by_identity(
        "course_1",
        "class_1",
        result.class_state_snapshot.snapshot_id,
    )

    assert learner == result.learner_state_snapshot
    assert learner is not result.learner_state_snapshot
    assert learner is not None
    assert learner.concept_states is not result.learner_state_snapshot.concept_states
    assert class_state == result.class_state_snapshot
    assert class_state is not result.class_state_snapshot
    assert class_state is not None
    assert class_state.scope is not result.class_state_snapshot.scope
    assert class_state_by_identity == result.class_state_snapshot
    assert class_state_by_identity is not result.class_state_snapshot
    assert class_state_by_identity is not None
    assert (
        class_state_by_identity.scope
        is not result.class_state_snapshot.scope
    )
    assert service.get_learner_state_exact(
        "course_missing",
        "class_1",
        "learner_1",
        1,
    ) is None
    assert service.get_class_state_exact(
        "course_missing",
        "class_1",
        1,
    ) is None
    assert service.get_class_state_by_identity(
        "course_missing",
        "class_1",
        result.class_state_snapshot.snapshot_id,
    ) is None


def test_m5_service_exact_reads_reject_repository_scope_mismatch() -> None:
    wrong_scope = _state_result(
        attempt_id="attempt_wrong_scope",
        course_id="course_2",
        class_id="class_2",
        state_version=1,
    )
    service = M5StateService(
        _ExactStateRepository(
            wrong_scope.learner_state_snapshot,
            wrong_scope.class_state_snapshot,
        ),
        object(),
        object(),
    )

    with pytest.raises(RuntimeError, match="learner-state scope mismatch"):
        service.get_learner_state_exact(
            "course_1",
            "class_1",
            "learner_1",
            1,
        )
    with pytest.raises(RuntimeError, match="class-state scope mismatch"):
        service.get_class_state_exact(
            "course_1",
            "class_1",
            1,
        )
    with pytest.raises(RuntimeError, match="class-state identity mismatch"):
        service.get_class_state_by_identity(
            "course_1",
            "class_1",
            wrong_scope.class_state_snapshot.snapshot_id,
        )
