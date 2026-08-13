ALTER TABLE m0_assessment_runs
    ADD COLUMN knowledge_bundle_id TEXT CHECK (
        knowledge_bundle_id IS NULL OR length(knowledge_bundle_id) > 0
    ),
    ADD COLUMN knowledge_bundle_version TEXT CHECK (
        knowledge_bundle_version IS NULL
        OR length(knowledge_bundle_version) > 0
    ),
    ADD COLUMN knowledge_bundle_checksum CHAR(64) CHECK (
        knowledge_bundle_checksum IS NULL
        OR knowledge_bundle_checksum ~ '^[0-9a-f]{64}$'
    ),
    ADD COLUMN course_package_id TEXT CHECK (
        course_package_id IS NULL OR length(course_package_id) > 0
    ),
    ADD COLUMN evidence_index_id TEXT CHECK (
        evidence_index_id IS NULL OR length(evidence_index_id) > 0
    ),
    ADD COLUMN evidence_index_version TEXT CHECK (
        evidence_index_version IS NULL OR length(evidence_index_version) > 0
    ),
    ADD COLUMN evidence_index_checksum CHAR(64) CHECK (
        evidence_index_checksum IS NULL
        OR evidence_index_checksum ~ '^[0-9a-f]{64}$'
    ),
    ADD COLUMN state_policy_checksum CHAR(64) CHECK (
        state_policy_checksum IS NULL
        OR state_policy_checksum ~ '^[0-9a-f]{64}$'
    ),
    ADD COLUMN teacher_policy_checksum CHAR(64) CHECK (
        teacher_policy_checksum IS NULL
        OR teacher_policy_checksum ~ '^[0-9a-f]{64}$'
    ),
    ADD COLUMN previous_state_frozen BOOLEAN,
    ADD COLUMN previous_learner_snapshot_id TEXT CHECK (
        previous_learner_snapshot_id IS NULL
        OR length(previous_learner_snapshot_id) > 0
    ),
    ADD COLUMN previous_learner_state_version INTEGER CHECK (
        previous_learner_state_version IS NULL
        OR previous_learner_state_version > 0
    ),
    ADD COLUMN previous_class_snapshot_id TEXT CHECK (
        previous_class_snapshot_id IS NULL
        OR length(previous_class_snapshot_id) > 0
    ),
    ADD COLUMN previous_class_state_version INTEGER CHECK (
        previous_class_state_version IS NULL
        OR previous_class_state_version > 0
    ),
    ADD CONSTRAINT m0_workflow_knowledge_identity_complete CHECK (
        (
            knowledge_bundle_id IS NULL
            AND knowledge_bundle_version IS NULL
            AND knowledge_bundle_checksum IS NULL
            AND course_package_id IS NULL
        )
        OR (
            knowledge_bundle_id IS NOT NULL
            AND knowledge_bundle_version IS NOT NULL
            AND knowledge_bundle_checksum IS NOT NULL
            AND course_package_id IS NOT NULL
        )
    ),
    ADD CONSTRAINT m0_workflow_evidence_identity_complete CHECK (
        (
            evidence_index_id IS NULL
            AND evidence_index_version IS NULL
            AND evidence_index_checksum IS NULL
        )
        OR (
            evidence_index_id IS NOT NULL
            AND evidence_index_version IS NOT NULL
            AND evidence_index_checksum IS NOT NULL
        )
    ),
    ADD CONSTRAINT m0_workflow_learner_baseline_complete CHECK (
        (previous_learner_snapshot_id IS NULL)
        = (previous_learner_state_version IS NULL)
    ),
    ADD CONSTRAINT m0_workflow_class_baseline_valid CHECK (
        previous_class_snapshot_id IS NOT NULL
        OR previous_class_state_version IS NULL
    ),
    ADD CONSTRAINT m0_workflow_baseline_frozen CHECK (
        COALESCE(previous_state_frozen, FALSE)
        OR (
            previous_learner_snapshot_id IS NULL
            AND previous_learner_state_version IS NULL
            AND previous_class_snapshot_id IS NULL
            AND previous_class_state_version IS NULL
        )
    );
