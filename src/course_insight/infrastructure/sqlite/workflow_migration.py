"""DDL and compatibility repair for payload-free M0 workflow metadata."""

from __future__ import annotations

import sqlite3


ASSESSMENT_RUNS_V5_SQL = """
CREATE TABLE IF NOT EXISTS m0_assessment_runs (
    operation_id TEXT PRIMARY KEY CHECK (length(operation_id) > 0),
    operation TEXT NOT NULL CHECK (
        operation IN ('start', 'submit', 'review')
    ),
    request_checksum TEXT NOT NULL CHECK (length(request_checksum) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    session_id TEXT NOT NULL CHECK (length(session_id) > 0),
    task_id TEXT NOT NULL CHECK (length(task_id) > 0),
    paper_id TEXT NOT NULL CHECK (length(paper_id) > 0),
    attempt_id TEXT CHECK (
        attempt_id IS NULL OR length(attempt_id) > 0
    ),
    feedback_id TEXT CHECK (
        feedback_id IS NULL OR length(feedback_id) > 0
    ),
    report_id TEXT CHECK (
        report_id IS NULL OR length(report_id) > 0
    ),
    checkpoint TEXT NOT NULL CHECK (length(checkpoint) > 0),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'failed', 'completed')
    ),
    version INTEGER NOT NULL CHECK (version > 0),
    locked_by TEXT CHECK (
        locked_by IS NULL OR length(locked_by) > 0
    ),
    lease_until TEXT CHECK (
        lease_until IS NULL OR length(lease_until) > 0
    ),
    error_code TEXT CHECK (
        error_code IS NULL OR length(error_code) > 0
    ),
    created_at TEXT NOT NULL CHECK (length(created_at) > 0),
    updated_at TEXT NOT NULL CHECK (length(updated_at) > 0),
    CHECK ((locked_by IS NULL) = (lease_until IS NULL)),
    CHECK (operation = 'start' OR attempt_id IS NOT NULL)
)
"""
ASSESSMENT_RUNS_V6_SQL = """
CREATE TABLE IF NOT EXISTS m0_assessment_runs (
    operation_id TEXT PRIMARY KEY CHECK (length(operation_id) > 0),
    operation TEXT NOT NULL CHECK (
        operation IN ('start', 'submit', 'review')
    ),
    request_checksum TEXT NOT NULL CHECK (length(request_checksum) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    session_id TEXT NOT NULL CHECK (length(session_id) > 0),
    task_id TEXT NOT NULL CHECK (length(task_id) > 0),
    paper_id TEXT NOT NULL CHECK (length(paper_id) > 0),
    attempt_id TEXT CHECK (
        attempt_id IS NULL OR length(attempt_id) > 0
    ),
    feedback_id TEXT CHECK (
        feedback_id IS NULL OR length(feedback_id) > 0
    ),
    report_id TEXT CHECK (
        report_id IS NULL OR length(report_id) > 0
    ),
    scoring_result_checksum TEXT CHECK (
        scoring_result_checksum IS NULL
        OR length(scoring_result_checksum) = 64
    ),
    target_audit_id TEXT CHECK (
        target_audit_id IS NULL OR length(target_audit_id) > 0
    ),
    target_audit_version INTEGER CHECK (
        target_audit_version IS NULL OR target_audit_version > 0
    ),
    state_version INTEGER CHECK (
        state_version IS NULL OR state_version > 0
    ),
    checkpoint TEXT NOT NULL CHECK (length(checkpoint) > 0),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'failed', 'completed')
    ),
    version INTEGER NOT NULL CHECK (version > 0),
    locked_by TEXT CHECK (
        locked_by IS NULL OR length(locked_by) > 0
    ),
    lease_until TEXT CHECK (
        lease_until IS NULL OR length(lease_until) > 0
    ),
    error_code TEXT CHECK (
        error_code IS NULL OR length(error_code) > 0
    ),
    created_at TEXT NOT NULL CHECK (length(created_at) > 0),
    updated_at TEXT NOT NULL CHECK (length(updated_at) > 0),
    CHECK ((locked_by IS NULL) = (lease_until IS NULL)),
    CHECK (operation = 'start' OR attempt_id IS NOT NULL)
)
"""
ASSESSMENT_RUNS_V9_SQL = """
CREATE TABLE IF NOT EXISTS m0_assessment_runs (
    operation_id TEXT PRIMARY KEY CHECK (length(operation_id) > 0),
    operation TEXT NOT NULL CHECK (
        operation IN ('start', 'submit', 'review')
    ),
    request_checksum TEXT NOT NULL CHECK (length(request_checksum) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    session_id TEXT NOT NULL CHECK (length(session_id) > 0),
    task_id TEXT NOT NULL CHECK (length(task_id) > 0),
    paper_id TEXT NOT NULL CHECK (length(paper_id) > 0),
    attempt_id TEXT CHECK (
        attempt_id IS NULL OR length(attempt_id) > 0
    ),
    feedback_id TEXT CHECK (
        feedback_id IS NULL OR length(feedback_id) > 0
    ),
    report_id TEXT CHECK (
        report_id IS NULL OR length(report_id) > 0
    ),
    scoring_result_checksum TEXT CHECK (
        scoring_result_checksum IS NULL
        OR (
            length(scoring_result_checksum) = 64
            AND scoring_result_checksum NOT GLOB '*[^0-9a-f]*'
        )
    ),
    target_audit_id TEXT CHECK (
        target_audit_id IS NULL OR length(target_audit_id) > 0
    ),
    target_audit_version INTEGER CHECK (
        target_audit_version IS NULL OR target_audit_version > 0
    ),
    state_version INTEGER CHECK (
        state_version IS NULL OR state_version > 0
    ),
    knowledge_bundle_id TEXT CHECK (
        knowledge_bundle_id IS NULL OR length(knowledge_bundle_id) > 0
    ),
    knowledge_bundle_version TEXT CHECK (
        knowledge_bundle_version IS NULL
        OR length(knowledge_bundle_version) > 0
    ),
    knowledge_bundle_checksum TEXT CHECK (
        knowledge_bundle_checksum IS NULL
        OR (
            length(knowledge_bundle_checksum) = 64
            AND knowledge_bundle_checksum NOT GLOB '*[^0-9a-f]*'
        )
    ),
    course_package_id TEXT CHECK (
        course_package_id IS NULL OR length(course_package_id) > 0
    ),
    evidence_index_id TEXT CHECK (
        evidence_index_id IS NULL OR length(evidence_index_id) > 0
    ),
    evidence_index_version TEXT CHECK (
        evidence_index_version IS NULL OR length(evidence_index_version) > 0
    ),
    evidence_index_checksum TEXT CHECK (
        evidence_index_checksum IS NULL
        OR (
            length(evidence_index_checksum) = 64
            AND evidence_index_checksum NOT GLOB '*[^0-9a-f]*'
        )
    ),
    state_policy_checksum TEXT CHECK (
        state_policy_checksum IS NULL
        OR (
            length(state_policy_checksum) = 64
            AND state_policy_checksum NOT GLOB '*[^0-9a-f]*'
        )
    ),
    teacher_policy_checksum TEXT CHECK (
        teacher_policy_checksum IS NULL
        OR (
            length(teacher_policy_checksum) = 64
            AND teacher_policy_checksum NOT GLOB '*[^0-9a-f]*'
        )
    ),
    previous_state_frozen INTEGER CHECK (
        previous_state_frozen IS NULL OR previous_state_frozen IN (0, 1)
    ),
    previous_learner_snapshot_id TEXT CHECK (
        previous_learner_snapshot_id IS NULL
        OR length(previous_learner_snapshot_id) > 0
    ),
    previous_learner_state_version INTEGER CHECK (
        previous_learner_state_version IS NULL
        OR previous_learner_state_version > 0
    ),
    previous_class_snapshot_id TEXT CHECK (
        previous_class_snapshot_id IS NULL
        OR length(previous_class_snapshot_id) > 0
    ),
    previous_class_state_version INTEGER CHECK (
        previous_class_state_version IS NULL
        OR previous_class_state_version > 0
    ),
    checkpoint TEXT NOT NULL CHECK (length(checkpoint) > 0),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'failed', 'completed')
    ),
    version INTEGER NOT NULL CHECK (version > 0),
    locked_by TEXT CHECK (
        locked_by IS NULL OR length(locked_by) > 0
    ),
    lease_until TEXT CHECK (
        lease_until IS NULL OR length(lease_until) > 0
    ),
    error_code TEXT CHECK (
        error_code IS NULL OR length(error_code) > 0
    ),
    created_at TEXT NOT NULL CHECK (length(created_at) > 0),
    updated_at TEXT NOT NULL CHECK (length(updated_at) > 0),
    CHECK ((locked_by IS NULL) = (lease_until IS NULL)),
    CHECK (operation = 'start' OR attempt_id IS NOT NULL),
    CHECK (
        (
            knowledge_bundle_id IS NULL
            AND knowledge_bundle_version IS NULL
            AND knowledge_bundle_checksum IS NULL
            AND course_package_id IS NULL
        )
        OR (
            knowledge_bundle_id IS NOT NULL
            AND knowledge_bundle_version IS NOT NULL
            AND knowledge_bundle_checksum IS NOT NULL
            AND course_package_id IS NOT NULL
        )
    ),
    CHECK (
        (
            evidence_index_id IS NULL
            AND evidence_index_version IS NULL
            AND evidence_index_checksum IS NULL
        )
        OR (
            evidence_index_id IS NOT NULL
            AND evidence_index_version IS NOT NULL
            AND evidence_index_checksum IS NOT NULL
        )
    ),
    CHECK (
        (previous_learner_snapshot_id IS NULL)
        = (previous_learner_state_version IS NULL)
    ),
    CHECK (
        previous_class_snapshot_id IS NOT NULL
        OR previous_class_state_version IS NULL
    ),
    CHECK (
        COALESCE(previous_state_frozen = 1, 0)
        OR (
            previous_learner_snapshot_id IS NULL
            AND previous_learner_state_version IS NULL
            AND previous_class_snapshot_id IS NULL
            AND previous_class_state_version IS NULL
        )
    )
)
"""
ASSESSMENT_RUNS_V11_SQL = ASSESSMENT_RUNS_V9_SQL.replace(
    "    checkpoint TEXT NOT NULL CHECK (length(checkpoint) > 0),",
    """    policy_id TEXT CHECK (
        policy_id IS NULL OR length(policy_id) > 0
    ),
    adapter_id TEXT CHECK (
        adapter_id IS NULL OR length(adapter_id) > 0
    ),
    adapter_version TEXT CHECK (
        adapter_version IS NULL OR length(adapter_version) > 0
    ),
    artifact_sha256 TEXT CHECK (
        artifact_sha256 IS NULL
        OR (
            length(artifact_sha256) = 64
            AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    feature_schema_version TEXT CHECK (
        feature_schema_version IS NULL
        OR length(feature_schema_version) > 0
    ),
    action_space_version TEXT CHECK (
        action_space_version IS NULL
        OR length(action_space_version) > 0
    ),
    gate_policy_version TEXT CHECK (
        gate_policy_version IS NULL
        OR length(gate_policy_version) > 0
    ),
    checkpoint TEXT NOT NULL CHECK (length(checkpoint) > 0),""",
).replace(
    "    CHECK ((locked_by IS NULL) = (lease_until IS NULL)),",
    """    CHECK ((locked_by IS NULL) = (lease_until IS NULL)),
    CHECK (
        (
            policy_id IS NULL
            AND adapter_id IS NULL
            AND adapter_version IS NULL
            AND artifact_sha256 IS NULL
            AND feature_schema_version IS NULL
            AND action_space_version IS NULL
            AND gate_policy_version IS NULL
        )
        OR (
            operation = 'submit'
            AND checkpoint IN (
                'policy_frozen',
                'tutoring_saved',
                'feedback_saved',
                'analytics_saved',
                'completed'
            )
            AND state_version IS NOT NULL
            AND previous_state_frozen = 1
            AND policy_id IS NOT NULL
            AND adapter_id IS NOT NULL
            AND adapter_version IS NOT NULL
            AND feature_schema_version IS NOT NULL
            AND action_space_version IS NOT NULL
            AND gate_policy_version IS NOT NULL
        )
    ),""",
)
ASSESSMENT_RUNS_V18_SQL = ASSESSMENT_RUNS_V11_SQL.replace(
    "status IN ('pending', 'running', 'failed', 'completed')",
    "status IN ('pending', 'running', 'failed', 'completed', "
    "'awaiting_review', 'awaiting_rescore')",
)
ASSESSMENT_RUNS_V19_SQL = ASSESSMENT_RUNS_V18_SQL.replace(
    "operation IN ('start', 'submit', 'review')",
    "operation IN ('start', 'submit', 'review', 'rescore')",
)
ASSESSMENT_RUNS_SUBMIT_INDEX_SQL = """
CREATE UNIQUE INDEX m0_one_submit_per_paper
ON m0_assessment_runs(paper_id)
WHERE operation = 'submit'
"""
ASSESSMENT_RUNS_NONTERMINAL_REVIEW_INDEX_SQL = """
CREATE UNIQUE INDEX m0_one_nonterminal_review_per_paper
ON m0_assessment_runs(paper_id)
WHERE operation = 'review' AND status <> 'completed'
"""
ASSESSMENT_RUNS_NONTERMINAL_RESCORE_INDEX_SQL = """
CREATE UNIQUE INDEX m0_one_nonterminal_rescore_per_paper
ON m0_assessment_runs(paper_id)
WHERE operation = 'rescore' AND status <> 'completed'
"""

