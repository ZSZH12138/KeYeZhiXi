"""Explicit, replay-safe SQLite to PostgreSQL migration orchestration.

The legacy M0 event table stores only the inner ``LearningEvent.payload``.
After an outbox row is delivered and removed, its wider event envelope cannot
be reconstructed. Reports therefore count these rows as source limitations
instead of claiming full public-contract validation.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from course_insight.contracts.analytics import (
    TeacherAnalyticsBundle,
    TeacherReviewDecision,
)
from course_insight.contracts.assessment import (
    AssessmentPaper,
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.state import (
    ClassStateSnapshot,
    LearnerStateSnapshot,
    StateUpdateResult,
)
from course_insight.contracts.tasking import TaskPlan
from course_insight.contracts.tutoring import (
    SessionStateSnapshot,
    StudentFeedbackPackage,
)
from course_insight.infrastructure.json_io import dumps_json, write_json
from course_insight.infrastructure.postgresql.sqlite_import_destination import (
    PostgresImportDestination,
)
from course_insight.infrastructure.sqlite.migrations import SCHEMA_VERSION


MigrationMode = Literal["dry-run", "apply"]
_TABLE_ORDER = (
    "m0_learning_events",
    "m0_event_outbox",
    "m0_assessment_runs",
    "m4_task_plans",
    "m5_learner_states",
    "m5_class_states",
    "m5_state_updates",
    "m6_session_states",
    "m6_tutoring_decisions",
    "m7_student_feedback",
    "m8_assessment_papers",
    "m8_score_audits",
    "m8_scoring_results",
    "m9_teacher_reviews",
    "m9_teacher_analytics",
)
_SOURCE_SELECTS = {
    "m0_learning_events": """
        SELECT events.*, outbox.record AS _outbox_record
        FROM m0_learning_events AS events
        LEFT JOIN m0_event_outbox AS outbox USING (event_id)
        ORDER BY events.event_id
    """,
    "m0_event_outbox": """
        SELECT * FROM m0_event_outbox ORDER BY event_id
    """,
    "m0_assessment_runs": """
        SELECT * FROM m0_assessment_runs ORDER BY operation_id
    """,
    "m4_task_plans": "SELECT * FROM m4_task_plans ORDER BY task_id",
    "m5_learner_states": """
        SELECT * FROM m5_learner_states
        ORDER BY course_id, class_id, learner_id, state_version
    """,
    "m5_class_states": """
        SELECT * FROM m5_class_states
        ORDER BY course_id, class_id, state_version, snapshot_id
    """,
    "m5_state_updates": """
        SELECT * FROM m5_state_updates ORDER BY attempt_id, state_version
    """,
    "m6_session_states": """
        SELECT * FROM m6_session_states ORDER BY session_id, turn_count
    """,
    "m6_tutoring_decisions": """
        SELECT * FROM m6_tutoring_decisions ORDER BY decision_id
    """,
    "m7_student_feedback": """
        SELECT * FROM m7_student_feedback ORDER BY feedback_id
    """,
    "m8_assessment_papers": """
        SELECT * FROM m8_assessment_papers ORDER BY paper_id
    """,
    "m8_score_audits": """
        SELECT * FROM m8_score_audits ORDER BY audit_id, audit_version
    """,
    "m8_scoring_results": """
        SELECT * FROM m8_scoring_results ORDER BY attempt_id, result_key
    """,
    "m9_teacher_reviews": """
        SELECT * FROM m9_teacher_reviews ORDER BY decision_id
    """,
    "m9_teacher_analytics": """
        SELECT * FROM m9_teacher_analytics ORDER BY report_id
    """,
}
_IDENTITY_COLUMNS = {
    "m0_learning_events": ("event_id",),
    "m0_event_outbox": ("event_id",),
    "m0_assessment_runs": ("operation_id",),
    "m4_task_plans": ("task_id",),
    "m5_learner_states": (
        "course_id",
        "class_id",
        "learner_id",
        "state_version",
    ),
    "m5_class_states": ("course_id", "class_id", "snapshot_id"),
    "m5_state_updates": ("attempt_id", "state_version"),
    "m6_session_states": ("session_id", "turn_count"),
    "m6_tutoring_decisions": ("decision_id",),
    "m7_student_feedback": ("feedback_id",),
    "m8_assessment_papers": ("paper_id",),
    "m8_score_audits": ("audit_id", "audit_version"),
    "m8_scoring_results": ("attempt_id", "result_key"),
    "m9_teacher_reviews": ("decision_id",),
    "m9_teacher_analytics": ("report_id",),
}
_CONTRACT_TABLES = {
    "m4_task_plans": TaskPlan,
    "m5_learner_states": LearnerStateSnapshot,
    "m5_class_states": ClassStateSnapshot,
    "m5_state_updates": StateUpdateResult,
    "m6_session_states": SessionStateSnapshot,
    "m7_student_feedback": StudentFeedbackPackage,
    "m8_assessment_papers": AssessmentPaper,
    "m8_score_audits": ScoreAuditRecord,
    "m8_scoring_results": ScoringResultBundle,
    "m9_teacher_reviews": TeacherReviewDecision,
    "m9_teacher_analytics": TeacherAnalyticsBundle,
}


class MigrationError(RuntimeError):
    """Stable migration failure that never includes source or DSN details."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PreparedImportRow:
    """One validated row ready for a fixed-table PostgreSQL import."""

    table: str
    columns: tuple[str, ...]
    values: tuple[Any, ...]
    identity: tuple[object, ...]
    version: tuple[object, ...]
    checksum: str
    fingerprint: str
    contract_validated: bool

    def value_for(self, column: str) -> Any:
        return self.values[self.columns.index(column)]


