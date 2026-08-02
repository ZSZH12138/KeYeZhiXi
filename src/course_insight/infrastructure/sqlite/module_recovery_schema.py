"""SQLite table definitions owned by the module-recovery migration."""

from __future__ import annotations


M5_LEARNER_STATES_V4_SQL = """
CREATE TABLE m5_learner_states_v4 (
    snapshot_id TEXT NOT NULL CHECK (length(snapshot_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    state_version INTEGER NOT NULL CHECK (state_version > 0),
    payload TEXT NOT NULL CHECK (
        CASE WHEN json_valid(payload)
            THEN json(payload) = payload
            ELSE 0
        END
    ),
    PRIMARY KEY (course_id, class_id, learner_id, state_version)
)
"""
M5_LEARNER_STATES_SQL = """
CREATE TABLE IF NOT EXISTS m5_learner_states (
    snapshot_id TEXT NOT NULL CHECK (length(snapshot_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    state_version INTEGER NOT NULL CHECK (state_version > 0),
    payload TEXT NOT NULL CHECK (
        CASE WHEN json_valid(payload)
            THEN json(payload) = payload
            ELSE 0
        END
    ),
    PRIMARY KEY (course_id, class_id, learner_id, state_version)
)
"""
M5_CLASS_STATES_V4_SQL = """
CREATE TABLE m5_class_states_v4 (
    snapshot_id TEXT NOT NULL CHECK (length(snapshot_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    state_version INTEGER NOT NULL CHECK (state_version > 0),
    aggregation_policy_version TEXT NOT NULL
        CHECK (length(aggregation_policy_version) > 0),
    payload TEXT NOT NULL CHECK (
        CASE WHEN json_valid(payload)
            THEN json(payload) = payload
            ELSE 0
        END
    ),
    PRIMARY KEY (course_id, class_id, snapshot_id),
    UNIQUE (course_id, class_id, state_version)
)
"""
M5_CLASS_STATES_SQL = """
CREATE TABLE IF NOT EXISTS m5_class_states (
    snapshot_id TEXT NOT NULL CHECK (length(snapshot_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    state_version INTEGER NOT NULL CHECK (state_version > 0),
    aggregation_policy_version TEXT NOT NULL
        CHECK (length(aggregation_policy_version) > 0),
    payload TEXT NOT NULL CHECK (
        CASE WHEN json_valid(payload)
            THEN json(payload) = payload
            ELSE 0
        END
    ),
    PRIMARY KEY (course_id, class_id, snapshot_id),
    UNIQUE (course_id, class_id, state_version)
)
"""
M5_STATE_UPDATES_SQL = """
CREATE TABLE IF NOT EXISTS m5_state_updates (
    attempt_id TEXT NOT NULL CHECK (length(attempt_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    state_version INTEGER NOT NULL CHECK (state_version > 0),
    payload TEXT NOT NULL CHECK (
        CASE WHEN json_valid(payload)
            THEN json(payload) = payload
            ELSE 0
        END
    ),
    PRIMARY KEY (attempt_id, state_version)
)
"""
M7_FEEDBACK_SQL = """
CREATE TABLE IF NOT EXISTS m7_student_feedback (
    feedback_id TEXT PRIMARY KEY CHECK (length(feedback_id) > 0),
    task_id TEXT NOT NULL CHECK (length(task_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    payload TEXT NOT NULL CHECK (
        CASE WHEN json_valid(payload)
            THEN json(payload) = payload
            ELSE 0
        END
    ),
    UNIQUE (task_id, learner_id)
)
"""
M8_PAPERS_SQL = """
CREATE TABLE IF NOT EXISTS m8_assessment_papers (
    paper_id TEXT PRIMARY KEY CHECK (length(paper_id) > 0),
    task_id TEXT NOT NULL UNIQUE CHECK (length(task_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    payload TEXT NOT NULL CHECK (
        CASE WHEN json_valid(payload)
            THEN json(payload) = payload
            ELSE 0
        END
    )
)
"""
M8_SCORING_RESULTS_SQL = """
CREATE TABLE IF NOT EXISTS m8_scoring_results (
    attempt_id TEXT NOT NULL CHECK (length(attempt_id) > 0),
    result_key TEXT NOT NULL CHECK (length(result_key) > 0),
    paper_id TEXT NOT NULL CHECK (length(paper_id) > 0),
    learner_id TEXT NOT NULL CHECK (length(learner_id) > 0),
    finalized_at TEXT NOT NULL CHECK (length(finalized_at) > 0),
    payload TEXT NOT NULL CHECK (
        CASE WHEN json_valid(payload)
            THEN json(payload) = payload
            ELSE 0
        END
    ),
    PRIMARY KEY (attempt_id, result_key)
)
"""
M9_ANALYTICS_SQL = """
CREATE TABLE IF NOT EXISTS m9_teacher_analytics (
    report_id TEXT PRIMARY KEY CHECK (length(report_id) > 0),
    course_id TEXT NOT NULL CHECK (length(course_id) > 0),
    class_id TEXT NOT NULL CHECK (length(class_id) > 0),
    generated_at TEXT NOT NULL CHECK (length(generated_at) > 0),
    learner_ids TEXT NOT NULL CHECK (
        CASE WHEN json_valid(learner_ids)
            THEN json(learner_ids) = learner_ids
            ELSE 0
        END
    ),
    payload TEXT NOT NULL CHECK (
        CASE WHEN json_valid(payload)
            THEN json(payload) = payload
            ELSE 0
        END
    )
)
"""
M9_MODEL_INVOCATION_AUDITS_SQL = """
CREATE TABLE IF NOT EXISTS m9_model_invocation_audits (
    invocation_id TEXT PRIMARY KEY CHECK (length(invocation_id) > 0),
    request_id TEXT NOT NULL UNIQUE CHECK (length(request_id) > 0),
    source_report_id TEXT NOT NULL CHECK (length(source_report_id) > 0),
    scope TEXT NOT NULL CHECK (scope = 'class_aggregate'),
    provider TEXT NOT NULL CHECK (provider = 'deepseek'),
    model_name TEXT NOT NULL CHECK (length(model_name) > 0),
    provider_status TEXT NOT NULL CHECK (
        provider_status IN ('not_run', 'succeeded', 'failed', 'blocked')
    ),
    validation_status TEXT NOT NULL CHECK (
        validation_status IN ('not_run', 'passed', 'blocked')
    ),
    created_at TEXT NOT NULL CHECK (length(created_at) > 0),
    payload TEXT NOT NULL CHECK (
        CASE WHEN json_valid(payload)
            THEN json(payload) = payload
            ELSE 0
        END
    ),
    payload_checksum TEXT NOT NULL CHECK (
        length(payload_checksum) = 64
        AND payload_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    FOREIGN KEY (source_report_id)
        REFERENCES m9_teacher_analytics(report_id)
        ON DELETE RESTRICT
)
"""

MODULE_RECOVERY_TABLES = (
    ("m5_state_updates", M5_STATE_UPDATES_SQL),
    ("m7_student_feedback", M7_FEEDBACK_SQL),
    ("m8_assessment_papers", M8_PAPERS_SQL),
    ("m8_scoring_results", M8_SCORING_RESULTS_SQL),
    ("m9_teacher_analytics", M9_ANALYTICS_SQL),
)