_REFERENCE_COLUMNS = {
    "scoring_result_checksum",
    "target_audit_id",
    "target_audit_version",
    "state_version",
}
_REFERENCE_COLUMNS_IN_ORDER = (
    "scoring_result_checksum",
    "target_audit_id",
    "target_audit_version",
    "state_version",
)
_COPY_COLUMNS = (
    "operation_id",
    "operation",
    "request_checksum",
    "course_id",
    "class_id",
    "learner_id",
    "session_id",
    "task_id",
    "paper_id",
    "attempt_id",
    "feedback_id",
    "report_id",
    "checkpoint",
    "status",
    "version",
    "locked_by",
    "lease_until",
    "error_code",
    "created_at",
    "updated_at",
)


def migrate_workflow_v5_to_v6(connection: sqlite3.Connection) -> None:
    """Add exact-result references while retaining every v5 workflow row."""

    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info('m0_assessment_runs')"
        ).fetchall()
    }
    if not columns or columns & _REFERENCE_COLUMNS:
        raise RuntimeError("M0 workflow v5 schema is incompatible")
    connection.execute("DROP INDEX IF EXISTS m0_one_submit_per_paper")
    connection.execute(
        "ALTER TABLE m0_assessment_runs RENAME TO m0_assessment_runs_v5"
    )
    connection.execute(ASSESSMENT_RUNS_V6_SQL)
    names = ", ".join(_COPY_COLUMNS)
    connection.execute(
        f"""
        INSERT INTO m0_assessment_runs({names})
        SELECT {names}
        FROM m0_assessment_runs_v5
        """
    )
    connection.execute("DROP TABLE m0_assessment_runs_v5")
    connection.execute(ASSESSMENT_RUNS_SUBMIT_INDEX_SQL)


