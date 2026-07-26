from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from course_insight.contracts.errors import DomainError
from course_insight.contracts.knowledge import AssessmentBlueprint, KnowledgeBundle
from course_insight.contracts.state import LearnerStateSnapshot
from course_insight.contracts.tasking import TaskPlan
from course_insight.modules.m4_task_orchestration.service import (
    M4TaskOrchestrationService,
)
from course_insight.modules.m4_task_orchestration.stubs import (
    M4TaskOrchestrationServiceStub,
)


NOW = datetime(2026, 7, 21, tzinfo=timezone.utc)
ASSESSMENT_WORKFLOW = ["M8", "M2", "M7", "M5", "M6", "M9"]
QA_WORKFLOW = ["M2", "M7", "M6"]


def _canonical_key(identity: Mapping[str, str | None]) -> str:
    payload = json.dumps(
        dict(identity),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _MemoryRepository:
    def __init__(self) -> None:
        self._plans: dict[str, TaskPlan] = {}

    def insert_or_get_task_plan(
        self,
        plan: TaskPlan,
        idempotency_key: str,
    ) -> TaskPlan:
        existing = self._plans.get(idempotency_key)
        if existing is not None:
            return existing.model_copy(deep=True)
        stored = plan.model_copy(deep=True)
        self._plans = {**self._plans, idempotency_key: stored}
        return stored.model_copy(deep=True)

    @property
    def count(self) -> int:
        return len(self._plans)


class _RecordingKeyFactory:
    def __init__(self) -> None:
        self.identities: list[dict[str, str | None]] = []

    def __call__(self, identity: Mapping[str, str | None]) -> str:
        copied = dict(identity)
        self.identities.append(copied)
        return _canonical_key(copied)


def _blueprint(
    blueprint_id: str,
    *,
    course_id: str = "course_1",
    status: str = "teacher_approved",
) -> AssessmentBlueprint:
    return AssessmentBlueprint(
        blueprint_id=blueprint_id,
        version="1.0.0",
        course_id=course_id,
        sections=[],
        total_score=0.0,
        duration_minutes=30,
        status=status,
    )


def _bundle(
    *,
    knowledge_bundle_id: str = "bundle_1",
    course_package_id: str = "package_1",
    course_id: str = "course_1",
    status: str = "published",
    blueprints: list[AssessmentBlueprint] | None = None,
) -> KnowledgeBundle:
    return KnowledgeBundle(
        knowledge_bundle_id=knowledge_bundle_id,
        course_package_id=course_package_id,
        course_id=course_id,
        bundle_version="1.0.0",
        concepts=[],
        prerequisite_relations=[],
        misconception_tags=[],
        items=[],
        rubrics=[],
        blueprints=(
            [_blueprint("blueprint_stage")]
            if blueprints is None
            else blueprints
        ),
        q_matrix=[],
        status=status,
        published_at=NOW if status == "published" else None,
    )


def _state(
    *,
    course_id: str = "course_1",
    class_id: str = "class_1",
    learner_id: str = "pseudonym_learner",
) -> LearnerStateSnapshot:
    return LearnerStateSnapshot(
        snapshot_id="snapshot_1",
        course_id=course_id,
        class_id=class_id,
        learner_id=learner_id,
        state_version=1,
        concept_states=[],
        overall_mastery=0.0,
        evidence_count=0,
        updated_at=NOW,
    )


def _service(
    *,
    repository: _MemoryRepository | None = None,
    key_factory: Any = _canonical_key,
    blueprint_by_task_type: Mapping[str, str] | None = None,
) -> tuple[M4TaskOrchestrationService, _MemoryRepository]:
    selected_repository = repository or _MemoryRepository()
    kwargs: dict[str, Any] = {}
    if blueprint_by_task_type is not None:
        kwargs["blueprint_by_task_type"] = blueprint_by_task_type
    service = M4TaskOrchestrationService(
        selected_repository,
        key_factory,
        **kwargs,
    )
    return service, selected_repository


def _create(
    service: M4TaskOrchestrationService,
    *,
    student_text: str = "start stage assessment",
    task_type_hint: str | None = "stage_assessment",
    course_id: str = "course_1",
    class_id: str = "class_1",
    learner_id: str = "pseudonym_learner",
    session_id: str = "session_1",
    knowledge_bundle: KnowledgeBundle | None = None,
    learner_state_snapshot: LearnerStateSnapshot | None = None,
) -> TaskPlan:
    return service.create_task_plan(
        student_text=student_text,
        task_type_hint=task_type_hint,
        course_id=course_id,
        class_id=class_id,
        learner_id=learner_id,
        session_id=session_id,
        knowledge_bundle=knowledge_bundle or _bundle(),
        learner_state_snapshot=learner_state_snapshot,
    )


@pytest.mark.parametrize(
    "task_type",
    ["qa", "diagnostic", "practice", "correction", "stage_assessment"],
)
def test_valid_hint_selects_each_supported_task_type(task_type: str) -> None:
    service, _ = _service()

    plan = _create(
        service,
        student_text="text intentionally suggests another route",
        task_type_hint=task_type,
    )

    assert plan.task_type == task_type


def test_valid_hint_has_priority_over_text_keywords() -> None:
    service, _ = _service()

    plan = _create(
        service,
        student_text="start assessment practice and diagnostic",
        task_type_hint="correction",
    )

    assert plan.task_type == "correction"


@pytest.mark.parametrize(
    ("student_text", "expected"),
    [
        ("请解释这个概念？", "qa"),
        ("请开始一次诊断", "diagnostic"),
        ("Let me PRACTICE this topic", "practice"),
        ("请帮我订正错题", "correction"),
        ("Start a stage assessment", "stage_assessment"),
    ],
)
def test_text_rules_recognize_all_task_types(
    student_text: str,
    expected: str,
) -> None:
    service, _ = _service()

    plan = _create(
        service,
        student_text=student_text,
        task_type_hint=None,
    )

    assert plan.task_type == expected


def test_conflicting_keywords_are_rejected_instead_of_using_tuple_order() -> None:
    service, _ = _service()

    with pytest.raises(DomainError) as captured:
        _create(
            service,
            student_text="diagnostic practice assessment",
            task_type_hint=None,
        )

    assert captured.value.code == "UNSUPPORTED_TASK"


@pytest.mark.parametrize(
    ("student_text", "task_type_hint"),
    [("unknown learner intent", None), ("start assessment", "unsupported")],
)
def test_unrecognized_task_fails_safely(
    student_text: str,
    task_type_hint: str | None,
) -> None:
    service, _ = _service()

    with pytest.raises(DomainError) as captured:
        _create(
            service,
            student_text=student_text,
            task_type_hint=task_type_hint,
        )

    assert captured.value.code == "UNSUPPORTED_TASK"


def test_equivalent_text_normalization_reuses_same_business_task() -> None:
    service, repository = _service()

    first = _create(
        service,
        student_text="  START   STAGE ASSESSMENT  ",
        task_type_hint=None,
    )
    second = _create(
        service,
        student_text="start stage assessment",
        task_type_hint=None,
    )

    assert second == first
    assert repository.count == 1


def test_rejects_knowledge_bundle_course_mismatch() -> None:
    service, _ = _service()
    bundle = _bundle(
        course_id="course_2",
        blueprints=[_blueprint("blueprint_stage", course_id="course_2")],
    )

    with pytest.raises(DomainError) as captured:
        _create(service, knowledge_bundle=bundle)

    assert captured.value.code == "KNOWLEDGE_BUNDLE_MISMATCH"


def test_rejects_draft_knowledge_bundle() -> None:
    service, _ = _service()

    with pytest.raises(DomainError) as captured:
        _create(service, knowledge_bundle=_bundle(status="draft"))

    assert captured.value.code == "KNOWLEDGE_BUNDLE_MISMATCH"


@pytest.mark.parametrize("field_name", ["knowledge_bundle_id", "course_package_id"])
def test_rejects_blank_authoritative_bundle_identity(field_name: str) -> None:
    service, _ = _service()
    # Boundary robustness: construct the otherwise impossible invalid contract
    # without weakening production validation.
    bundle = _bundle().model_copy(update={field_name: ""}, deep=True)

    with pytest.raises(DomainError) as captured:
        _create(service, knowledge_bundle=bundle)

    assert captured.value.code == "KNOWLEDGE_BUNDLE_MISMATCH"


@pytest.mark.parametrize(
    "snapshot",
    [
        _state(course_id="course_2"),
        _state(class_id="class_2"),
        _state(learner_id="pseudonym_other"),
    ],
)
def test_rejects_learner_state_scope_mismatch(
    snapshot: LearnerStateSnapshot,
) -> None:
    service, _ = _service()

    with pytest.raises(DomainError) as captured:
        _create(service, learner_state_snapshot=snapshot)

    assert captured.value.code == "LEARNER_STATE_MISMATCH"


def test_qa_never_binds_an_assessment_blueprint() -> None:
    service, _ = _service(
        blueprint_by_task_type={"qa": "blueprint_stage"},
    )

    plan = _create(
        service,
        student_text="why does this rule apply?",
        task_type_hint="qa",
    )

    assert plan.blueprint_id is None
    assert plan.workflow == QA_WORKFLOW
    assert plan.next_module == "M2"


def test_explicit_mapping_selects_authoritative_blueprint_not_first_item() -> None:
    bundle = _bundle(
        blueprints=[_blueprint("blueprint_first"), _blueprint("blueprint_target")]
    )
    service, _ = _service(
        blueprint_by_task_type={"stage_assessment": "blueprint_target"},
    )

    plan = _create(service, knowledge_bundle=bundle)

    assert plan.blueprint_id == "blueprint_target"


def test_blueprint_mapping_is_independent_of_bundle_order() -> None:
    mapping = {"stage_assessment": "blueprint_target"}
    first_service, _ = _service(blueprint_by_task_type=mapping)
    second_service, _ = _service(blueprint_by_task_type=mapping)

    first = _create(
        first_service,
        knowledge_bundle=_bundle(
            blueprints=[
                _blueprint("blueprint_other"),
                _blueprint("blueprint_target"),
            ]
        ),
    )
    second = _create(
        second_service,
        knowledge_bundle=_bundle(
            blueprints=[
                _blueprint("blueprint_target"),
                _blueprint("blueprint_other"),
            ]
        ),
    )

    assert first.blueprint_id == second.blueprint_id == "blueprint_target"
    assert first.task_id == second.task_id


def test_mapping_is_copied_when_service_is_constructed() -> None:
    mapping = {"stage_assessment": "blueprint_target"}
    service, _ = _service(blueprint_by_task_type=mapping)
    mapping["stage_assessment"] = "blueprint_other"

    plan = _create(
        service,
        knowledge_bundle=_bundle(
            blueprints=[
                _blueprint("blueprint_other"),
                _blueprint("blueprint_target"),
            ]
        ),
    )

    assert plan.blueprint_id == "blueprint_target"


def test_multiple_approved_blueprints_require_explicit_mapping() -> None:
    service, _ = _service()
    bundle = _bundle(
        blueprints=[_blueprint("blueprint_a"), _blueprint("blueprint_b")]
    )

    with pytest.raises(DomainError) as captured:
        _create(service, knowledge_bundle=bundle)

    assert captured.value.code == "BLUEPRINT_NOT_FOUND"
    assert captured.value.details["reason"] == "selection_ambiguous"


@pytest.mark.parametrize(
    "blueprints",
    [
        [_blueprint("blueprint_other")],
        [_blueprint("blueprint_target", status="draft")],
    ],
)
def test_mapping_must_reference_an_approved_bundle_blueprint(
    blueprints: list[AssessmentBlueprint],
) -> None:
    service, _ = _service(
        blueprint_by_task_type={"stage_assessment": "blueprint_target"},
    )

    with pytest.raises(DomainError) as captured:
        _create(service, knowledge_bundle=_bundle(blueprints=blueprints))

    assert captured.value.code == "BLUEPRINT_NOT_FOUND"


def test_wrong_course_blueprint_cannot_be_selected() -> None:
    service, _ = _service(
        blueprint_by_task_type={"stage_assessment": "blueprint_target"},
    )
    bundle = _bundle().model_copy(
        update={
            "blueprints": [
                _blueprint("blueprint_target", course_id="course_2")
            ]
        },
        deep=True,
    )

    with pytest.raises(DomainError) as captured:
        _create(service, knowledge_bundle=bundle)

    assert captured.value.code == "BLUEPRINT_NOT_FOUND"


def test_no_approved_blueprint_fails_safely_without_mapping() -> None:
    service, _ = _service()

    with pytest.raises(DomainError) as captured:
        _create(
            service,
            knowledge_bundle=_bundle(
                blueprints=[_blueprint("blueprint_draft", status="draft")]
            ),
        )

    assert captured.value.code == "BLUEPRINT_NOT_FOUND"
    assert captured.value.details["reason"] == "no_approved_blueprint"


def test_assessment_workflow_and_next_module_are_frozen() -> None:
    service, _ = _service()

    plan = _create(service)

    assert plan.workflow == ASSESSMENT_WORKFLOW
    assert plan.next_module == "M8"
    assert len(plan.workflow) == len(set(plan.workflow))
    assert plan.next_module in plan.workflow


def test_idempotency_factory_receives_only_authoritative_business_fields() -> None:
    key_factory = _RecordingKeyFactory()
    service, _ = _service(key_factory=key_factory)

    plan = _create(service, student_text="arbitrary original wording")

    assert key_factory.identities == [
        {
            "course_id": "course_1",
            "class_id": "class_1",
            "learner_id": "pseudonym_learner",
            "session_id": "session_1",
            "task_type": "stage_assessment",
            "knowledge_bundle_id": "bundle_1",
            "course_package_id": "package_1",
            "blueprint_id": "blueprint_stage",
        }
    ]
    assert plan.task_id == f"task_{_canonical_key(key_factory.identities[0])}"


def test_repository_returns_first_authoritative_plan_on_replay() -> None:
    service, repository = _service()

    first = _create(service, student_text="start assessment")
    second = _create(service, student_text="please begin the assessment")

    assert second.task_id == first.task_id
    assert second.created_at == first.created_at
    assert second.content_checksum() == first.content_checksum()
    assert repository.count == 1


def test_service_rejects_idempotency_key_collision_between_identities() -> None:
    service, repository = _service(key_factory=lambda _: "a" * 64)
    _create(service, session_id="session_1")

    with pytest.raises(RuntimeError, match="idempotency collision"):
        _create(service, session_id="session_2")

    assert repository.count == 1


def test_fresh_zero_argument_stubs_are_deterministic() -> None:
    first = _create(M4TaskOrchestrationServiceStub())
    second = _create(M4TaskOrchestrationServiceStub())

    assert second == first
    assert first.created_at == datetime(
        2026,
        7,
        15,
        9,
        0,
        tzinfo=timezone(timedelta(hours=8)),
    )
