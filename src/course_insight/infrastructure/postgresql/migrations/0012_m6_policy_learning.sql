CREATE TABLE m6_policy_artifacts (
    policy_id TEXT PRIMARY KEY CHECK (length(policy_id) > 0),
    artifact_sha256 CHAR(64) NOT NULL UNIQUE
        CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$')
);

CREATE TABLE m6_policy_executions (
    request_fingerprint CHAR(64) PRIMARY KEY
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    policy_execution_fingerprint CHAR(64) NOT NULL UNIQUE
        CHECK (policy_execution_fingerprint ~ '^[0-9a-f]{64}$'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$')
);

CREATE TABLE m6_policy_observations (
    decision_id TEXT PRIMARY KEY CHECK (length(decision_id) > 0),
    request_fingerprint CHAR(64) NOT NULL UNIQUE
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    policy_execution_fingerprint CHAR(64) NOT NULL UNIQUE
        CHECK (policy_execution_fingerprint ~ '^[0-9a-f]{64}$'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    FOREIGN KEY (decision_id)
        REFERENCES m6_tutoring_decisions(decision_id),
    FOREIGN KEY (request_fingerprint)
        REFERENCES m6_policy_executions(request_fingerprint),
    FOREIGN KEY (policy_execution_fingerprint)
        REFERENCES m6_policy_executions(policy_execution_fingerprint)
);

CREATE TABLE m6_policy_rewards (
    reward_identity CHAR(64) PRIMARY KEY
        CHECK (reward_identity ~ '^[0-9a-f]{64}$'),
    policy_execution_fingerprint CHAR(64) NOT NULL
        CHECK (policy_execution_fingerprint ~ '^[0-9a-f]{64}$'),
    reward_version TEXT NOT NULL CHECK (length(reward_version) > 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    UNIQUE (policy_execution_fingerprint, reward_version),
    FOREIGN KEY (policy_execution_fingerprint)
        REFERENCES m6_policy_executions(policy_execution_fingerprint)
);

CREATE TABLE m6_policy_evaluations (
    evaluation_identity CHAR(64) PRIMARY KEY
        CHECK (evaluation_identity ~ '^[0-9a-f]{64}$'),
    policy_id TEXT NOT NULL CHECK (length(policy_id) > 0),
    dataset_identity TEXT NOT NULL CHECK (length(dataset_identity) > 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    UNIQUE (policy_id, dataset_identity)
);
