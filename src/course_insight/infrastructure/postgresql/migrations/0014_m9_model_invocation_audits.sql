CREATE TABLE m9_model_invocation_audits (
    invocation_id TEXT PRIMARY KEY CHECK (length(invocation_id) > 0),
    request_id TEXT NOT NULL UNIQUE CHECK (length(request_id) > 0),
    source_report_id TEXT NOT NULL
        REFERENCES m9_teacher_analytics(report_id)
        ON DELETE RESTRICT,
    scope TEXT NOT NULL CHECK (scope = 'class_aggregate'),
    provider TEXT NOT NULL CHECK (provider = 'deepseek'),
    model_name TEXT NOT NULL CHECK (length(model_name) > 0),
    provider_status TEXT NOT NULL CHECK (
        provider_status IN ('not_run', 'succeeded', 'failed', 'blocked')
    ),
    validation_status TEXT NOT NULL CHECK (
        validation_status IN ('not_run', 'passed', 'blocked')
    ),
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$')
);
