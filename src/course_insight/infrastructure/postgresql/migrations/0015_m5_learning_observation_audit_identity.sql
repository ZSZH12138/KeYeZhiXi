CREATE TABLE m5_learning_observation_audits (
    source_audit_id TEXT NOT NULL CHECK (length(source_audit_id) > 0),
    source_audit_version INTEGER NOT NULL CHECK (source_audit_version > 0),
    observation_id TEXT NOT NULL UNIQUE CHECK (length(observation_id) > 0),
    PRIMARY KEY (source_audit_id, source_audit_version)
);

INSERT INTO m5_learning_observation_audits (
    source_audit_id,
    source_audit_version,
    observation_id
)
SELECT DISTINCT ON (
    payload ->> 'source_audit_id',
    CAST(payload ->> 'source_audit_version' AS INTEGER)
)
    payload ->> 'source_audit_id',
    CAST(payload ->> 'source_audit_version' AS INTEGER),
    observation_id
FROM m5_learning_observations
ORDER BY
    payload ->> 'source_audit_id',
    CAST(payload ->> 'source_audit_version' AS INTEGER),
    occurred_at,
    attempt_id,
    observation_id;