class ImportDestination(Protocol):
    """Migration-only destination with atomic per-call batches."""

    def apply_batch(
        self,
        table: str,
        rows: tuple[PreparedImportRow, ...],
    ) -> None:
        """Insert and verify one batch in exactly one transaction."""


@dataclass(frozen=True, slots=True)
class TableMigrationReport:
    table: str
    source_count: int
    verified_count: int
    identity_digest: str
    version_digest: str
    checksum_digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "table": self.table,
            "source_count": self.source_count,
            "verified_count": self.verified_count,
            "identity_digest": self.identity_digest,
            "version_digest": self.version_digest,
            "checksum_digest": self.checksum_digest,
        }


@dataclass(frozen=True, slots=True)
class MigrationReport:
    mode: MigrationMode
    status: str
    schema_version: int
    batch_size: int
    completed_batches: int
    partial_envelope_rows: int
    fully_contract_validated: bool
    source_file_checksum: str
    tables: tuple[TableMigrationReport, ...]
    error_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "report_version": 1,
            "mode": self.mode,
            "status": self.status,
            "schema_version": self.schema_version,
            "batch_size": self.batch_size,
            "completed_batches": self.completed_batches,
            "partial_envelope_rows": self.partial_envelope_rows,
            "fully_contract_validated": self.fully_contract_validated,
            "source_file_checksum": self.source_file_checksum,
            "tables": [table.to_dict() for table in self.tables],
            "error_code": self.error_code,
        }


class SQLiteToPostgresMigrator:
    """Validate first, then apply deterministic atomic table batches."""

    def __init__(
        self,
        *,
        source_path: str | os.PathLike[str],
        destination: ImportDestination,
    ) -> None:
        self._source_path = Path(source_path)
        self._destination = destination

    def run(
        self,
        *,
        mode: MigrationMode = "dry-run",
        batch_size: int = 500,
        report_path: str | os.PathLike[str] | None = None,
    ) -> MigrationReport:
        if mode not in {"dry-run", "apply"}:
            raise ValueError("mode must be 'dry-run' or 'apply'")
        if type(batch_size) is not int or not 1 <= batch_size <= 10_000:
            raise ValueError("batch_size must be between 1 and 10000")
        source = _validated_source_path(self._source_path)
        target_report = _validated_report_path(source, report_path)
        try:
            (
                rows_by_table,
                partial_count,
                source_checksum,
            ) = _read_and_validate_source(source)
        except Exception:
            raise MigrationError("SOURCE_VALIDATION_FAILED") from None
        fully_validated = partial_count == 0
        report = MigrationReport(
            mode=mode,
            status=(
                "validated"
                if fully_validated
                else "validated_with_source_limitations"
            ),
            schema_version=SCHEMA_VERSION,
            batch_size=batch_size,
            completed_batches=0,
            partial_envelope_rows=partial_count,
            fully_contract_validated=fully_validated,
            source_file_checksum=source_checksum,
            tables=_table_reports(rows_by_table, verified=False),
        )
        if mode == "dry-run":
            _write_report(target_report, report)
            return report

        completed_batches = 0
        try:
            for table in _TABLE_ORDER:
                rows = rows_by_table[table]
                for start in range(0, len(rows), batch_size):
                    batch = rows[start : start + batch_size]
                    self._destination.apply_batch(table, batch)
                    completed_batches += 1
                    _write_report(
                        target_report,
                        _completed_report(
                            report,
                            rows_by_table,
                            completed_batches=completed_batches,
                            status="applying",
                        ),
                    )
        except Exception:
            _write_report(
                target_report,
                _completed_report(
                    report,
                    rows_by_table,
                    completed_batches=completed_batches,
                    status="failed",
                    error_code="MIGRATION_BATCH_FAILED",
                ),
            )
            raise MigrationError("MIGRATION_BATCH_FAILED") from None
        completed = _completed_report(
            report,
            rows_by_table,
            completed_batches=completed_batches,
            status=(
                "completed"
                if fully_validated
                else "completed_with_source_limitations"
            ),
        )
        _write_report(target_report, completed)
        return completed


