"""Parameterized physical actor erasure for PostgreSQL module tables."""

from __future__ import annotations

import re
from collections.abc import Sequence

from course_insight.infrastructure.postgresql.pool import PostgresPool


_ACTOR = re.compile(r"^pseudonym_[a-z0-9][a-z0-9_-]{0,116}$")
_JSON_MATCH = (
    "jsonb_path_exists({column}, "
    "'$.** ? (@ == $actor)', "
    "jsonb_build_object('actor', to_jsonb(%s::text)))"
)


def purge_postgres_actor(
    pool: PostgresPool,
    *,
    module: str,
    actor_id: str,
) -> int:
    if not isinstance(actor_id, str) or not _ACTOR.fullmatch(actor_id):
        raise ValueError("actor erasure scope is invalid")
    statements = _statements(module)
    total = 0
    with pool.connection() as connection:
        with connection.transaction():
            for statement, parameter_count in statements:
                cursor = connection.execute(
                    statement,
                    tuple(actor_id for _ in range(parameter_count)),
                )
                total += max(0, cursor.rowcount)
    return total


def _json(column: str) -> str:
    return _JSON_MATCH.format(column=column)


def _statements(module: str) -> Sequence[tuple[str, int]]:
    if module == "m0":
        return (
            (f"DELETE FROM m0_learning_events WHERE {_json('payload')}", 1),
            ("DELETE FROM m0_assessment_runs WHERE learner_id = %s", 1),
        )
    if module == "m4":
        return ((f"DELETE FROM m4_task_plans WHERE {_json('payload')}", 1),)
    if module == "m5":
        return (
            (
                "DELETE FROM m5_learning_observation_audits WHERE observation_id IN "
                "(SELECT observation_id FROM m5_learning_observations WHERE learner_id = %s)",
                1,
            ),
            ("DELETE FROM m5_learning_observations WHERE learner_id = %s", 1),
            ("DELETE FROM m5_knowledge_traces WHERE learner_id = %s", 1),
            ("DELETE FROM m5_state_updates WHERE learner_id = %s", 1),
            ("DELETE FROM m5_learner_states WHERE learner_id = %s", 1),
            (f"DELETE FROM m5_class_states WHERE {_json('payload')}", 1),
            (f"DELETE FROM m5_dina_models WHERE {_json('payload')}", 1),
            (f"DELETE FROM m5_bkt_models WHERE {_json('payload')}", 1),
        )
    if module == "m6":
        decision_match = _json("result_payload")
        return (
            (f"DELETE FROM m6_policy_rewards WHERE {_json('payload')}", 1),
            (
                f"DELETE FROM m6_policy_observations WHERE {_json('payload')} "
                f"OR decision_id IN (SELECT decision_id FROM m6_tutoring_decisions WHERE {decision_match})",
                2,
            ),
            (f"DELETE FROM m6_policy_evaluations WHERE {_json('payload')}", 1),
            (f"DELETE FROM m6_policy_executions WHERE {_json('payload')}", 1),
            (
                f"DELETE FROM m6_tutoring_decisions WHERE {_json('evidence_identity')} "
                f"OR {_json('result_payload')}",
                2,
            ),
            (f"DELETE FROM m6_session_states WHERE {_json('payload')}", 1),
        )
    if module == "m7":
        return (
            ("DELETE FROM m7_student_feedback WHERE learner_id = %s", 1),
            (f"DELETE FROM m7_model_invocation_audits WHERE {_json('payload')}", 1),
        )
    if module == "m8":
        return (
            (
                "DELETE FROM m8_score_audits WHERE audit_id IN ("
                "SELECT DISTINCT value #>> '{}' FROM m8_scoring_results, "
                "jsonb_path_query(payload, '$.**.audit_id') AS value "
                "WHERE learner_id = %s)",
                1,
            ),
            (
                "DELETE FROM m8_frozen_assessment_records WHERE paper_id IN "
                "(SELECT paper_id FROM m8_assessment_papers WHERE learner_id = %s) "
                f"OR {_json('payload')}",
                2,
            ),
            ("DELETE FROM m8_scoring_results WHERE learner_id = %s", 1),
            ("DELETE FROM m8_assessment_papers WHERE learner_id = %s", 1),
            ("DELETE FROM m8_ability_estimates WHERE learner_id = %s", 1),
            ("DELETE FROM m8_adaptive_selections WHERE learner_id = %s", 1),
            (f"DELETE FROM m8_irt_calibration_runs WHERE {_json('payload')}", 1),
            (f"DELETE FROM m8_irt_parameter_sets WHERE {_json('payload')}", 1),
            (f"DELETE FROM m8_calibration_reviews WHERE {_json('payload')}", 1),
        )
    if module == "m9":
        analytics_match = (
            f"{_json('learner_ids')} OR {_json('payload')}"
        )
        return (
            (
                "DELETE FROM m9_model_invocation_audits WHERE source_report_id IN "
                f"(SELECT report_id FROM m9_teacher_analytics WHERE {analytics_match})",
                2,
            ),
            (f"DELETE FROM m9_teacher_reviews WHERE {_json('payload')}", 1),
            (
                f"DELETE FROM m9_teacher_analytics WHERE {analytics_match}",
                2,
            ),
        )
    raise ValueError("actor erasure module is invalid")


__all__ = ["purge_postgres_actor"]
