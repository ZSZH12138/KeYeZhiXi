"""PostgreSQL persistence for complete M5 state-update histories."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

import psycopg
from psycopg.types.json import Jsonb

from course_insight.contracts.base import ContractModel
from course_insight.contracts.learning_models import (
    BktModelArtifact,
    DinaModelArtifact,
    KnowledgeTraceSnapshot,
    LearningObservation,
    LearningObservationBatch,
)
from course_insight.contracts.state import (
    ClassStateSnapshot,
    LearnerStateSnapshot,
    StateUpdateResult,
)
from course_insight.infrastructure.postgresql.base import (
    PostgresError,
    PostgresOperationError,
)
from course_insight.infrastructure.postgresql.pool import PostgresPool
from course_insight.infrastructure.postgresql import m5_bkt_runtime


_OPERATION_ERROR = "PostgreSQL repository operation failed"
_INTEGRITY_ERROR = "PostgreSQL M5 repository integrity check failed"
_CONFLICT_ERROR = "PostgreSQL M5 repository identity conflict"
_CHECKSUM_ERROR = "PostgreSQL M5 persisted payload checksum mismatch"
_SCHEMA_ERROR = "PostgreSQL M5 persisted schema version mismatch"
_UNSPECIFIED_CLASS_BASELINE = object()
_TContract = TypeVar("_TContract", bound=ContractModel)

_LEARNER_COLUMNS = """
snapshot_id,
course_id,
class_id,
learner_id,
state_version,
payload,
payload_checksum,
schema_version
"""
_CLASS_COLUMNS = """
snapshot_id,
course_id,
class_id,
state_version,
aggregation_policy_version,
payload,
payload_checksum,
schema_version
"""
_UPDATE_COLUMNS = """
attempt_id,
course_id,
class_id,
learner_id,
state_version,
payload,
payload_checksum,
schema_version
"""
_OBSERVATION_COLUMNS = """
observation_id,
course_id,
class_id,
learner_id,
attempt_id,
occurred_at,
payload,
payload_checksum,
schema_version
"""
_DINA_MODEL_COLUMNS = """
model_id,
course_id,
model_version,
created_at,
payload,
payload_checksum,
schema_version
"""


class PostgresM5Repository:
    """Persist append-only M5 state within M5-owned PostgreSQL tables."""

    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    def insert_or_get_learning_observation_batch(
        self,
        batch: LearningObservationBatch,
    ) -> LearningObservationBatch:
        """Insert immutable observations or return their identical replay."""

        candidate = _isolated_contract(batch, LearningObservationBatch)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    for observation in candidate.observations:
                        connection.execute(
                            """
                            INSERT INTO m5_learning_observations(
                                observation_id,
                                course_id,
                                class_id,
                                learner_id,
                                attempt_id,
                                occurred_at,
                                payload,
                                payload_checksum,
                                schema_version
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (observation_id) DO NOTHING
                            """,
                            (
                                observation.observation_id,
                                observation.course_id,
                                observation.class_id,
                                observation.learner_id,
                                observation.attempt_id,
                                observation.occurred_at,
                                Jsonb(observation.to_dict()),
                                observation.content_checksum(),
                                observation.schema_version,
                            ),
                        )
                        row = connection.execute(
                            f"""
                            SELECT {_OBSERVATION_COLUMNS}
                            FROM m5_learning_observations
                            WHERE observation_id = %s
                            """,
                            (observation.observation_id,),
                        ).fetchone()
                        if _learning_observation_from_row(row) != observation:
                            raise PostgresOperationError(_CONFLICT_ERROR)
            return candidate
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def list_learning_observations(
        self,
        *,
        course_id: str,
        class_id: str,
    ) -> list[LearningObservation]:
        """List observations in stable chronological order."""

        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    f"""
                    SELECT {_OBSERVATION_COLUMNS}
                    FROM m5_learning_observations
                    WHERE course_id = %s AND class_id = %s
                    ORDER BY occurred_at, attempt_id, observation_id
                    """,
                    (course_id, class_id),
                ).fetchall()
                return [_learning_observation_from_row(row) for row in rows]
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def insert_or_get_dina_model(
        self,
        model: DinaModelArtifact,
    ) -> DinaModelArtifact:
        """Insert one append-only DINA model version."""

        candidate = _isolated_contract(model, DinaModelArtifact)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO m5_dina_models(
                            model_id,
                            course_id,
                            model_version,
                            created_at,
                            payload,
                            payload_checksum,
                            schema_version
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            candidate.model_id,
                            candidate.course_id,
                            candidate.model_version,
                            candidate.created_at,
                            Jsonb(candidate.to_dict()),
                            candidate.content_checksum(),
                            candidate.schema_version,
                        ),
                    )
                    row = connection.execute(
                        f"""
                        SELECT {_DINA_MODEL_COLUMNS}
                        FROM m5_dina_models
                        WHERE model_id = %s
                           OR (course_id = %s AND model_version = %s)
                        ORDER BY (model_id = %s) DESC
                        LIMIT 1
                        """,
                        (
                            candidate.model_id,
                            candidate.course_id,
                            candidate.model_version,
                            candidate.model_id,
                        ),
                    ).fetchone()
                    stored = _dina_model_from_row(row)
                    if stored != candidate:
                        raise PostgresOperationError(_CONFLICT_ERROR)
                    return stored
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_dina_model(
        self,
        *,
        course_id: str,
        model_version: str,
    ) -> DinaModelArtifact | None:
        """Load one exact course-scoped DINA version."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_DINA_MODEL_COLUMNS}
                    FROM m5_dina_models
                    WHERE course_id = %s AND model_version = %s
                    """,
                    (course_id, model_version),
                ).fetchone()
                return None if row is None else _dina_model_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def insert_or_get_bkt_model(
        self,
        model: BktModelArtifact,
    ) -> BktModelArtifact:
        return m5_bkt_runtime.insert_or_get_bkt_model(self._pool, model)

    def get_bkt_model(
        self,
        *,
        course_id: str,
        model_version: str,
    ) -> BktModelArtifact | None:
        return m5_bkt_runtime.get_bkt_model(
            self._pool,
            course_id=course_id,
            model_version=model_version,
        )

    def insert_or_get_knowledge_trace(
        self,
        trace: KnowledgeTraceSnapshot,
    ) -> KnowledgeTraceSnapshot:
        return m5_bkt_runtime.insert_or_get_knowledge_trace(self._pool, trace)

    def get_knowledge_trace(
        self,
        *,
        trace_id: str,
    ) -> KnowledgeTraceSnapshot | None:
        return m5_bkt_runtime.get_knowledge_trace(
            self._pool,
            trace_id=trace_id,
        )

    def insert_or_get_state_update(
        self,
        result: StateUpdateResult,
        *,
        expected_previous_class_snapshot_id: str | None | object = (
            _UNSPECIFIED_CLASS_BASELINE
        ),
    ) -> StateUpdateResult:
        """Insert an attempt version atomically or return its identical winner."""

        candidate = _isolated_contract(result, StateUpdateResult)
        candidate.assert_consistent()
        learner = candidate.learner_state_snapshot
        class_state = candidate.class_state_snapshot
        attempt_id = candidate.diagnosis_result.attempt_id
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    if (
                        expected_previous_class_snapshot_id
                        is not _UNSPECIFIED_CLASS_BASELINE
                    ):
                        _lock_class_scope(
                            connection,
                            learner.course_id,
                            learner.class_id,
                        )
                        existing_row = connection.execute(
                            f"""
                            SELECT {_UPDATE_COLUMNS}
                            FROM m5_state_updates
                            WHERE attempt_id = %s AND state_version = %s
                            """,
                            (attempt_id, learner.state_version),
                        ).fetchone()
                        if existing_row is not None:
                            stored = _state_result_from_row(existing_row)
                            if stored != candidate:
                                raise PostgresOperationError(_CONFLICT_ERROR)
                            return stored
                        baseline_row = connection.execute(
                            """
                            SELECT snapshot_id
                            FROM m5_class_states
                            WHERE course_id = %s AND class_id = %s
                            ORDER BY state_version DESC
                            LIMIT 1
                            """,
                            (learner.course_id, learner.class_id),
                        ).fetchone()
                        current_snapshot_id = (
                            None
                            if baseline_row is None
                            else _required_text(
                                baseline_row,
                                "snapshot_id",
                            )
                        )
                        if (
                            current_snapshot_id
                            != expected_previous_class_snapshot_id
                        ):
                            raise PostgresOperationError(_CONFLICT_ERROR)
                    _insert_or_validate_learner(connection, learner)
                    _insert_or_validate_class(connection, class_state)
                    connection.execute(
                        """
                        INSERT INTO m5_state_updates(
                            attempt_id,
                            course_id,
                            class_id,
                            learner_id,
                            state_version,
                            payload,
                            payload_checksum,
                            schema_version
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (attempt_id, state_version) DO NOTHING
                        """,
                        (
                            attempt_id,
                            learner.course_id,
                            learner.class_id,
                            learner.learner_id,
                            learner.state_version,
                            Jsonb(candidate.to_dict()),
                            candidate.content_checksum(),
                            candidate.schema_version,
                        ),
                    )
                    row = connection.execute(
                        f"""
                        SELECT {_UPDATE_COLUMNS}
                        FROM m5_state_updates
                        WHERE attempt_id = %s AND state_version = %s
                        """,
                        (attempt_id, learner.state_version),
                    ).fetchone()
                    stored = _state_result_from_row(row)
                    if stored != candidate:
                        raise PostgresOperationError(_CONFLICT_ERROR)
                    return stored
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def save_state_update(self, result: StateUpdateResult) -> None:
        """Retain the compatibility write alias."""

        self.insert_or_get_state_update(result)

    def get_state_update(self, attempt_id: str) -> StateUpdateResult | None:
        """Load the greatest state version for one assessment attempt."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_UPDATE_COLUMNS}
                    FROM m5_state_updates
                    WHERE attempt_id = %s
                    ORDER BY state_version DESC
                    LIMIT 1
                    """,
                    (attempt_id,),
                ).fetchone()
                return None if row is None else _state_result_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_state_update_result(
        self,
        attempt_id: str,
    ) -> StateUpdateResult | None:
        """Retain the compatibility read alias."""

        return self.get_state_update(attempt_id)

    def get_state_update_version(
        self,
        attempt_id: str,
        state_version: int,
    ) -> StateUpdateResult | None:
        """Load one exact attempt-bound state version."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_UPDATE_COLUMNS}
                    FROM m5_state_updates
                    WHERE attempt_id = %s AND state_version = %s
                    """,
                    (attempt_id, state_version),
                ).fetchone()
                return None if row is None else _state_result_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_state_update_for_audit(
        self,
        attempt_id: str,
        audit_id: str,
        audit_version: int,
    ) -> StateUpdateResult | None:
        """Load the earliest state version containing an exact audit version."""

        audit_key = f"{audit_id}:{audit_version}"
        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    f"""
                    SELECT {_UPDATE_COLUMNS}
                    FROM m5_state_updates
                    WHERE attempt_id = %s
                    ORDER BY state_version ASC
                    """,
                    (attempt_id,),
                ).fetchall()
                for row in rows:
                    result = _state_result_from_row(row)
                    if audit_key in result.processed_audit_ids:
                        return result
                return None
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def save_learner_state(self, snapshot: LearnerStateSnapshot) -> None:
        """Insert one immutable course/class-scoped learner snapshot."""

        candidate = _isolated_contract(snapshot, LearnerStateSnapshot)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    _insert_or_validate_learner(connection, candidate)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_learner_state(
        self,
        learner_id: str,
        state_version: int,
    ) -> LearnerStateSnapshot | None:
        """Load the legacy key only when it resolves to one exact scope."""

        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    f"""
                    SELECT {_LEARNER_COLUMNS}
                    FROM m5_learner_states
                    WHERE learner_id = %s AND state_version = %s
                    """,
                    (learner_id, state_version),
                ).fetchall()
                if len(rows) > 1:
                    raise PostgresOperationError(
                        "M5 legacy learner-state lookup is ambiguous across courses"
                    )
                return None if not rows else _learner_from_row(rows[0])
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_learner_state_exact(
        self,
        course_id: str,
        class_id: str,
        learner_id: str,
        state_version: int,
    ) -> LearnerStateSnapshot | None:
        """Load one learner version under its complete persisted scope."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_LEARNER_COLUMNS}
                    FROM m5_learner_states
                    WHERE course_id = %s
                      AND class_id = %s
                      AND learner_id = %s
                      AND state_version = %s
                    """,
                    (course_id, class_id, learner_id, state_version),
                ).fetchone()
                if row is None:
                    return None
                snapshot = _learner_from_row(row)
                if (
                    snapshot.course_id,
                    snapshot.class_id,
                    snapshot.learner_id,
                    snapshot.state_version,
                ) != (course_id, class_id, learner_id, state_version):
                    raise PostgresOperationError(_INTEGRITY_ERROR)
                return snapshot
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_latest_learner_state(
        self,
        course_id: str,
        class_id: str,
        learner_id: str,
    ) -> LearnerStateSnapshot | None:
        """Load the greatest learner version in one exact teaching scope."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_LEARNER_COLUMNS}
                    FROM m5_learner_states
                    WHERE course_id = %s
                      AND class_id = %s
                      AND learner_id = %s
                    ORDER BY state_version DESC
                    LIMIT 1
                    """,
                    (course_id, class_id, learner_id),
                ).fetchone()
                return None if row is None else _learner_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def list_latest_learner_states(
        self,
        course_id: str,
        class_id: str,
    ) -> list[LearnerStateSnapshot]:
        """Load one greatest state version for every learner in scope."""

        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    f"""
                    SELECT DISTINCT ON (learner_id) {_LEARNER_COLUMNS}
                    FROM m5_learner_states
                    WHERE course_id = %s AND class_id = %s
                    ORDER BY learner_id, state_version DESC
                    """,
                    (course_id, class_id),
                ).fetchall()
                return [_learner_from_row(row) for row in rows]
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def save_class_state(self, snapshot: ClassStateSnapshot) -> None:
        """Insert one immutable class aggregate with a serialized version."""

        candidate = _isolated_contract(snapshot, ClassStateSnapshot)
        try:
            with self._pool.connection() as connection:
                with connection.transaction():
                    _insert_or_validate_class(connection, candidate)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_class_state(self, snapshot_id: str) -> ClassStateSnapshot | None:
        """Load a legacy snapshot identity only when its scope is unambiguous."""

        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    f"""
                    SELECT {_CLASS_COLUMNS}
                    FROM m5_class_states
                    WHERE snapshot_id = %s
                    """,
                    (snapshot_id,),
                ).fetchall()
                if len(rows) > 1:
                    raise PostgresOperationError(
                        "M5 legacy class-state lookup is ambiguous across courses"
                    )
                return None if not rows else _class_from_row(rows[0])
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_class_state_by_identity(
        self,
        course_id: str,
        class_id: str,
        snapshot_id: str,
    ) -> ClassStateSnapshot | None:
        """Load one class identity without crossing a teaching scope."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_CLASS_COLUMNS}
                    FROM m5_class_states
                    WHERE course_id = %s
                      AND class_id = %s
                      AND snapshot_id = %s
                    """,
                    (course_id, class_id, snapshot_id),
                ).fetchone()
                if row is None:
                    return None
                snapshot = _class_from_row(row)
                if (
                    snapshot.course_id,
                    snapshot.class_id,
                    snapshot.snapshot_id,
                ) != (course_id, class_id, snapshot_id):
                    raise PostgresOperationError(_INTEGRITY_ERROR)
                return snapshot
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_class_state_exact(
        self,
        course_id: str,
        class_id: str,
        state_version: int,
    ) -> ClassStateSnapshot | None:
        """Load one class aggregate under its internal numeric version."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_CLASS_COLUMNS}
                    FROM m5_class_states
                    WHERE course_id = %s
                      AND class_id = %s
                      AND state_version = %s
                    """,
                    (course_id, class_id, state_version),
                ).fetchone()
                if row is None:
                    return None
                snapshot = _class_from_row(row)
                if (
                    snapshot.course_id,
                    snapshot.class_id,
                ) != (course_id, class_id) or (
                    _positive_int(row, "state_version") != state_version
                ):
                    raise PostgresOperationError(_INTEGRITY_ERROR)
                return snapshot
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_latest_class_state(
        self,
        course_id: str,
        class_id: str,
    ) -> ClassStateSnapshot | None:
        """Load the numerically greatest class version in one course."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    f"""
                    SELECT {_CLASS_COLUMNS}
                    FROM m5_class_states
                    WHERE course_id = %s AND class_id = %s
                    ORDER BY state_version DESC
                    LIMIT 1
                    """,
                    (course_id, class_id),
                ).fetchone()
                return None if row is None else _class_from_row(row)
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_latest_class_state_version(
        self,
        course_id: str,
        class_id: str,
    ) -> int | None:
        """Load the greatest internal class history version."""

        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT MAX(state_version) AS state_version
                    FROM m5_class_states
                    WHERE course_id = %s AND class_id = %s
                    """,
                    (course_id, class_id),
                ).fetchone()
                value = None if row is None else row.get("state_version")
                if value is not None and (type(value) is not int or value < 1):
                    raise PostgresOperationError(_INTEGRITY_ERROR)
                return value
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None

    def get_processed_audit_ids(
        self,
        course_id: str,
        class_id: str,
        learner_id: str,
    ) -> frozenset[str]:
        """Rebuild the durable duplicate-processing watermark."""

        try:
            with self._pool.connection() as connection:
                rows = connection.execute(
                    f"""
                    SELECT {_UPDATE_COLUMNS}
                    FROM m5_state_updates
                    WHERE course_id = %s
                      AND class_id = %s
                      AND learner_id = %s
                    """,
                    (course_id, class_id, learner_id),
                ).fetchall()
                return frozenset(
                    audit_id
                    for row in rows
                    for audit_id in _state_result_from_row(
                        row
                    ).processed_audit_ids
                )
        except PostgresError:
            raise
        except psycopg.Error:
            raise PostgresOperationError(_OPERATION_ERROR) from None


def _insert_or_validate_learner(
    connection: Any,
    snapshot: LearnerStateSnapshot,
) -> None:
    connection.execute(
        """
        INSERT INTO m5_learner_states(
            snapshot_id,
            course_id,
            class_id,
            learner_id,
            state_version,
            payload,
            payload_checksum,
            schema_version
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (course_id, class_id, learner_id, state_version)
        DO NOTHING
        """,
        (
            snapshot.snapshot_id,
            snapshot.course_id,
            snapshot.class_id,
            snapshot.learner_id,
            snapshot.state_version,
            Jsonb(snapshot.to_dict()),
            snapshot.content_checksum(),
            snapshot.schema_version,
        ),
    )
    row = connection.execute(
        f"""
        SELECT {_LEARNER_COLUMNS}
        FROM m5_learner_states
        WHERE course_id = %s
          AND class_id = %s
          AND learner_id = %s
          AND state_version = %s
        """,
        (
            snapshot.course_id,
            snapshot.class_id,
            snapshot.learner_id,
            snapshot.state_version,
        ),
    ).fetchone()
    if row is None or _learner_from_row(row) != snapshot:
        raise PostgresOperationError(_CONFLICT_ERROR)


def _learning_observation_from_row(
    row: Mapping[str, Any] | None,
) -> LearningObservation:
    observation = _contract_from_row(
        row,
        LearningObservation,
        label="learning observation",
    )
    try:
        if (
            observation.observation_id != _required_text(row, "observation_id")
            or observation.course_id != _required_text(row, "course_id")
            or observation.class_id != _required_text(row, "class_id")
            or observation.learner_id != _required_text(row, "learner_id")
            or observation.attempt_id != _required_text(row, "attempt_id")
        ):
            raise ValueError
        return observation
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _dina_model_from_row(
    row: Mapping[str, Any] | None,
) -> DinaModelArtifact:
    model = _contract_from_row(row, DinaModelArtifact, label="DINA model")
    try:
        if (
            model.model_id != _required_text(row, "model_id")
            or model.course_id != _required_text(row, "course_id")
            or model.model_version != _required_text(row, "model_version")
        ):
            raise ValueError
        return model
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _insert_or_validate_class(
    connection: Any,
    snapshot: ClassStateSnapshot,
) -> None:
    _lock_class_scope(connection, snapshot.course_id, snapshot.class_id)
    existing = connection.execute(
        f"""
        SELECT {_CLASS_COLUMNS}
        FROM m5_class_states
        WHERE course_id = %s
          AND class_id = %s
          AND snapshot_id = %s
        """,
        (snapshot.course_id, snapshot.class_id, snapshot.snapshot_id),
    ).fetchone()
    if existing is not None:
        if _class_from_row(existing) != snapshot:
            raise PostgresOperationError(_CONFLICT_ERROR)
        return
    next_row = connection.execute(
        """
        SELECT COALESCE(MAX(state_version), 0) + 1 AS next_version
        FROM m5_class_states
        WHERE course_id = %s AND class_id = %s
        """,
        (snapshot.course_id, snapshot.class_id),
    ).fetchone()
    if next_row is None or type(next_row.get("next_version")) is not int:
        raise PostgresOperationError(_INTEGRITY_ERROR)
    connection.execute(
        """
        INSERT INTO m5_class_states(
            snapshot_id,
            course_id,
            class_id,
            state_version,
            aggregation_policy_version,
            payload,
            payload_checksum,
            schema_version
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (course_id, class_id, snapshot_id) DO NOTHING
        """,
        (
            snapshot.snapshot_id,
            snapshot.course_id,
            snapshot.class_id,
            next_row["next_version"],
            snapshot.aggregation_policy_version,
            Jsonb(snapshot.to_dict()),
            snapshot.content_checksum(),
            snapshot.schema_version,
        ),
    )
    row = connection.execute(
        f"""
        SELECT {_CLASS_COLUMNS}
        FROM m5_class_states
        WHERE course_id = %s
          AND class_id = %s
          AND snapshot_id = %s
        """,
        (snapshot.course_id, snapshot.class_id, snapshot.snapshot_id),
    ).fetchone()
    if row is None or _class_from_row(row) != snapshot:
        raise PostgresOperationError(_CONFLICT_ERROR)


def _lock_class_scope(connection: Any, course_id: str, class_id: str) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0)) AS locked",
        (f"m5-class:{course_id}\x1f{class_id}",),
    )


