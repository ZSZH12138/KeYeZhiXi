"""Append-only SQLite tables for M5/M8 model evidence and artifacts."""

from __future__ import annotations


def _payload_table(
    table_name: str,
    columns: str,
    primary_key: str,
    *constraints: str,
) -> str:
    constraint_sql = "".join(f",\n    {item}" for item in constraints)
    return f"""
CREATE TABLE IF NOT EXISTS {table_name} (
    {columns},
    payload TEXT NOT NULL CHECK (
        CASE WHEN json_valid(payload)
            THEN json(payload) = payload
            ELSE 0
        END
    ),
    payload_checksum TEXT NOT NULL CHECK (
        length(payload_checksum) = 64
        AND payload_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0),
    PRIMARY KEY ({primary_key}){constraint_sql}
)
"""


MODEL_RUNTIME_TABLES = (
    (
        "m8_frozen_assessment_records",
        _payload_table(
            "m8_frozen_assessment_records",
            "paper_id TEXT NOT NULL CHECK (length(paper_id) > 0)",
            "paper_id",
        ),
    ),
    (
        "m5_learning_observations",
        _payload_table(
            "m5_learning_observations",
            """observation_id TEXT NOT NULL CHECK (length(observation_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    attempt_id TEXT NOT NULL CHECK (length(attempt_id) > 0),
    occurred_at TEXT NOT NULL CHECK (length(occurred_at) > 0)""",
            "observation_id",
        ),
    ),
    (
        "m5_dina_models",
        _payload_table(
            "m5_dina_models",
            """model_id TEXT NOT NULL CHECK (length(model_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    model_version TEXT NOT NULL CHECK (length(model_version) > 0),
    created_at TEXT NOT NULL CHECK (length(created_at) > 0)""",
            "model_id",
            "UNIQUE (course_id, model_version)",
        ),
    ),
    (
        "m5_bkt_models",
        _payload_table(
            "m5_bkt_models",
            """model_id TEXT NOT NULL CHECK (length(model_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    model_version TEXT NOT NULL CHECK (length(model_version) > 0),
    created_at TEXT NOT NULL CHECK (length(created_at) > 0)""",
            "model_id",
            "UNIQUE (course_id, model_version)",
        ),
    ),
    (
        "m5_knowledge_traces",
        _payload_table(
            "m5_knowledge_traces",
            """trace_id TEXT NOT NULL CHECK (length(trace_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    model_version TEXT NOT NULL CHECK (length(model_version) > 0),
    updated_at TEXT NOT NULL CHECK (length(updated_at) > 0)""",
            "trace_id",
        ),
    ),
    (
        "m8_irt_calibration_runs",
        _payload_table(
            "m8_irt_calibration_runs",
            """run_id TEXT NOT NULL CHECK (length(run_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    model_version TEXT NOT NULL CHECK (length(model_version) > 0),
    generated_at TEXT NOT NULL CHECK (length(generated_at) > 0)""",
            "run_id",
        ),
    ),
    (
        "m8_irt_parameter_sets",
        _payload_table(
            "m8_irt_parameter_sets",
            """parameter_set_id TEXT NOT NULL CHECK (length(parameter_set_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    version TEXT NOT NULL CHECK (length(version) > 0),
    created_at TEXT NOT NULL CHECK (length(created_at) > 0)""",
            "parameter_set_id",
            "UNIQUE (course_id, version)",
        ),
    ),
    (
        "m8_ability_estimates",
        _payload_table(
            "m8_ability_estimates",
            """estimate_id TEXT NOT NULL CHECK (length(estimate_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    parameter_set_id TEXT NOT NULL CHECK (length(parameter_set_id) > 0),
    estimated_at TEXT NOT NULL CHECK (length(estimated_at) > 0)""",
            "estimate_id",
        ),
    ),
    (
        "m8_adaptive_selections",
        _payload_table(
            "m8_adaptive_selections",
            """selection_id TEXT NOT NULL CHECK (length(selection_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    selected_at TEXT NOT NULL CHECK (length(selected_at) > 0)""",
            "selection_id",
        ),
    ),
    (
        "m8_calibration_reviews",
        _payload_table(
            "m8_calibration_reviews",
            """decision_id TEXT NOT NULL CHECK (length(decision_id) > 0),
    calibration_run_id TEXT NOT NULL CHECK (length(calibration_run_id) > 0),
    reviewed_at TEXT NOT NULL CHECK (length(reviewed_at) > 0)""",
            "decision_id",
            "UNIQUE (calibration_run_id)",
        ),
    ),
)


LEARNING_OBSERVATION_AUDIT_TABLE = (
    "m5_learning_observation_audits",
    """
CREATE TABLE IF NOT EXISTS m5_learning_observation_audits (
    source_audit_id TEXT NOT NULL CHECK (length(source_audit_id) > 0),
    source_audit_version INTEGER NOT NULL CHECK (source_audit_version > 0),
    observation_id TEXT NOT NULL UNIQUE CHECK (length(observation_id) > 0),
    PRIMARY KEY (source_audit_id, source_audit_version)
)
""",
)


LEARNING_OBSERVATION_AUDIT_BACKFILL_SQL = """
INSERT OR IGNORE INTO m5_learning_observation_audits (
    source_audit_id,
    source_audit_version,
    observation_id
)
SELECT
    json_extract(payload, '$.source_audit_id'),
    CAST(json_extract(payload, '$.source_audit_version') AS INTEGER),
    observation_id
FROM m5_learning_observations
ORDER BY occurred_at, attempt_id, observation_id
"""


__all__ = [
    "LEARNING_OBSERVATION_AUDIT_BACKFILL_SQL",
    "LEARNING_OBSERVATION_AUDIT_TABLE",
    "MODEL_RUNTIME_TABLES",
]
