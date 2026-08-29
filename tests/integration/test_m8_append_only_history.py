"""M8 paper and audit identities are append-only."""

import hashlib
import json
import sqlite3

import pytest

from course_insight.contracts.assessment import AssessmentPaper, ScoreAuditRecord
from course_insight.infrastructure.sqlite.m8_repository import SQLiteM8Repository
from course_insight.infrastructure.sqlite.migrations import current_schema_version
from course_insight.infrastructure.json_io import dumps_json
from course_insight.modules.m8_assessment_scoring.paper_record import (
    FrozenAssessmentRecord,
)
from tests.factories.m5_m8 import make_paper, make_rubric, make_scoring_bundle


def _record(paper: AssessmentPaper) -> FrozenAssessmentRecord:
    return FrozenAssessmentRecord(
        paper=paper,
        course_id="course_1",
        class_id="class_1",
        frozen_rubrics=[],
    )


def test_same_paper_identity_cannot_overwrite_frozen_content(tmp_path) -> None:
    repository = SQLiteM8Repository(tmp_path / "history.sqlite3")
    repository.initialize()
    original = make_paper()
    changed_sections = [
        original.sections[0].model_copy(update={"name": "Changed section"}),
        *original.sections[1:],
    ]
    changed_payload = {
        **original.model_dump(mode="python"),
        "sections": changed_sections,
        "immutable_checksum": "pending",
    }
    candidate = AssessmentPaper(**changed_payload)
    changed = AssessmentPaper(
        **{
            **changed_payload,
            "immutable_checksum": candidate.freeze(),
        }
    )

    repository.insert_or_get_paper_record(_record(original))

    with pytest.raises(RuntimeError, match="conflict"):
        repository.insert_or_get_paper_record(_record(changed))


def test_direct_paper_conflict_rolls_back_and_preserves_original(tmp_path) -> None:
    repository = SQLiteM8Repository(tmp_path / "paper-conflict.sqlite3")
    repository.initialize()
    original = make_paper()
    changed_sections = [
        original.sections[0].model_copy(update={"name": "Changed section"}),
        *original.sections[1:],
    ]
    changed_payload = {
        **original.model_dump(mode="python"),
        "sections": changed_sections,
        "immutable_checksum": "pending",
    }
    candidate = AssessmentPaper(**changed_payload)
    changed = AssessmentPaper(
        **{**changed_payload, "immutable_checksum": candidate.freeze()}
    )
    repository.insert_or_get_paper(
        original,
        course_id="course_1",
        class_id="class_1",
    )

    with pytest.raises(RuntimeError, match="conflict"):
        repository.insert_or_get_paper(
            changed,
            course_id="course_1",
            class_id="class_1",
        )

    assert repository.get_paper(original.paper_id) == original


def test_direct_paper_retry_rejects_an_invalid_immutable_checksum(tmp_path) -> None:
    repository = SQLiteM8Repository(tmp_path / "paper-freeze.sqlite3")
    repository.initialize()
    original = make_paper()
    repository.insert_or_get_paper(
        original,
        course_id="course_1",
        class_id="class_1",
    )

    tampered = original.model_copy(update={"immutable_checksum": "tampered"})
    with pytest.raises(ValueError, match="immutable checksum"):
        repository.insert_or_get_paper(
            tampered,
            course_id="course_1",
            class_id="class_1",
        )


def test_repository_validates_scope_and_empty_recovery_paths(tmp_path) -> None:
    repository = SQLiteM8Repository(tmp_path / "empty-paths.sqlite3")
    repository.initialize()
    paper = make_paper()

    with pytest.raises(ValueError, match="scope"):
        repository.insert_or_get_paper(
            paper,
            course_id=" ",
            class_id="class_1",
        )
    with pytest.raises(ValueError, match="requires course and class"):
        repository.save_paper(paper)
    assert repository.get_paper_execution_context("missing") is None
    assert repository.get_score_audit("missing", 1) is None
    assert repository.get_scoring_result_by_checksum(
        "missing",
        "0" * 64,
    ) is None


