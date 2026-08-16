CREATE TABLE m8_frozen_assessment_records (
    paper_id TEXT PRIMARY KEY CHECK (length(paper_id) > 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0)
);

CREATE TABLE m5_learning_observations (
    observation_id TEXT PRIMARY KEY CHECK (length(observation_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    attempt_id TEXT NOT NULL CHECK (length(attempt_id) > 0),
    occurred_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0)
);

CREATE TABLE m5_dina_models (
    model_id TEXT PRIMARY KEY CHECK (length(model_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    model_version TEXT NOT NULL CHECK (length(model_version) > 0),
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0),
    UNIQUE (course_id, model_version)
);

CREATE TABLE m5_bkt_models (
    model_id TEXT PRIMARY KEY CHECK (length(model_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    model_version TEXT NOT NULL CHECK (length(model_version) > 0),
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0),
    UNIQUE (course_id, model_version)
);

CREATE TABLE m5_knowledge_traces (
    trace_id TEXT PRIMARY KEY CHECK (length(trace_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    model_version TEXT NOT NULL CHECK (length(model_version) > 0),
    updated_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0)
);

CREATE TABLE m8_irt_calibration_runs (
    run_id TEXT PRIMARY KEY CHECK (length(run_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    model_version TEXT NOT NULL CHECK (length(model_version) > 0),
    generated_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0)
);

CREATE TABLE m8_irt_parameter_sets (
    parameter_set_id TEXT PRIMARY KEY CHECK (length(parameter_set_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    version TEXT NOT NULL CHECK (length(version) > 0),
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0),
    UNIQUE (course_id, version)
);

CREATE TABLE m8_ability_estimates (
    estimate_id TEXT PRIMARY KEY CHECK (length(estimate_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    parameter_set_id TEXT NOT NULL CHECK (length(parameter_set_id) > 0),
    estimated_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0)
);

CREATE TABLE m8_adaptive_selections (
    selection_id TEXT PRIMARY KEY CHECK (length(selection_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    selected_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0)
);

CREATE TABLE m8_calibration_reviews (
    decision_id TEXT PRIMARY KEY CHECK (length(decision_id) > 0),
    calibration_run_id TEXT NOT NULL UNIQUE
        CHECK (length(calibration_run_id) > 0),
    reviewed_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0)
);
