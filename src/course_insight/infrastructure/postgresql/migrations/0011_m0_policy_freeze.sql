ALTER TABLE m0_assessment_runs
    ADD COLUMN policy_id TEXT CHECK (
        policy_id IS NULL OR length(policy_id) > 0
    ),
    ADD COLUMN adapter_id TEXT CHECK (
        adapter_id IS NULL OR length(adapter_id) > 0
    ),
    ADD COLUMN adapter_version TEXT CHECK (
        adapter_version IS NULL OR length(adapter_version) > 0
    ),
    ADD COLUMN artifact_sha256 CHAR(64) CHECK (
        artifact_sha256 IS NULL
        OR artifact_sha256 ~ '^[0-9a-f]{64}$'
    ),
    ADD COLUMN feature_schema_version TEXT CHECK (
        feature_schema_version IS NULL
        OR length(feature_schema_version) > 0
    ),
    ADD COLUMN action_space_version TEXT CHECK (
        action_space_version IS NULL
        OR length(action_space_version) > 0
    ),
    ADD COLUMN gate_policy_version TEXT CHECK (
        gate_policy_version IS NULL
        OR length(gate_policy_version) > 0
    ),
    ADD CONSTRAINT m0_workflow_policy_identity_complete CHECK (
        (
            policy_id IS NULL
            AND adapter_id IS NULL
            AND adapter_version IS NULL
            AND artifact_sha256 IS NULL
            AND feature_schema_version IS NULL
            AND action_space_version IS NULL
            AND gate_policy_version IS NULL
        )
        OR (
            operation = 'submit'
            AND checkpoint IN (
                'policy_frozen',
                'tutoring_saved',
                'feedback_saved',
                'analytics_saved',
                'completed'
            )
            AND state_version IS NOT NULL
            AND previous_state_frozen IS TRUE
            AND policy_id IS NOT NULL
            AND adapter_id IS NOT NULL
            AND adapter_version IS NOT NULL
            AND feature_schema_version IS NOT NULL
            AND action_space_version IS NOT NULL
            AND gate_policy_version IS NOT NULL
        )
    );