def test_transient_assessment_can_be_purged_after_result_delivery(tmp_path) -> None:
    repository = SQLiteM8Repository(tmp_path / "transient-purge.sqlite3")
    repository.initialize()
    paper = make_paper()
    scoring = make_scoring_bundle(paper)
    repository.insert_or_get_paper_record(_record(paper))
    repository.insert_or_get_scoring_result(scoring)

    repository.purge_assessment_attempt(
        paper_id=paper.paper_id,
        attempt_id=scoring.attempt_id,
    )

    assert repository.get_paper(paper.paper_id) is None
    assert repository.get_paper_record(paper.paper_id) is None
    assert repository.get_scoring_result(scoring.attempt_id) is None
    audit = scoring.score_audit_records[0]
    assert repository.get_score_audit(audit.audit_id, audit.audit_version) is None


def test_same_audit_version_cannot_overwrite_content(tmp_path) -> None:
    repository = SQLiteM8Repository(tmp_path / "audit.sqlite3")
    repository.initialize()
    audit = make_scoring_bundle(make_paper()).score_audit_records[0]
    changed = ScoreAuditRecord(
        **{
            **audit.model_dump(mode="python"),
            "criterion_scores": [
                audit.criterion_scores[0].model_copy(
                    update={"reason": "Changed reason."}
                )
            ],
        }
    )

    repository.save_score_audit(audit)

    with pytest.raises(RuntimeError, match="conflict"):
        repository.save_score_audit(changed)


def test_frozen_paper_and_record_are_saved_in_one_transaction(
    tmp_path,
    monkeypatch,
) -> None:
    repository = SQLiteM8Repository(tmp_path / "atomic-paper.sqlite3")
    repository.initialize()
    record = _record(make_paper())

    def fail_record_insert(*_args, **_kwargs):
        raise RuntimeError("injected frozen-record failure")

    monkeypatch.setattr(
        repository,
        "_insert_or_validate_paper_record",
        fail_record_insert,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="injected frozen-record failure"):
        repository.insert_or_get_paper_record(record)

    assert repository.get_paper(record.paper.paper_id) is None
    assert repository.get_paper_record(record.paper.paper_id) is None


