CREATE TABLE m5_learner_states (
    snapshot_id TEXT NOT NULL CHECK (length(snapshot_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    state_version INTEGER NOT NULL CHECK (state_version > 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0),
    PRIMARY KEY (course_id, class_id, learner_id, state_version)
);

CREATE TABLE m5_class_states (
    snapshot_id TEXT NOT NULL CHECK (length(snapshot_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    state_version INTEGER NOT NULL CHECK (state_version > 0),
    aggregation_policy_version TEXT NOT NULL
        CHECK (length(aggregation_policy_version) > 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0),
    PRIMARY KEY (course_id, class_id, snapshot_id),
    UNIQUE (course_id, class_id, state_version)
);

CREATE TABLE m5_state_updates (
    attempt_id TEXT NOT NULL CHECK (length(attempt_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    state_version INTEGER NOT NULL CHECK (state_version > 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0),
    PRIMARY KEY (attempt_id, state_version)
);
