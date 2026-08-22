"""SQLite schema owned by the M7 privacy-minimized audit migration."""

M7_MODEL_INVOCATION_AUDITS_SQL = """
CREATE TABLE IF NOT EXISTS m7_model_invocation_audits (
    invocation_id TEXT PRIMARY KEY CHECK (length(invocation_id) > 0),
    request_id TEXT NOT NULL UNIQUE CHECK (length(request_id) > 0),
    scoring_task_id TEXT NOT NULL CHECK (length(scoring_task_id) > 0),
    provider TEXT NOT NULL CHECK (provider = 'deepseek'),
    model_name TEXT NOT NULL CHECK (length(model_name) > 0),
    provider_status TEXT NOT NULL CHECK (
        provider_status IN ('not_run', 'succeeded', 'failed', 'blocked')
    ),
    validation_status TEXT NOT NULL CHECK (
        validation_status IN ('not_run', 'passed', 'blocked')
    ),
    privacy_decision TEXT NOT NULL CHECK (
        privacy_decision IN ('allowed', 'redacted', 'blocked')
    ),
    created_at TEXT NOT NULL CHECK (
        datetime(created_at) IS NOT NULL
        AND instr(created_at, 'T') > 0
        AND (
            substr(created_at, -1) = 'Z'
            OR substr(created_at, -6, 1) IN ('+', '-')
        )
    ),
    payload TEXT NOT NULL CHECK (
        CASE WHEN json_valid(payload)
            THEN json(payload) = payload
                AND json_type(payload) = 'object'
                AND json_type(payload, '$.student_answer') IS NULL
                AND json_type(payload, '$.messages') IS NULL
                AND json_type(payload, '$.prompt') IS NULL
                AND json_type(payload, '$.provider_response') IS NULL
                AND json_type(payload, '$.structured_output') IS NULL
                AND json_type(payload, '$.content') IS NULL
            ELSE 0
        END
    ),
    payload_checksum TEXT NOT NULL CHECK (
        length(payload_checksum) = 64
        AND payload_checksum NOT GLOB '*[^0-9a-f]*'
    )
)
"""

M7_MODEL_INVOCATION_AUDITS_CREATED_AT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS m7_model_invocation_audits_created_at_idx
ON m7_model_invocation_audits(created_at)
"""


__all__ = [
    "M7_MODEL_INVOCATION_AUDITS_CREATED_AT_INDEX_SQL",
    "M7_MODEL_INVOCATION_AUDITS_SQL",
]