def test_frozen_record_checksum_detects_database_tampering(tmp_path) -> None:
    database_path = tmp_path / "tampered-record.sqlite3"
    repository = SQLiteM8Repository(database_path)
    repository.initialize()
    record = _record(make_paper())
    repository.insert_or_get_paper_record(record)
    tampered = record.model_copy(update={"class_id": "class_tampered"})

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE m8_frozen_assessment_records
            SET payload = ?
            WHERE paper_id = ?
            """,
            (dumps_json(tampered.to_dict()), record.paper.paper_id),
        )

    with pytest.raises(RuntimeError, match="checksum"):
        repository.get_paper_record(record.paper.paper_id)


def test_frozen_record_detects_scope_and_schema_tampering(tmp_path) -> None:
    database_path = tmp_path / "tampered-scope.sqlite3"
    repository = SQLiteM8Repository(database_path)
    repository.initialize()
    record = _record(make_paper())
    repository.insert_or_get_paper_record(record)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE m8_assessment_papers
            SET class_id = 'class_tampered'
            WHERE paper_id = ?
            """,
            (record.paper.paper_id,),
        )
    with pytest.raises(RuntimeError, match="inconsistent"):
        repository.get_paper_record(record.paper.paper_id)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE m8_assessment_papers
            SET class_id = 'class_1'
            WHERE paper_id = ?
            """,
            (record.paper.paper_id,),
        )
        connection.execute(
            """
            UPDATE m8_frozen_assessment_records
            SET schema_version = '2.0.0'
            WHERE paper_id = ?
            """,
            (record.paper.paper_id,),
        )
    with pytest.raises(RuntimeError, match="schema version"):
        repository.get_paper_record(record.paper.paper_id)


def test_same_paper_can_never_replace_its_frozen_rubric(tmp_path) -> None:
    repository = SQLiteM8Repository(tmp_path / "rubric-conflict.sqlite3")
    repository.initialize()
    paper = make_paper(subjective=True)
    original = FrozenAssessmentRecord(
        paper=paper,
        course_id="course_1",
        class_id="class_1",
        frozen_rubrics=[make_rubric(version="1.0.0")],
    )
    changed = FrozenAssessmentRecord(
        paper=paper,
        course_id="course_1",
        class_id="class_1",
        frozen_rubrics=[make_rubric(version="2.0.0")],
    )
    repository.insert_or_get_paper_record(original)

    with pytest.raises(RuntimeError, match="paper record conflict"):
        repository.insert_or_get_paper_record(changed)


def test_initialize_migrates_legacy_double_score_review_policy(tmp_path) -> None:
    database_path = tmp_path / "legacy-review-policy.sqlite3"
    repository = SQLiteM8Repository(database_path)
    repository.initialize()
    record = FrozenAssessmentRecord(
        paper=make_paper(subjective=True),
        course_id="course_1",
        class_id="class_1",
        frozen_rubrics=[make_rubric()],
    )
    repository.insert_or_get_paper_record(record)

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT payload
            FROM m8_frozen_assessment_records
            WHERE paper_id = ?
            """,
            (record.paper.paper_id,),
        ).fetchone()
        legacy = json.loads(str(row[0]))
        legacy["frozen_rubrics"][0]["review_policy"][
            "double_score_disagreement_threshold"
        ] = 1.0
        legacy_payload = dumps_json(legacy)
        legacy_checksum = hashlib.sha256(
            json.dumps(
                legacy,
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        connection.execute(
            """
            UPDATE m8_frozen_assessment_records
            SET payload = ?, payload_checksum = ?
            WHERE paper_id = ?
            """,
            (legacy_payload, legacy_checksum, record.paper.paper_id),
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 20")

    repository.initialize()

    assert repository.get_paper_record(record.paper.paper_id) == record
    with sqlite3.connect(database_path) as connection:
        payload, checksum = connection.execute(
            """
            SELECT payload, payload_checksum
            FROM m8_frozen_assessment_records
            WHERE paper_id = ?
            """,
            (record.paper.paper_id,),
        ).fetchone()
        assert current_schema_version(connection) == 21
    assert "double_score_disagreement_threshold" not in str(payload)
    assert str(checksum) == record.content_checksum()


def test_legacy_review_policy_migration_rejects_bad_checksum(tmp_path) -> None:
    database_path = tmp_path / "tampered-legacy-review-policy.sqlite3"
    repository = SQLiteM8Repository(database_path)
    repository.initialize()
    record = FrozenAssessmentRecord(
        paper=make_paper(subjective=True),
        course_id="course_1",
        class_id="class_1",
        frozen_rubrics=[make_rubric()],
    )
    repository.insert_or_get_paper_record(record)

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT payload FROM m8_frozen_assessment_records WHERE paper_id = ?",
            (record.paper.paper_id,),
        ).fetchone()
        legacy = json.loads(str(row[0]))
        legacy["frozen_rubrics"][0]["review_policy"][
            "double_score_disagreement_threshold"
        ] = 1.0
        connection.execute(
            """
            UPDATE m8_frozen_assessment_records
            SET payload = ?, payload_checksum = ?
            WHERE paper_id = ?
            """,
            (dumps_json(legacy), "0" * 64, record.paper.paper_id),
        )
        connection.execute("DELETE FROM schema_migrations WHERE version = 20")

    with pytest.raises(RuntimeError, match="legacy paper-record checksum mismatch"):
        repository.initialize()

    with sqlite3.connect(database_path) as connection:
        assert current_schema_version(connection) == 21
        assert connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 20"
        ).fetchone() is None
        payload = connection.execute(
            "SELECT payload FROM m8_frozen_assessment_records WHERE paper_id = ?",
            (record.paper.paper_id,),
        ).fetchone()[0]
    assert "double_score_disagreement_threshold" in str(payload)
