CREATE TABLE m4_task_plans (
    task_id TEXT PRIMARY KEY CHECK (length(task_id) > 0),
    idempotency_key CHAR(64) NOT NULL UNIQUE
        CHECK (idempotency_key ~ '^[0-9a-f]{64}$'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0)
);
