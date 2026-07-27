ALTER TABLE m4_intent_decisions
    DROP CONSTRAINT m4_intent_decisions_decision_status_check;

ALTER TABLE m4_intent_decisions
    ADD CONSTRAINT m4_intent_decisions_decision_status_check
    CHECK (
        decision_status IN (
            'accepted',
            'abstained',
            'out_of_scope',
            'unavailable',
            'failed',
            'invalid'
        )
    );
