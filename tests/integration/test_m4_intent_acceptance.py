"""Stage-1 acceptance coverage for the private M4 intent adapter."""

from __future__ import annotations

import json
import shutil
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


pytestmark = pytest.mark.filterwarnings(
    "ignore:Setting the shape on a NumPy array has been deprecated:"
    "DeprecationWarning:joblib.numpy_pickle"
)


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
        scores = {
            "qa": 0.0,
            "diagnostic": 0.0,
            "practice": 0.0,
            "correction": 0.0,
            "stage_assessment": 0.0,
        }
        runner_up = "diagnostic" if self._label != "diagnostic" else "qa"
        return IntentPrediction.accepted(
            self._label,
            {
                **scores,
                self._label: self._confidence,
                runner_up: self._confidence - self._margin,
            },
            self.adapter_id,
            self.adapter_version,
        )


class _RaisingAdapter(_Adapter):
    def predict(self, text: str) -> IntentPrediction:
        del text
        raise RuntimeError("private adapter failure")


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


def _trained_artifact(tmp_path: Path, *, name: str = "artifact") -> Path:
    pytest.importorskip("joblib")
    pytest.importorskip("sklearn")
    from scripts.train_m4_intent import train_and_publish

    examples = {
        "correction": "correctum", "diagnostic": "diagnum",
        "out_of_scope": "outsideum", "practice": "praxium",
        "qa": "queryum", "stage_assessment": "stageum",
    }
    dataset = tmp_path / f"{name}.jsonl"
    dataset.write_text(
        "".join(
            json.dumps(
                {
                    "example_id": f"{split}-{label}",
                    "text": f"{token} {split}",
                    "label": label,
                    "locale": "en",
                    "paraphrase_group_id": f"{split}-{label}",
                    "source": "test_fixture",
                    "approved": True,
                    "notes": "",
                    "split": split,
                },
                ensure_ascii=False,
            )
            + "\n"
            for split in ("train", "validation", "test")
            for label, token in examples.items()
        ),
        encoding="utf-8",
    )
    artifact = tmp_path / name
    train_and_publish(
        input_path=dataset,
        output_dir=artifact,
        runtime_dir=tmp_path,
        model_id="m4-intent-acceptance",
        model_version="1.0.0",
        seed=17,
        min_confidence=0.0,
        min_margin=0.0,
    )
    return artifact


def _factory_with_intent(
    tmp_path: Path,
    *,
    mode: str,
    model_dir: Path,
    min_confidence: float = 0.70,
    min_margin: float = 0.10,
):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    if model_dir.resolve().is_relative_to(runtime_dir.resolve()):
        deployed_model_dir = model_dir.resolve()
    else:
        deployed_model_dir = runtime_dir / "models" / model_dir.name
        shutil.copytree(model_dir, deployed_model_dir)
    manifest = json.loads(
        (deployed_model_dir / "manifest.json").read_text(encoding="utf-8")
    )
    settings = _settings(tmp_path).model_copy(
        update={
            "intent": IntentSettings(
                mode=mode,  # type: ignore[arg-type]
                backend="sklearn",
                model_ref=deployed_model_dir.relative_to(runtime_dir),
                model_id=manifest["model_id"],
                model_version=manifest["model_version"],
                model_sha256=manifest["model_artifact_checksum"],
                min_confidence=min_confidence,
                min_margin=min_margin,
            )
        }
    )
    application = build_application(settings)
    application.m0_service.initialize()
    return application


def _set_artifact_version(artifact: Path, version: str) -> None:
    manifest_path = artifact / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["model_version"] = version
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
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
    public_contracts = public_contract_types()
    assert len(public_contracts) == 91
    assert {
        "BktConceptParameters",
        "BktModelArtifact",
        "ConceptResponse",
        "ConceptResponseSequence",
        "DinaItemParameters",
        "DinaModelArtifact",
        "ItemExposureSnapshot",
    } <= public_contracts.keys()
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


def test_stage1_public_factory_shadow_records_real_artifact_audit(
    tmp_path: Path,
) -> None:
    artifact = _trained_artifact(tmp_path)
    application = _factory_with_intent(
        tmp_path,
        mode="shadow",
        model_dir=artifact,
    )
    try:
        plan = _create_plan(
            application.m4_service,
            student_text="请解释这个概念",
        )
    finally:
        application.close()

    assert plan.task_type == "qa"
    with sqlite3.connect(tmp_path / "runtime" / "course_insight.sqlite3") as connection:
        source, shadow_json = connection.execute(
            "SELECT decision_source, shadow_json FROM m4_intent_decisions"
        ).fetchone()
    assert source == "legacy_rule"
    assert shadow_json is not None


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


