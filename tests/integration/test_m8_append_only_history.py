"""M8 paper and audit identities are append-only."""

import sqlite3
from datetime import timedelta

import pytest

from course_insight.contracts.assessment import AssessmentPaper, ScoreAuditRecord
from course_insight.infrastructure.sqlite.m8_repository import SQLiteM8Repository
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
    changed_payload = {
        **original.model_dump(mode="python"),
        "generated_at": original.generated_at + timedelta(minutes=1),
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
    changed_payload = {
        **original.model_dump(mode="python"),
        "generated_at": original.generated_at + timedelta(minutes=1),
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
