CREATE UNIQUE INDEX m0_one_nonterminal_review_per_paper
ON m0_assessment_runs(paper_id)
WHERE operation = 'review' AND status <> 'completed';