def test_stage1_public_factory_active_enforces_thresholds(tmp_path: Path) -> None:
    artifact = _trained_artifact(tmp_path)
    accepted_app = _factory_with_intent(
        tmp_path / "accepted",
        mode="active",
        model_dir=artifact,
        min_confidence=0.0,
        min_margin=0.0,
    )
    try:
        accepted = _create_plan(
            accepted_app.m4_service,
            student_text="praxium train",
        )
    finally:
        accepted_app.close()
    assert accepted.task_type == "practice"

    rejected_root = tmp_path / "rejected"
    rejected_app = _factory_with_intent(
        rejected_root,
        mode="active",
        model_dir=artifact,
        min_confidence=1.0,
        min_margin=1.0,
    )
    try:
        with pytest.raises(DomainError) as captured:
            _create_plan(
                rejected_app.m4_service,
                student_text="diagnum train",
            )
    finally:
        rejected_app.close()
    assert captured.value.code == "UNSUPPORTED_TASK"
    with sqlite3.connect(rejected_root / "runtime" / "course_insight.sqlite3") as connection:
        decision = connection.execute(
            "SELECT decision_status, decision_source FROM m4_intent_decisions"
        ).fetchone()
        task_count = connection.execute(
            "SELECT COUNT(*) FROM m4_task_plans"
        ).fetchone()[0]
    assert decision == ("abstained", "refusal")
    assert task_count == 0


def test_stage1_public_factory_active_oos_refuses_and_replays_without_task_plan(
    tmp_path: Path,
) -> None:
    artifact = _trained_artifact(tmp_path)
    application = _factory_with_intent(
        tmp_path,
        mode="active",
        model_dir=artifact,
        min_confidence=0.0,
        min_margin=0.0,
    )
    try:
        for _ in range(2):
            with pytest.raises(DomainError) as captured:
                _create_plan(
                    application.m4_service,
                    student_text="outsideum train",
                )
            assert captured.value.code == "UNSUPPORTED_TASK"
            assert captured.value.recoverable is True
    finally:
        application.close()

    with sqlite3.connect(tmp_path / "runtime" / "course_insight.sqlite3") as connection:
        decision = connection.execute(
            "SELECT decision_status, decision_source FROM m4_intent_decisions"
        ).fetchone()
        decision_count = connection.execute(
            "SELECT COUNT(*) FROM m4_intent_decisions"
        ).fetchone()[0]
        task_count = connection.execute(
            "SELECT COUNT(*) FROM m4_task_plans"
        ).fetchone()[0]
    assert decision == ("out_of_scope", "refusal")
    assert decision_count == 1
    assert task_count == 0


def test_stage1_active_adapter_failure_is_persisted_and_replayed(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage1.sqlite3"
    service = _service(
        database_path,
        mode="active",
        adapter=_RaisingAdapter(
            label="qa",
            confidence=0.90,
            margin=0.30,
            version="failing-v1",
        ),
    )

    for _ in range(2):
        with pytest.raises(DomainError) as captured:
            _create_plan(service, student_text="greetings")
        assert captured.value.code == "UNSUPPORTED_TASK"
        assert captured.value.recoverable is True

    with sqlite3.connect(database_path) as connection:
        decision = connection.execute(
            """
            SELECT decision_status, decision_source, reason_codes_json
            FROM m4_intent_decisions
            """
        ).fetchone()
        decision_count = connection.execute(
            "SELECT COUNT(*) FROM m4_intent_decisions"
        ).fetchone()[0]
        task_count = connection.execute(
            "SELECT COUNT(*) FROM m4_task_plans"
        ).fetchone()[0]
    assert decision == (
        "failed",
        "refusal",
        '["adapter_exception","no_supported_intent"]',
    )
    assert decision_count == 1
    assert task_count == 0


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


def test_stage1_public_factory_restart_replays_first_real_artifact_version(
    tmp_path: Path,
) -> None:
    first_artifact = _trained_artifact(tmp_path, name="artifact-v1")
    _set_artifact_version(first_artifact, "stage1-v1")
    first_app = _factory_with_intent(
        tmp_path / "first",
        mode="active",
        model_dir=first_artifact,
        min_confidence=0.0,
        min_margin=0.0,
    )
    try:
        first = _create_plan(
            first_app.m4_service,
            student_text="praxium train",
        )
    finally:
        first_app.close()

    second_artifact = _trained_artifact(tmp_path, name="artifact-v2")
    _set_artifact_version(second_artifact, "stage1-v2")
    second_app = _factory_with_intent(
        tmp_path / "first",
        mode="active",
        model_dir=second_artifact,
        min_confidence=0.0,
        min_margin=0.0,
    )
    try:
        replay = _create_plan(
            second_app.m4_service,
            student_text="praxium train",
        )
    finally:
        second_app.close()

    assert replay == first
    with sqlite3.connect(
        tmp_path / "first" / "runtime" / "course_insight.sqlite3"
    ) as connection:
        source, version = connection.execute(
            "SELECT decision_source, adapter_version FROM m4_intent_decisions"
        ).fetchone()
    first_manifest = json.loads(
        (first_artifact / "manifest.json").read_text(encoding="utf-8")
    )
    assert source == "active_model"
    assert version == (
        "stage1-v1+sha256."
        + first_manifest["model_artifact_checksum"]
    )
