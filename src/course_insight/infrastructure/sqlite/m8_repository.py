"""SQLite persistence for M8 papers, scope, audits, and scoring bundles."""

from __future__ import annotations

import hmac
import sqlite3
from datetime import timezone
from pathlib import Path

from course_insight.contracts.assessment import (
    AssessmentPaper,
    ScoreAuditRecord,
    ScoringResultBundle,
)
from course_insight.contracts.learning_models import (
    AbilityEstimate,
    AdaptiveSelectionResult,
    CalibrationReviewDecision,
    CalibrationRunResult,
    IRTParameterSet,
)
from course_insight.infrastructure.json_io import dumps_json
from course_insight.infrastructure.sqlite import m8_model_runtime
from course_insight.infrastructure.sqlite.connection import connect_sqlite
from course_insight.infrastructure.sqlite.actor_erasure import purge_sqlite_actor
from course_insight.infrastructure.sqlite.migrations import migrate
from course_insight.modules.m8_assessment_scoring.paper_record import (
    FrozenAssessmentRecord,
)
from course_insight.modules.m8_assessment_scoring.repository import (
    ReviewVersionConflictError,
    assert_review_transition,
)
from course_insight.modules.m8_assessment_scoring.retry_equivalence import (
    same_paper_generation,
    same_score_audit_result,
    same_scoring_result,
)


