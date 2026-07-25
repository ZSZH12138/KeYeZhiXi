CREATE TABLE m8_assessment_papers (
    paper_id TEXT PRIMARY KEY CHECK (length(paper_id) > 0),
    task_id TEXT NOT NULL UNIQUE CHECK (length(task_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0)
);

CREATE TABLE m8_score_audits (
    audit_id TEXT NOT NULL CHECK (length(audit_id) > 0),
    audit_version INTEGER NOT NULL CHECK (audit_version > 0),
    item_instance_id TEXT NOT NULL CHECK (length(item_instance_id) > 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0),
    PRIMARY KEY (audit_id, audit_version)
);

CREATE TABLE m8_scoring_results (
    attempt_id TEXT NOT NULL CHECK (length(attempt_id) > 0),
    result_key TEXT NOT NULL CHECK (length(result_key) > 0),
    paper_id TEXT NOT NULL CHECK (length(paper_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    finalized_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0),
    PRIMARY KEY (attempt_id, result_key)
);
