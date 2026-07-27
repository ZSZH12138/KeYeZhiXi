CREATE TABLE m4_intent_decisions (
    request_key TEXT PRIMARY KEY CHECK (length(btrim(request_key)) > 0),
    resolved_task_type TEXT,
    decision_status TEXT NOT NULL,
    decision_source TEXT NOT NULL CHECK (length(btrim(decision_source)) > 0),
    adapter_id TEXT NOT NULL CHECK (length(btrim(adapter_id)) > 0),
    adapter_version TEXT NOT NULL CHECK (length(btrim(adapter_version)) > 0),
    policy_version TEXT NOT NULL CHECK (length(btrim(policy_version)) > 0),
    confidence DOUBLE PRECISION CHECK (
        confidence IS NULL
        OR (confidence >= 0.0 AND confidence <= 1.0)
    ),
    margin DOUBLE PRECISION CHECK (
        margin IS NULL
        OR (margin >= 0.0 AND margin <= 1.0)
    ),
    input_checksum CHAR(64) NOT NULL
        CHECK (input_checksum ~ '^[0-9a-f]{64}$'),
    reason_codes_json JSONB NOT NULL
        CHECK (jsonb_typeof(reason_codes_json) = 'array'),
    shadow_json JSONB
        CHECK (
            shadow_json IS NULL
            OR jsonb_typeof(shadow_json) = 'object'
        ),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    payload_checksum CHAR(64) NOT NULL
        CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL,
    CHECK (
        decision_status IN ('accepted', 'abstained', 'out_of_scope', 'invalid')
    ),
    CHECK (
        resolved_task_type IS NULL
        OR resolved_task_type IN (
            'qa',
            'diagnostic',
            'practice',
            'correction',
            'stage_assessment'
        )
    ),
    CHECK (
        (decision_status = 'accepted' AND resolved_task_type IS NOT NULL)
        OR (decision_status <> 'accepted' AND resolved_task_type IS NULL)
    ),
    CHECK (
        (
            decision_status = 'accepted'
            AND decision_source IN (
                'legal_hint',
                'high_precision_rule',
                'legacy_rule',
                'active_model'
            )
        )
        OR (decision_status <> 'accepted' AND decision_source = 'refusal')
    ),
    CHECK (
        decision_source <> 'active_model'
        OR (confidence IS NOT NULL AND margin IS NOT NULL)
    )
);
