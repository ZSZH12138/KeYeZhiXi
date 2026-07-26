"""Stage-1 acceptance coverage for the private M4 intent adapter."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from course_insight.application.factory import build_application
from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import AssessmentBlueprint, KnowledgeBundle
from course_insight.contracts.tasking import TaskPlan
from course_insight.infrastructure.config import (
    DatabaseSettings,
    IntentSettings,
    LoggingSettings,
    PlatformSettings,
)
from course_insight.infrastructure.sqlite.m4_repository import SQLiteM4Repository
from course_insight.modules.m4_task_orchestration.identity import (
    canonical_idempotency_key,
)
from course_insight.modules.m4_task_orchestration.intent import (
    IntentPrediction,
)
from course_insight.modules.m4_task_orchestration.intent_policy import IntentPolicy
from course_insight.modules.m4_task_orchestration.intent_service import (
    M4IntentService,
)
from course_insight.modules.m4_task_orchestration.service import (
    M4TaskOrchestrationService,
)
from scripts.export_schemas import public_contract_types


NOW = datetime(2026, 7, 26, tzinfo=timezone.utc)
_QA_WORKFLOW = ["M2", "M7", "M6"]
_ASSESSMENT_WORKFLOW = ["M8", "M2", "M7", "M5", "M6", "M9"]
_TASK_PLAN_FIELDS = (
    "schema_version",
    "task_id",
    "task_type",
    "course_id",
    "class_id",
    "learner_id",
    "session_id",
    "blueprint_id",
    "knowledge_bundle_id",
    "course_package_id",
    "workflow",
    "next_module",
    "created_at",
)


class _Adapter:
    def __init__(
        self,
        *,
        label: str,
        confidence: float,
        margin: float,
        version: str,
    ) -> None:
        self._label = label
        self._confidence = confidence
        self._margin = margin
        self._version = version

    @property
    def adapter_id(self) -> str:
        return "stage1-adapter"

    @property
    def adapter_version(self) -> str:
        return self._version

    def predict(self, text: str) -> IntentPrediction:
        del text
        return IntentPrediction.accepted(
            self._label,
            self._confidence,
            self._margin,
            self.adapter_id,
            self.adapter_version,
        )


def _bundle() -> KnowledgeBundle:
    blueprint = AssessmentBlueprint(
        blueprint_id="blueprint_stage1",
        version="1.0.0",
        course_id="course_1",
        sections=[],
        total_score=0.0,
        duration_minutes=30,
        status="teacher_approved",
    )
    return KnowledgeBundle(
        knowledge_bundle_id="bundle_1",
        course_package_id="package_1",
        course_id="course_1",
        bundle_version="1.0.0",
        concepts=[],
        prerequisite_relations=[],
        misconception_tags=[],
        items=[],
        rubrics=[],
        blueprints=[blueprint],
        q_matrix=[],
        status="published",
        published_at=NOW,
    )


def _settings(tmp_path: Path) -> PlatformSettings:
    runtime_dir = tmp_path / "runtime"
    return PlatformSettings(
        environment="test",
        runtime_dir=runtime_dir,
        config_dir=tmp_path / "config",
        database=DatabaseSettings(
            backend="sqlite",
            sqlite_path=runtime_dir / "course_insight.sqlite3",
        ),
        logging=LoggingSettings(directory=runtime_dir / "logs"),
        intent=IntentSettings(),
    )


def _service(
    database_path: Path,
    *,
    mode: str = "rules",
    adapter: _Adapter | None = None,
    min_confidence: float = 0.70,
    min_margin: float = 0.10,
) -> M4TaskOrchestrationService:
    repository = SQLiteM4Repository(database_path)
    repository.initialize()
    intent = M4IntentService(
        repository,
        canonical_idempotency_key,
        mode=mode,  # type: ignore[arg-type]
        adapter=adapter,
        policy=IntentPolicy(min_confidence, min_margin),
    )
    return M4TaskOrchestrationService(
        repository,
        canonical_idempotency_key,
        intent_service=intent,
    )


def _create_plan(
    service: M4TaskOrchestrationService,
    *,
    student_text: str,
    task_type_hint: str | None = None,
    session_id: str = "session_1",
) -> TaskPlan:
    return service.create_task_plan(
        student_text=student_text,
        task_type_hint=task_type_hint,
        course_id="course_1",
        class_id="class_1",
        learner_id="learner_1",
        session_id=session_id,
        knowledge_bundle=_bundle(),
        learner_state_snapshot=None,
    )


@pytest.mark.parametrize(
    ("text", "expected_type", "expected_workflow"),
    [
        ("请解释这个概念", "qa", _QA_WORKFLOW),
        ("请进行诊断", "diagnostic", _ASSESSMENT_WORKFLOW),
        ("请安排练习", "practice", _ASSESSMENT_WORKFLOW),
        ("请订正错题", "correction", _ASSESSMENT_WORKFLOW),
        ("请进行阶段测评", "stage_assessment", _ASSESSMENT_WORKFLOW),
    ],
)
def test_stage1_public_factory_keeps_all_five_task_workflows(
    tmp_path: Path,
    text: str,
    expected_type: str,
    expected_workflow: list[str],
) -> None:
    application = build_application(_settings(tmp_path))
    try:
        application.m0_service.initialize()
        plan = _create_plan(application.m4_service, student_text=text)
    finally:
        application.close()

    assert plan.task_type == expected_type
    assert plan.workflow == expected_workflow
    assert plan.next_module == expected_workflow[0]


def test_stage1_public_schema_and_task_plan_fields_are_unchanged() -> None:
    assert len(public_contract_types()) == 84
    assert tuple(TaskPlan.model_fields) == _TASK_PLAN_FIELDS


def test_stage1_private_decisions_distinguish_requests_while_task_plan_uses_its_business_identity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage1.sqlite3"
    service = _service(database_path)

    first = _create_plan(service, student_text="请解释概念甲")
    same_task_type = _create_plan(
        service,
        student_text="STAGE1-PRIVATE-TEXT-ONLY",
        task_type_hint="qa",
    )
    different_task_type = _create_plan(
        service,
        student_text="STAGE1-PRIVATE-PRACTICE",
        task_type_hint="practice",
    )

    assert same_task_type.task_id == first.task_id
    assert different_task_type.task_id != first.task_id
    with sqlite3.connect(database_path) as connection:
        decisions = connection.execute(
            "SELECT request_key, input_checksum FROM m4_intent_decisions"
        ).fetchall()
        database_dump = "\n".join(connection.iterdump())
    assert len(decisions) == 3
    assert len({row[0] for row in decisions}) == 3
    assert "STAGE1-PRIVATE-TEXT-ONLY" not in database_dump
    assert "STAGE1-PRIVATE-PRACTICE" not in database_dump


def test_stage1_refusal_and_rule_conflict_are_recoverable(tmp_path: Path) -> None:
    service = _service(tmp_path / "stage1.sqlite3")

    for text in ("查询天气", "请诊断并练习"):
        with pytest.raises(DomainError) as captured:
            _create_plan(service, student_text=text)
        assert captured.value.code == "UNSUPPORTED_TASK"
        assert captured.value.recoverable is True


def test_stage1_shadow_audits_without_changing_the_task_plan(tmp_path: Path) -> None:
    rules_plan = _create_plan(
        _service(tmp_path / "rules.sqlite3"),
        student_text="请解释这个概念",
    )
    shadow_database = tmp_path / "shadow.sqlite3"
    shadow_plan = _create_plan(
        _service(
            shadow_database,
            mode="shadow",
            adapter=_Adapter(
                label="practice",
                confidence=0.99,
                margin=0.80,
                version="shadow-v1",
            ),
        ),
        student_text="请解释这个概念",
    )

    assert shadow_plan.model_dump(exclude={"created_at"}) == rules_plan.model_dump(
        exclude={"created_at"}
    )
    with sqlite3.connect(shadow_database) as connection:
        source, shadow_json = connection.execute(
            "SELECT decision_source, shadow_json FROM m4_intent_decisions"
        ).fetchone()
    assert source == "legacy_rule"
    assert '"label":"practice"' in shadow_json


def test_stage1_active_accepts_only_predictions_passing_both_thresholds(
    tmp_path: Path,
) -> None:
    low_confidence = _service(
        tmp_path / "low.sqlite3",
        mode="active",
        adapter=_Adapter(
            label="practice",
            confidence=0.69,
            margin=0.50,
            version="active-v1",
        ),
    )
    with pytest.raises(DomainError) as captured:
        _create_plan(low_confidence, student_text="模型低置信输入")
    assert captured.value.code == "UNSUPPORTED_TASK"

    high_confidence = _service(
        tmp_path / "high.sqlite3",
        mode="active",
        adapter=_Adapter(
            label="practice",
            confidence=0.90,
            margin=0.30,
            version="active-v1",
        ),
    )
    accepted = _create_plan(high_confidence, student_text="模型高置信输入")

    assert accepted.task_type == "practice"
    assert accepted.workflow == _ASSESSMENT_WORKFLOW


def test_stage1_exact_replay_survives_restart_and_adapter_version_change(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage1.sqlite3"
    first = _create_plan(
        _service(
            database_path,
            mode="active",
            adapter=_Adapter(
                label="practice",
                confidence=0.90,
                margin=0.30,
                version="active-v1",
            ),
        ),
        student_text="模型版本重放输入",
    )
    replay = _create_plan(
        _service(
            database_path,
            mode="active",
            adapter=_Adapter(
                label="qa",
                confidence=0.99,
                margin=0.90,
                version="active-v2",
            ),
        ),
        student_text="模型版本重放输入",
    )

    assert replay == first
    with sqlite3.connect(database_path) as connection:
        adapter_version = connection.execute(
            "SELECT adapter_version FROM m4_intent_decisions"
        ).fetchone()[0]
    assert adapter_version == "active-v1"
