CREATE TABLE m6_session_states (
    session_id TEXT NOT NULL CHECK (length(session_id) > 0),
    turn_count INTEGER NOT NULL CHECK (turn_count >= 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0),
    PRIMARY KEY (session_id, turn_count)
);

CREATE TABLE m6_tutoring_decisions (
    decision_id TEXT PRIMARY KEY CHECK (length(decision_id) > 0),
    session_id TEXT NOT NULL CHECK (length(session_id) > 0),
    turn_count INTEGER NOT NULL CHECK (turn_count >= 0),
    previous_turn_count INTEGER CHECK (
        previous_turn_count IS NULL OR previous_turn_count >= 0
    ),
    request_fingerprint CHAR(64) NOT NULL UNIQUE
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    input_fingerprint CHAR(64) NOT NULL UNIQUE
        CHECK (input_fingerprint ~ '^[0-9a-f]{64}$'),
    evidence_fingerprint CHAR(64) NOT NULL
        CHECK (evidence_fingerprint ~ '^[0-9a-f]{64}$'),
    evidence_identity JSONB NOT NULL
        CHECK (jsonb_typeof(evidence_identity) = 'object'),
    result_payload JSONB NOT NULL
        CHECK (jsonb_typeof(result_payload) = 'object'),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    schema_version TEXT NOT NULL CHECK (length(schema_version) > 0),
    UNIQUE (session_id, turn_count),
    FOREIGN KEY (session_id, turn_count)
        REFERENCES m6_session_states(session_id, turn_count)
);