def _learner_from_row(row: Mapping[str, Any] | None) -> LearnerStateSnapshot:
    snapshot = _contract_from_row(
        row,
        LearnerStateSnapshot,
        label="learner state",
    )
    try:
        if (
            snapshot.snapshot_id != _required_text(row, "snapshot_id")
            or snapshot.course_id != _required_text(row, "course_id")
            or snapshot.class_id != _required_text(row, "class_id")
            or snapshot.learner_id != _required_text(row, "learner_id")
            or snapshot.state_version != _positive_int(row, "state_version")
        ):
            raise ValueError
        return snapshot
    except PostgresError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _class_from_row(row: Mapping[str, Any] | None) -> ClassStateSnapshot:
    snapshot = _contract_from_row(
        row,
        ClassStateSnapshot,
        label="class state",
    )
    try:
        if (
            snapshot.snapshot_id != _required_text(row, "snapshot_id")
            or snapshot.course_id != _required_text(row, "course_id")
            or snapshot.class_id != _required_text(row, "class_id")
            or snapshot.aggregation_policy_version
            != _required_text(row, "aggregation_policy_version")
        ):
            raise ValueError
        _positive_int(row, "state_version")
        return snapshot
    except PostgresError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _state_result_from_row(
    row: Mapping[str, Any] | None,
) -> StateUpdateResult:
    result = _contract_from_row(
        row,
        StateUpdateResult,
        label="state update",
    )
    learner = result.learner_state_snapshot
    try:
        if (
            result.diagnosis_result.attempt_id
            != _required_text(row, "attempt_id")
            or learner.course_id != _required_text(row, "course_id")
            or learner.class_id != _required_text(row, "class_id")
            or learner.learner_id != _required_text(row, "learner_id")
            or learner.state_version != _positive_int(row, "state_version")
        ):
            raise ValueError
        return result
    except PostgresError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _contract_from_row(
    row: Mapping[str, Any] | None,
    contract_type: type[_TContract],
    *,
    label: str,
) -> _TContract:
    try:
        if row is None or type(row.get("payload")) is not dict:
            raise ValueError
        contract = contract_type.model_validate(row["payload"])
        stored_schema = _required_text(row, "schema_version")
        expected_schema = str(
            contract_type.model_fields["schema_version"].default
        )
        if (
            stored_schema != expected_schema
            or contract.schema_version != expected_schema
        ):
            raise PostgresOperationError(
                f"{_SCHEMA_ERROR}: {label}"
            )
        checksum = _required_checksum(row, "payload_checksum")
        if contract.content_checksum() != checksum:
            raise PostgresOperationError(
                f"{_CHECKSUM_ERROR}: {label}"
            )
        return contract
    except PostgresError:
        raise
    except Exception:
        raise PostgresOperationError(_INTEGRITY_ERROR) from None


def _isolated_contract(
    value: _TContract,
    contract_type: type[_TContract],
) -> _TContract:
    if not isinstance(value, contract_type):
        raise TypeError(f"value must be a {contract_type.__name__}")
    candidate = contract_type.model_validate(value.model_dump(mode="python"))
    expected_schema = str(
        contract_type.model_fields["schema_version"].default
    )
    if candidate.schema_version != expected_schema:
        raise ValueError("M5 contract schema version is unsupported")
    return candidate


def _required_text(row: Mapping[str, Any] | None, field: str) -> str:
    if row is None:
        raise ValueError
    value = row[field]
    if type(value) is not str or not value:
        raise ValueError
    return value


def _positive_int(row: Mapping[str, Any] | None, field: str) -> int:
    if row is None:
        raise ValueError
    value = row[field]
    if type(value) is not int or value < 1:
        raise ValueError
    return value


def _required_checksum(row: Mapping[str, Any] | None, field: str) -> str:
    value = _required_text(row, field)
    if (
        len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError
    return value


PostgreSQLM5Repository = PostgresM5Repository

__all__ = ["PostgresM5Repository", "PostgreSQLM5Repository"]