def migrate_workflow_v7_to_v8(connection: sqlite3.Connection) -> None:
    """Enforce one nonterminal review chain per paper."""

    duplicate = connection.execute(
        """
        SELECT paper_id
        FROM m0_assessment_runs
        WHERE operation = 'review' AND status <> 'completed'
        GROUP BY paper_id
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if duplicate is not None:
        raise RuntimeError(
            "M0 workflow review history has multiple nonterminal rows for one paper"
        )
    connection.execute(
        "DROP INDEX IF EXISTS m0_one_nonterminal_review_per_paper"
    )
    connection.execute(ASSESSMENT_RUNS_NONTERMINAL_REVIEW_INDEX_SQL)


def migrate_workflow_v8_to_v9(connection: sqlite3.Connection) -> None:
    """Add deterministic dependency and exact pre-state metadata."""

    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info('m0_assessment_runs')"
        ).fetchall()
    }
    if not columns or columns & {
        "knowledge_bundle_id",
        "previous_state_frozen",
    }:
        raise RuntimeError("M0 workflow v8 schema is incompatible")
    connection.execute(
        "DROP INDEX IF EXISTS m0_one_nonterminal_review_per_paper"
    )
    connection.execute("DROP INDEX IF EXISTS m0_one_submit_per_paper")
    connection.execute(
        "ALTER TABLE m0_assessment_runs RENAME TO m0_assessment_runs_v8"
    )
    connection.execute(ASSESSMENT_RUNS_V9_SQL)
    names = ", ".join(
        (
            *_COPY_COLUMNS[:12],
            *_REFERENCE_COLUMNS_IN_ORDER,
            *_COPY_COLUMNS[12:],
        )
    )
    connection.execute(
        f"""
        INSERT INTO m0_assessment_runs({names})
        SELECT {names}
        FROM m0_assessment_runs_v8
        """
    )
    connection.execute("DROP TABLE m0_assessment_runs_v8")
    connection.execute(ASSESSMENT_RUNS_SUBMIT_INDEX_SQL)
    connection.execute(ASSESSMENT_RUNS_NONTERMINAL_REVIEW_INDEX_SQL)


def migrate_workflow_v10_to_v11(connection: sqlite3.Connection) -> None:
    """Add complete nullable policy identities while retaining every v10 row."""

    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info('m0_assessment_runs')"
        ).fetchall()
    }
    if not columns or columns & {
        "policy_id",
        "gate_policy_version",
    }:
        raise RuntimeError("M0 workflow v10 schema is incompatible")
    connection.execute(
        "DROP INDEX IF EXISTS m0_one_nonterminal_review_per_paper"
    )
    connection.execute("DROP INDEX IF EXISTS m0_one_submit_per_paper")
    connection.execute(
        "ALTER TABLE m0_assessment_runs RENAME TO m0_assessment_runs_v10"
    )
    connection.execute(ASSESSMENT_RUNS_V11_SQL)
    names = ", ".join(
        (
            *_COPY_COLUMNS[:12],
            *_REFERENCE_COLUMNS_IN_ORDER,
            "knowledge_bundle_id",
            "knowledge_bundle_version",
            "knowledge_bundle_checksum",
            "course_package_id",
            "evidence_index_id",
            "evidence_index_version",
            "evidence_index_checksum",
            "state_policy_checksum",
            "teacher_policy_checksum",
            "previous_state_frozen",
            "previous_learner_snapshot_id",
            "previous_learner_state_version",
            "previous_class_snapshot_id",
            "previous_class_state_version",
            *_COPY_COLUMNS[12:],
        )
    )
    connection.execute(
        f"""
        INSERT INTO m0_assessment_runs({names})
        SELECT {names}
        FROM m0_assessment_runs_v10
        """
    )
    connection.execute("DROP TABLE m0_assessment_runs_v10")
    connection.execute(ASSESSMENT_RUNS_SUBMIT_INDEX_SQL)
    connection.execute(ASSESSMENT_RUNS_NONTERMINAL_REVIEW_INDEX_SQL)


def migrate_workflow_v17_to_v18(connection: sqlite3.Connection) -> None:
    """Allow teacher waiting-room statuses without rewriting earlier ledgers."""

    connection.execute(
        "DROP INDEX IF EXISTS m0_one_nonterminal_review_per_paper"
    )
    connection.execute("DROP INDEX IF EXISTS m0_one_submit_per_paper")
    connection.execute(
        "ALTER TABLE m0_assessment_runs RENAME TO m0_assessment_runs_v17"
    )
    connection.execute(ASSESSMENT_RUNS_V18_SQL)
    names = ", ".join(
        (
            *_COPY_COLUMNS[:12],
            *_REFERENCE_COLUMNS_IN_ORDER,
            "knowledge_bundle_id",
            "knowledge_bundle_version",
            "knowledge_bundle_checksum",
            "course_package_id",
            "evidence_index_id",
            "evidence_index_version",
            "evidence_index_checksum",
            "state_policy_checksum",
            "teacher_policy_checksum",
            "previous_state_frozen",
            "previous_learner_snapshot_id",
            "previous_learner_state_version",
            "previous_class_snapshot_id",
            "previous_class_state_version",
            "policy_id",
            "adapter_id",
            "adapter_version",
            "artifact_sha256",
            "feature_schema_version",
            "action_space_version",
            "gate_policy_version",
            *_COPY_COLUMNS[12:],
        )
    )
    connection.execute(
        f"""
        INSERT INTO m0_assessment_runs({names})
        SELECT {names}
        FROM m0_assessment_runs_v17
        """
    )
    connection.execute("DROP TABLE m0_assessment_runs_v17")
    connection.execute(ASSESSMENT_RUNS_SUBMIT_INDEX_SQL)
    connection.execute(ASSESSMENT_RUNS_NONTERMINAL_REVIEW_INDEX_SQL)


_WORKFLOW_COPY_NAMES = ", ".join(
    (
        *_COPY_COLUMNS[:12],
        *_REFERENCE_COLUMNS_IN_ORDER,
        "knowledge_bundle_id",
        "knowledge_bundle_version",
        "knowledge_bundle_checksum",
        "course_package_id",
        "evidence_index_id",
        "evidence_index_version",
        "evidence_index_checksum",
        "state_policy_checksum",
        "teacher_policy_checksum",
        "previous_state_frozen",
        "previous_learner_snapshot_id",
        "previous_learner_state_version",
        "previous_class_snapshot_id",
        "previous_class_state_version",
        "policy_id",
        "adapter_id",
        "adapter_version",
        "artifact_sha256",
        "feature_schema_version",
        "action_space_version",
        "gate_policy_version",
        *_COPY_COLUMNS[12:],
    )
)


def migrate_workflow_v18_to_v19(connection: sqlite3.Connection) -> None:
    """Allow the model rescore operation without rewriting earlier ledgers."""

    connection.execute(
        "DROP INDEX IF EXISTS m0_one_nonterminal_review_per_paper"
    )
    connection.execute(
        "DROP INDEX IF EXISTS m0_one_nonterminal_rescore_per_paper"
    )
    connection.execute("DROP INDEX IF EXISTS m0_one_submit_per_paper")
    connection.execute(
        "ALTER TABLE m0_assessment_runs RENAME TO m0_assessment_runs_v18"
    )
    connection.execute(ASSESSMENT_RUNS_V19_SQL)
    connection.execute(
        f"""
        INSERT INTO m0_assessment_runs({_WORKFLOW_COPY_NAMES})
        SELECT {_WORKFLOW_COPY_NAMES}
        FROM m0_assessment_runs_v18
        """
    )
    connection.execute("DROP TABLE m0_assessment_runs_v18")
    connection.execute(ASSESSMENT_RUNS_SUBMIT_INDEX_SQL)
    connection.execute(ASSESSMENT_RUNS_NONTERMINAL_REVIEW_INDEX_SQL)
    connection.execute(ASSESSMENT_RUNS_NONTERMINAL_RESCORE_INDEX_SQL)