def _read_and_validate_source(
    source: Path,
) -> tuple[dict[str, tuple[PreparedImportRow, ...]], int, str]:
    from course_insight.infrastructure.postgresql.sqlite_import_source import (
        read_and_validate_source,
    )

    return read_and_validate_source(source)


def _validate_contract_identity(
    table: str,
    row: Any,
    contract: Any,
) -> None:
    """Compatibility wrapper retained for focused internal tests."""

    from course_insight.infrastructure.postgresql.sqlite_import_source import (
        _validate_contract_identity as validate,
    )

    validate(table, row, contract)


def _table_reports(
    rows_by_table: dict[str, tuple[PreparedImportRow, ...]],
    *,
    verified: bool,
) -> tuple[TableMigrationReport, ...]:
    return tuple(
        TableMigrationReport(
            table=table,
            source_count=len(rows_by_table[table]),
            verified_count=len(rows_by_table[table]) if verified else 0,
            identity_digest=_digest(
                tuple(row.identity for row in rows_by_table[table])
            ),
            version_digest=_digest(
                tuple(row.version for row in rows_by_table[table])
            ),
            checksum_digest=_digest(
                tuple(row.checksum for row in rows_by_table[table])
            ),
        )
        for table in _TABLE_ORDER
    )


def _completed_report(
    base: MigrationReport,
    rows_by_table: dict[str, tuple[PreparedImportRow, ...]],
    *,
    completed_batches: int,
    status: str,
    error_code: str | None = None,
) -> MigrationReport:
    return MigrationReport(
        mode=base.mode,
        status=status,
        schema_version=base.schema_version,
        batch_size=base.batch_size,
        completed_batches=completed_batches,
        partial_envelope_rows=base.partial_envelope_rows,
        fully_contract_validated=base.fully_contract_validated,
        source_file_checksum=base.source_file_checksum,
        tables=_table_reports(
            rows_by_table,
            verified=status
            in {"completed", "completed_with_source_limitations"},
        ),
        error_code=error_code,
    )


def _validated_source_path(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise MigrationError("SOURCE_UNAVAILABLE") from None
    if not resolved.is_file():
        raise MigrationError("SOURCE_UNAVAILABLE")
    return resolved


def _validated_report_path(
    source: Path,
    report_path: str | os.PathLike[str] | None,
) -> Path | None:
    if report_path is None:
        return None
    target = Path(report_path).resolve()
    if target == source:
        raise ValueError("report_path must not overwrite the SQLite source")
    return target


def _write_report(path: Path | None, report: MigrationReport) -> None:
    if path is not None:
        write_json(path, report.to_dict())

def _digest(value: object) -> str:
    serialized = dumps_json(_json_value(value))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _json_value(value: object) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    return value


__all__ = [
    "ImportDestination",
    "MigrationError",
    "MigrationMode",
    "MigrationReport",
    "PostgresImportDestination",
    "PreparedImportRow",
    "SQLiteToPostgresMigrator",
    "TableMigrationReport",
]
