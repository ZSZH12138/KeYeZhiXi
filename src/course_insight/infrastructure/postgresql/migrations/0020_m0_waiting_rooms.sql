ALTER TABLE m0_assessment_runs
    DROP CONSTRAINT IF EXISTS m0_assessment_runs_status_check;

ALTER TABLE m0_assessment_runs
    ADD CONSTRAINT m0_assessment_runs_status_check
    CHECK (
        status IN (
            'pending',
            'running',
            'failed',
            'completed',
            'awaiting_review',
            'awaiting_rescore'
        )
    );
