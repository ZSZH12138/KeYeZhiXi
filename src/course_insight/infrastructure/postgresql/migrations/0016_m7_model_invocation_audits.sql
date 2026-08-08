CREATE TABLE m7_model_invocation_audits (
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
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (
        jsonb_typeof(payload) = 'object'
        AND NOT (
            payload ?| ARRAY[
                'student_answer',
                'messages',
                'prompt',
                'provider_response',
                'structured_output',
                'content'
            ]
        )
    ),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$')
);

CREATE INDEX m7_model_invocation_audits_created_at_idx
    ON m7_model_invocation_audits(created_at);
