CREATE TABLE m7_student_feedback (
    feedback_id TEXT PRIMARY KEY CHECK (length(feedback_id) > 0),
    task_id TEXT NOT NULL CHECK (length(task_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0),
    UNIQUE (task_id, learner_id)
);