class SQLiteM8Repository:
    """Persist M8-owned recovery objects without exposing SQLite rows."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    def initialize(self) -> None:
        connection = connect_sqlite(self._database_path)
        try:
            migrate(connection)
        finally:
            connection.close()

    def purge_actor(self, actor_id: str) -> int:
        return purge_sqlite_actor(
            self._database_path,
            module="m8",
            actor_id=actor_id,
        )

    def insert_or_get_paper(
        self,
        paper: AssessmentPaper,
        *,
        course_id: str,
        class_id: str,
    ) -> AssessmentPaper:
        if not course_id.strip() or not class_id.strip():
            raise ValueError("M8 paper execution scope must not be blank")
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            stored = self._insert_or_validate_paper(
                connection,
                paper,
                course_id=course_id,
                class_id=class_id,
            )
            connection.execute("COMMIT")
            return stored.model_copy(deep=True)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def insert_or_get_paper_record(
        self,
        record: FrozenAssessmentRecord,
    ) -> FrozenAssessmentRecord:
        """Persist one append-only paper, scope, and rubric record."""

        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            authoritative_paper = self._insert_or_validate_paper(
                connection,
                record.paper,
                course_id=record.course_id,
                class_id=record.class_id,
            )
            if (
                authoritative_paper != record.paper
                and not same_paper_generation(authoritative_paper, record.paper)
            ):
                raise RuntimeError("M8 paper record conflict")
            authoritative_record = record.model_copy(
                update={"paper": authoritative_paper},
                deep=True,
            )
            stored = self._insert_or_validate_paper_record(
                connection,
                authoritative_record,
            )
            connection.execute("COMMIT")
            return stored.model_copy(deep=True)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_paper_record(
        self,
        paper_id: str,
    ) -> FrozenAssessmentRecord | None:
        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT paper_id, payload, payload_checksum, schema_version
                FROM m8_frozen_assessment_records
                WHERE paper_id = ?
                """,
                (paper_id,),
            ).fetchone()
            if row is None:
                return None
            record = self._paper_record_from_row(row)
            paper_row = connection.execute(
                """
                SELECT
                    paper_id,
                    task_id,
                    course_id,
                    class_id,
                    learner_id,
                    payload
                FROM m8_assessment_papers
                WHERE paper_id = ?
                """,
                (paper_id,),
            ).fetchone()
            if (
                paper_row is None
                or record.paper != self._paper_from_row(paper_row)
                or (record.course_id, record.class_id)
                != (str(paper_row["course_id"]), str(paper_row["class_id"]))
            ):
                raise RuntimeError("M8 frozen paper record is inconsistent")
            return record.model_copy(deep=True)
        finally:
            connection.close()

    def purge_assessment_attempt(self, *, paper_id: str, attempt_id: str) -> None:
        """Delete one transient attempt atomically after result delivery."""

        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM m8_score_audits "
                "WHERE json_extract(payload, '$.attempt_id') = ?",
                (attempt_id,),
            )
            connection.execute(
                "DELETE FROM m8_scoring_results WHERE attempt_id = ?",
                (attempt_id,),
            )
            remaining = connection.execute(
                "SELECT 1 FROM m8_scoring_results WHERE paper_id = ? LIMIT 1",
                (paper_id,),
            ).fetchone()
            if remaining is None:
                connection.execute(
                    "DELETE FROM m8_frozen_assessment_records WHERE paper_id = ?",
                    (paper_id,),
                )
                connection.execute(
                    "DELETE FROM m8_assessment_papers WHERE paper_id = ?",
                    (paper_id,),
                )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def save_paper(self, paper: AssessmentPaper) -> None:
        context = self.get_paper_execution_context(paper.paper_id)
        if context is None:
            raise ValueError(
                "M8 paper persistence requires course and class execution scope"
            )
        self.insert_or_get_paper(
            paper,
            course_id=context[0],
            class_id=context[1],
        )

    def get_paper(self, paper_id: str) -> AssessmentPaper | None:
        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT
                    paper_id,
                    task_id,
                    course_id,
                    class_id,
                    learner_id,
                    payload
                FROM m8_assessment_papers
                WHERE paper_id = ?
                """,
                (paper_id,),
            ).fetchone()
            return (
                None
                if row is None
                else self._paper_from_row(row).model_copy(deep=True)
            )
        finally:
            connection.close()

    def get_paper_execution_context(
        self,
        paper_id: str,
    ) -> tuple[str, str] | None:
        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT course_id, class_id
                FROM m8_assessment_papers
                WHERE paper_id = ?
                """,
                (paper_id,),
            ).fetchone()
            if row is None:
                return None
            return (str(row["course_id"]), str(row["class_id"]))
        finally:
            connection.close()

    def save_score_audit(self, record: ScoreAuditRecord) -> None:
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._insert_or_validate_audit(connection, record)
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_score_audit(
        self,
        audit_id: str,
        audit_version: int,
    ) -> ScoreAuditRecord | None:
        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT audit_id, audit_version, item_instance_id, payload
                FROM m8_score_audits
                WHERE audit_id = ? AND audit_version = ?
                """,
                (audit_id, audit_version),
            ).fetchone()
            return (
                None
                if row is None
                else self._audit_from_row(row).model_copy(deep=True)
            )
        finally:
            connection.close()

    def insert_or_get_scoring_result(
        self,
        bundle: ScoringResultBundle,
    ) -> ScoringResultBundle:
        bundle.validate_business_rules()
        result_key = self._result_key(bundle)
        payload = dumps_json(bundle.to_dict())
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for record in bundle.score_audit_records:
                self._insert_or_validate_audit(connection, record)
            connection.execute(
                """
                INSERT INTO m8_scoring_results(
                    attempt_id,
                    result_key,
                    paper_id,
                    learner_id,
                    finalized_at,
                    payload
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id, result_key) DO NOTHING
                """,
                (
                    bundle.attempt_id,
                    result_key,
                    bundle.paper_id,
                    bundle.learner_id,
                    bundle.finalized_at.astimezone(timezone.utc).isoformat(),
                    payload,
                ),
            )
            row = connection.execute(
                """
                SELECT
                    attempt_id,
                    result_key,
                    paper_id,
                    learner_id,
                    finalized_at,
                    payload
                FROM m8_scoring_results
                WHERE attempt_id = ? AND result_key = ?
                """,
                (bundle.attempt_id, result_key),
            ).fetchone()
            stored = self._scoring_from_row(row)
            if stored != bundle and not same_scoring_result(stored, bundle):
                raise RuntimeError(
                    "M8 scoring-result conflict for the same attempt version"
                )
            connection.execute("COMMIT")
            return stored.model_copy(deep=True)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def insert_or_get_reviewed_scoring_result(
        self,
        bundle: ScoringResultBundle,
        *,
        audit_id: str,
        expected_audit_version: int,
        expected_audit_checksum: str,
    ) -> ScoringResultBundle:
        """Compare the current winner and append its review in one lock."""

        bundle.validate_business_rules()
        result_key = self._result_key(bundle)
        payload = dumps_json(bundle.to_dict())
        connection = connect_sqlite(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing_row = connection.execute(
                """
                SELECT attempt_id, result_key, paper_id, learner_id,
                       finalized_at, payload
                FROM m8_scoring_results
                WHERE attempt_id = ? AND result_key = ?
                """,
                (bundle.attempt_id, result_key),
            ).fetchone()
            if existing_row is not None:
                stored = self._scoring_from_row(existing_row)
                if stored != bundle and not same_scoring_result(stored, bundle):
                    raise ReviewVersionConflictError(
                        "review result vector already has a different winner"
                    )
                connection.execute("COMMIT")
                return stored.model_copy(deep=True)

            audit_row = connection.execute(
                """
                SELECT audit_id, audit_version, item_instance_id, payload
                FROM m8_score_audits
                WHERE audit_id = ?
                ORDER BY audit_version DESC
                LIMIT 1
                """,
                (audit_id,),
            ).fetchone()
            if audit_row is None:
                raise ReviewVersionConflictError(
                    "review base audit is unavailable"
                )
            current_audit = self._audit_from_row(audit_row)
            if (
                current_audit.audit_version != expected_audit_version
                or current_audit.content_checksum()
                != expected_audit_checksum
            ):
                raise ReviewVersionConflictError(
                    "review base audit version or checksum changed"
                )

            current_row = connection.execute(
                """
                SELECT attempt_id, result_key, paper_id, learner_id,
                       finalized_at, payload
                FROM m8_scoring_results
                WHERE attempt_id = ?
                ORDER BY finalized_at DESC, rowid DESC
                LIMIT 1
                """,
                (bundle.attempt_id,),
            ).fetchone()
            if current_row is None:
                raise ReviewVersionConflictError(
                    "review base scoring result is unavailable"
                )
            current = self._scoring_from_row(current_row)
            assert_review_transition(
                current,
                bundle,
                audit_id=audit_id,
                expected_audit_version=expected_audit_version,
                expected_audit_checksum=expected_audit_checksum,
            )
            for record in bundle.score_audit_records:
                self._insert_or_validate_audit(connection, record)
            connection.execute(
                """
                INSERT INTO m8_scoring_results(
                    attempt_id, result_key, paper_id, learner_id,
                    finalized_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    bundle.attempt_id,
                    result_key,
                    bundle.paper_id,
                    bundle.learner_id,
                    bundle.finalized_at.astimezone(timezone.utc).isoformat(),
                    payload,
                ),
            )
            stored_row = connection.execute(
                """
                SELECT attempt_id, result_key, paper_id, learner_id,
                       finalized_at, payload
                FROM m8_scoring_results
                WHERE attempt_id = ? AND result_key = ?
                """,
                (bundle.attempt_id, result_key),
            ).fetchone()
            stored = self._scoring_from_row(stored_row)
            if stored != bundle and not same_scoring_result(stored, bundle):
                raise ReviewVersionConflictError(
                    "persisted review differs from the requested result"
                )
            connection.execute("COMMIT")
            return stored.model_copy(deep=True)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def save_scoring_result(self, bundle: ScoringResultBundle) -> None:
        self.insert_or_get_scoring_result(bundle)

    def get_scoring_result(
        self,
        attempt_id: str,
    ) -> ScoringResultBundle | None:
        connection = connect_sqlite(self._database_path)
        try:
            row = connection.execute(
                """
                SELECT
                    attempt_id,
                    result_key,
                    paper_id,
                    learner_id,
                    finalized_at,
                    payload
                FROM m8_scoring_results
                WHERE attempt_id = ?
                ORDER BY finalized_at DESC, rowid DESC
                LIMIT 1
                """,
                (attempt_id,),
            ).fetchone()
            return (
                None
                if row is None
                else self._scoring_from_row(row).model_copy(deep=True)
            )
        finally:
            connection.close()

    def get_scoring_result_by_checksum(
        self,
        attempt_id: str,
        checksum: str,
    ) -> ScoringResultBundle | None:
        for bundle in self._scoring_history(attempt_id):
            if bundle.content_checksum() == checksum:
                return bundle.model_copy(deep=True)
        return None

    def get_scoring_result_for_audit(
        self,
        attempt_id: str,
        audit_id: str,
        audit_version: int,
    ) -> ScoringResultBundle | None:
        for bundle in self._scoring_history(attempt_id):
            if any(
                record.audit_id == audit_id
                and record.audit_version == audit_version
                for record in bundle.score_audit_records
            ):
                return bundle.model_copy(deep=True)
        return None

    def insert_or_get_calibration_run(
        self,
        result: CalibrationRunResult,
        *,
        course_id: str,
    ) -> CalibrationRunResult:
        return m8_model_runtime.insert_or_get_calibration_run(
            self._database_path,
            result,
            course_id=course_id,
        )

    def get_calibration_run(
        self,
        run_id: str,
    ) -> CalibrationRunResult | None:
        return m8_model_runtime.get_calibration_run(self._database_path, run_id)

    def get_calibration_run_course_id(self, run_id: str) -> str | None:
        return m8_model_runtime.get_calibration_run_course_id(
            self._database_path,
            run_id,
        )

    def insert_or_get_parameter_set(
        self,
        parameter_set: IRTParameterSet,
        *,
        course_id: str,
    ) -> IRTParameterSet:
        return m8_model_runtime.insert_or_get_parameter_set(
            self._database_path,
            parameter_set,
            course_id=course_id,
        )

    def get_parameter_set(
        self,
        parameter_set_id: str,
    ) -> IRTParameterSet | None:
        return m8_model_runtime.get_parameter_set(
            self._database_path,
            parameter_set_id,
        )

    def get_parameter_set_course_id(self, parameter_set_id: str) -> str | None:
        return m8_model_runtime.get_parameter_set_course_id(
            self._database_path,
            parameter_set_id,
        )

    def list_parameter_sets(self, *, course_id: str) -> list[IRTParameterSet]:
        return m8_model_runtime.list_parameter_sets(
            self._database_path,
            course_id=course_id,
        )

    def insert_or_get_calibration_review(
        self,
        decision: CalibrationReviewDecision,
        reviewed_parameter_set: IRTParameterSet,
        *,
        course_id: str,
    ) -> tuple[CalibrationReviewDecision, IRTParameterSet]:
        return m8_model_runtime.insert_or_get_calibration_review(
            self._database_path,
            decision,
            reviewed_parameter_set,
            course_id=course_id,
        )

    def get_calibration_review(
        self,
        calibration_run_id: str,
    ) -> CalibrationReviewDecision | None:
        return m8_model_runtime.get_calibration_review(
            self._database_path,
            calibration_run_id,
        )

    def insert_or_get_ability_estimate(
        self,
        estimate: AbilityEstimate,
        *,
        course_id: str,
    ) -> AbilityEstimate:
        return m8_model_runtime.insert_or_get_ability_estimate(
            self._database_path,
            estimate,
            course_id=course_id,
        )

    def get_ability_estimate(
        self,
        estimate_id: str,
    ) -> AbilityEstimate | None:
        return m8_model_runtime.get_ability_estimate(
            self._database_path,
            estimate_id,
        )

    def insert_or_get_adaptive_selection(
        self,
        selection: AdaptiveSelectionResult,
        *,
        course_id: str,
    ) -> AdaptiveSelectionResult:
        return m8_model_runtime.insert_or_get_adaptive_selection(
            self._database_path,
            selection,
            course_id=course_id,
        )

    def get_adaptive_selection(
        self,
        selection_id: str,
    ) -> AdaptiveSelectionResult | None:
        return m8_model_runtime.get_adaptive_selection(
            self._database_path,
            selection_id,
        )

    @staticmethod
    def _insert_or_validate_paper(
        connection: sqlite3.Connection,
        paper: AssessmentPaper,
        *,
        course_id: str,
        class_id: str,
    ) -> AssessmentPaper:
        if paper.immutable_checksum != paper.freeze():
            raise ValueError("M8 paper immutable checksum is invalid")
        connection.execute(
            """
            INSERT INTO m8_assessment_papers(
                paper_id,
                task_id,
                course_id,
                class_id,
                learner_id,
                payload
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (
                paper.paper_id,
                paper.task_id,
                course_id,
                class_id,
                paper.learner_id,
                dumps_json(paper.to_dict()),
            ),
        )
        row = connection.execute(
            """
            SELECT
                paper_id,
                task_id,
                course_id,
                class_id,
                learner_id,
                payload
            FROM m8_assessment_papers
            WHERE paper_id = ? OR task_id = ?
            ORDER BY CASE WHEN paper_id = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (paper.paper_id, paper.task_id, paper.paper_id),
        ).fetchone()
        stored = SQLiteM8Repository._paper_from_row(row)
        stored_scope = (str(row["course_id"]), str(row["class_id"]))
        if stored_scope != (course_id, class_id) or (
            stored != paper and not same_paper_generation(stored, paper)
        ):
            raise RuntimeError("M8 paper identity or scope conflict")
        return stored

    @staticmethod
    def _insert_or_validate_paper_record(
        connection: sqlite3.Connection,
        record: FrozenAssessmentRecord,
    ) -> FrozenAssessmentRecord:
        connection.execute(
            """
            INSERT INTO m8_frozen_assessment_records(
                paper_id,
                payload,
                payload_checksum,
                schema_version
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(paper_id) DO NOTHING
            """,
            (
                record.paper.paper_id,
                dumps_json(record.to_dict()),
                record.content_checksum(),
                record.schema_version,
            ),
        )
        row = connection.execute(
            """
            SELECT paper_id, payload, payload_checksum, schema_version
            FROM m8_frozen_assessment_records
            WHERE paper_id = ?
            """,
            (record.paper.paper_id,),
        ).fetchone()
        stored = SQLiteM8Repository._paper_record_from_row(row)
        if stored != record:
            raise RuntimeError("M8 paper record conflict")
        return stored

    def _scoring_history(
        self,
        attempt_id: str,
    ) -> list[ScoringResultBundle]:
        connection = connect_sqlite(self._database_path)
        try:
            rows = connection.execute(
                """
                SELECT
                    attempt_id,
                    result_key,
                    paper_id,
                    learner_id,
                    finalized_at,
                    payload
                FROM m8_scoring_results
                WHERE attempt_id = ?
                ORDER BY finalized_at ASC, rowid ASC
                """,
                (attempt_id,),
            ).fetchall()
            return [self._scoring_from_row(row) for row in rows]
        finally:
            connection.close()

    @staticmethod
    def _result_key(bundle: ScoringResultBundle) -> str:
        latest_versions: dict[str, int] = {}
        for record in bundle.score_audit_records:
            latest_versions = {
                **latest_versions,
                record.audit_id: max(
                    record.audit_version,
                    latest_versions.get(record.audit_id, 0),
                ),
            }
        return dumps_json(
            [
                {"audit_id": audit_id, "audit_version": audit_version}
                for audit_id, audit_version in sorted(latest_versions.items())
            ]
        )

    @staticmethod
    def _insert_or_validate_audit(
        connection: sqlite3.Connection,
        record: ScoreAuditRecord,
    ) -> None:
        payload = dumps_json(record.to_dict())
        connection.execute(
            """
            INSERT INTO m8_score_audits(
                audit_id,
                audit_version,
                item_instance_id,
                payload
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(audit_id, audit_version) DO NOTHING
            """,
            (
                record.audit_id,
                record.audit_version,
                record.item_instance_id,
                payload,
            ),
        )
        row = connection.execute(
            """
            SELECT audit_id, audit_version, item_instance_id, payload
            FROM m8_score_audits
            WHERE audit_id = ? AND audit_version = ?
            """,
            (record.audit_id, record.audit_version),
        ).fetchone()
        stored = SQLiteM8Repository._audit_from_row(row)
        if stored != record and not same_score_audit_result(stored, record):
            raise RuntimeError("M8 score-audit version conflict")

    @staticmethod
    def _paper_from_row(row: sqlite3.Row | None) -> AssessmentPaper:
        if row is None:
            raise RuntimeError("M8 paper insert produced no row")
        paper = AssessmentPaper.model_validate_json(str(row["payload"]))
        if (
            paper.paper_id != str(row["paper_id"])
            or paper.task_id != str(row["task_id"])
            or paper.learner_id != str(row["learner_id"])
        ):
            raise RuntimeError("M8 paper row identity mismatch")
        return paper

    @staticmethod
    def _paper_record_from_row(
        row: sqlite3.Row | None,
    ) -> FrozenAssessmentRecord:
        if row is None:
            raise RuntimeError("M8 paper-record insert produced no row")
        record = FrozenAssessmentRecord.model_validate_json(str(row["payload"]))
        if record.paper.paper_id != str(row["paper_id"]):
            raise RuntimeError("M8 paper-record row identity mismatch")
        if not hmac.compare_digest(
            record.content_checksum(),
            str(row["payload_checksum"]),
        ):
            raise RuntimeError("M8 paper-record checksum mismatch")
        if record.schema_version != str(row["schema_version"]):
            raise RuntimeError("M8 paper-record schema version mismatch")
        return record

    @staticmethod
    def _audit_from_row(row: sqlite3.Row | None) -> ScoreAuditRecord:
        if row is None:
            raise RuntimeError("M8 score-audit insert produced no row")
        record = ScoreAuditRecord.model_validate_json(str(row["payload"]))
        if (
            record.audit_id != str(row["audit_id"])
            or record.audit_version != int(row["audit_version"])
            or record.item_instance_id != str(row["item_instance_id"])
        ):
            raise RuntimeError("M8 score-audit row identity mismatch")
        return record

    @staticmethod
    def _scoring_from_row(row: sqlite3.Row | None) -> ScoringResultBundle:
        if row is None:
            raise RuntimeError("M8 scoring-result insert produced no row")
        bundle = ScoringResultBundle.model_validate_json(str(row["payload"]))
        if (
            bundle.attempt_id != str(row["attempt_id"])
            or bundle.paper_id != str(row["paper_id"])
            or bundle.learner_id != str(row["learner_id"])
            or SQLiteM8Repository._result_key(bundle)
            != str(row["result_key"])
        ):
            raise RuntimeError("M8 scoring-result row identity mismatch")
        return bundle
