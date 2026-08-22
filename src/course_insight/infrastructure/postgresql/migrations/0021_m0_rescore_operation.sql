ALTER TABLE m0_assessment_runs
    DROP CONSTRAINT IF EXISTS m0_assessment_runs_operation_check;

ALTER TABLE m0_assessment_runs
    ADD CONSTRAINT m0_assessment_runs_operation_check
    CHECK (
        operation IN (
            'start',
            'submit',
            'review',
            'rescore'
        )
    );

CREATE UNIQUE INDEX IF NOT EXISTS m0_one_nonterminal_rescore_per_paper
ON m0_assessment_runs(paper_id)
WHERE operation = 'rescore' AND status <> 'completed';
