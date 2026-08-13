CREATE TABLE m9_teacher_reviews (
    decision_id TEXT PRIMARY KEY CHECK (length(decision_id) > 0),
    audit_id TEXT NOT NULL CHECK (length(audit_id) > 0),
    expected_audit_version INTEGER NOT NULL
        CHECK (expected_audit_version > 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0),
    UNIQUE (audit_id, expected_audit_version)
);

CREATE TABLE m9_teacher_analytics (
    report_id TEXT PRIMARY KEY CHECK (length(report_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    generated_at TIMESTAMPTZ NOT NULL,
    learner_ids JSONB NOT NULL CHECK (jsonb_typeof(learner_ids) = 'array'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0)
);
