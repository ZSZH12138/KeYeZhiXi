ALTER TABLE m0_assessment_runs
    ADD COLUMN class_roster_size INTEGER
        CHECK (class_roster_size IS NULL OR class_roster_size > 0),
    ADD COLUMN class_roster_checksum TEXT
        CHECK (
            class_roster_checksum IS NULL
            OR class_roster_checksum ~ '^[0-9a-f]{64}$'
        ),
    ADD COLUMN class_roster_captured_at TIMESTAMPTZ;

ALTER TABLE m0_assessment_runs
    ADD CONSTRAINT m0_assessment_runs_class_roster_identity_check
    CHECK (
        (
            class_roster_size IS NULL
            AND class_roster_checksum IS NULL
            AND class_roster_captured_at IS NULL
        )
        OR (
            class_roster_size IS NOT NULL
            AND class_roster_checksum IS NOT NULL
            AND class_roster_captured_at IS NOT NULL
        )
    );
