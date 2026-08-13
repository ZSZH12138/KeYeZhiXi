CREATE TABLE m0_learning_events (
    event_id TEXT PRIMARY KEY CHECK (length(event_id) > 0),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (length(idempotency_key) > 0),
    event_type TEXT NOT NULL CHECK (length(event_type) > 0),
    occurred_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object')
);

CREATE TABLE m0_event_outbox (
    event_id TEXT PRIMARY KEY
        REFERENCES m0_learning_events(event_id) ON DELETE CASCADE
        CHECK (length(event_id) > 0),
    record TEXT NOT NULL CHECK (jsonb_typeof(record::jsonb) = 'object'),
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'processing', 'dead')),
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    version INTEGER NOT NULL CHECK (version >= 1),
    available_at TIMESTAMPTZ NOT NULL,
    locked_by TEXT CHECK (
        locked_by IS NULL
        OR locked_by ~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$'
    ),
    locked_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    last_error_code TEXT CHECK (
        last_error_code IS NULL
        OR last_error_code ~ '^[A-Z][A-Z0-9_]{0,127}$'
    ),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK (updated_at >= created_at),
    CHECK (
        (
            status = 'processing'
            AND attempt_count >= 1
            AND locked_by IS NOT NULL
            AND locked_at IS NOT NULL
            AND lease_until IS NOT NULL
            AND lease_until > locked_at
        )
        OR (
            status IN ('pending', 'dead')
            AND locked_by IS NULL
            AND locked_at IS NULL
            AND lease_until IS NULL
        )
    )
);

CREATE INDEX m0_outbox_claim_order
ON m0_event_outbox(status, available_at, created_at, event_id);

CREATE TABLE m0_assessment_runs (
    operation_id TEXT PRIMARY KEY CHECK (length(operation_id) > 0),
    operation TEXT NOT NULL
        CHECK (operation IN ('start', 'submit', 'review')),
    request_checksum TEXT NOT NULL CHECK (length(request_checksum) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    session_id TEXT NOT NULL CHECK (length(session_id) > 0),
    task_id TEXT NOT NULL CHECK (length(task_id) > 0),
    paper_id TEXT NOT NULL CHECK (length(paper_id) > 0),
    attempt_id TEXT CHECK (attempt_id IS NULL OR length(attempt_id) > 0),
    feedback_id TEXT CHECK (
        feedback_id IS NULL OR length(feedback_id) > 0
    ),
    report_id TEXT CHECK (report_id IS NULL OR length(report_id) > 0),
    scoring_result_checksum CHAR(64) CHECK (
        scoring_result_checksum IS NULL
        OR scoring_result_checksum ~ '^[0-9a-f]{64}$'
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
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'running', 'failed', 'completed')),
    version INTEGER NOT NULL CHECK (version > 0),
    locked_by TEXT CHECK (locked_by IS NULL OR length(locked_by) > 0),
    lease_until TIMESTAMPTZ,
    error_code TEXT CHECK (error_code IS NULL OR length(error_code) > 0),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK ((locked_by IS NULL) = (lease_until IS NULL)),
    CHECK (operation = 'start' OR attempt_id IS NOT NULL)
);

CREATE UNIQUE INDEX m0_one_submit_per_paper
ON m0_assessment_runs(paper_id)
WHERE operation = 'submit';
