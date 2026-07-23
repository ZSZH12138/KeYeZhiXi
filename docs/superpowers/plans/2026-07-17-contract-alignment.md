# Contract Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair only the cross-module contract mismatches that would block M0/M4/M6/M8/M9 integration, without changing any module's business responsibility.

**Architecture:** Keep the existing 84 Pydantic contracts, ten services, and `AppCoordinator`. Make the existing path-free M0 submission contracts consumable by M8/M9, carry the authoritative course-package identity through `TaskPlan`, and remove non-existent service endpoints from the provenance graph.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, JSON Schema.

## Global Constraints

- Do not add an M10, internal HTTP boundary, ORM/SDK type, new contract class, or new business rule.
- Preserve file-path JSON inputs while also accepting the existing path-free M0 contracts.
- Do not rename the 84 public contract classes or change module ownership.
- Keep changes minimal; leave non-blocking semantic naming issues unchanged.
- The workspace is not a Git repository, so do not attempt commits or branch operations.

---

### Task 1: Connect M0 submission contracts to M8 and M9

**Files:**
- Modify: `src/course_insight/contracts/platform.py`
- Modify: `src/course_insight/application/coordinator.py`
- Modify: `src/course_insight/modules/m8_assessment_scoring/service.py`
- Modify: `src/course_insight/modules/m9_teacher_analytics/service.py`
- Test: `tests/integration/test_submission_contract_boundaries.py`

**Interfaces:**
- Consumes: existing `AssessmentSubmission`, `TeacherReviewSubmission`, `AssessmentPaper`, `KnowledgeBundle`, and `ScoringResultBundle`.
- Produces: unchanged `ScoringPreparationResult` and `TeacherReviewDecision`.

- [x] **Step 1: Write failing contract-boundary tests**

```python
def test_assessment_submission_accepts_scalar_objective_answers() -> None:
    submission = AssessmentSubmission(
        submission_id="submission_1",
        attempt_id="attempt_1",
        paper_id="paper_1",
        learner_id="pseudonym_1",
        answers={"item_1": False},
        submitted_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
    )
    assert submission.answers == {"item_1": False}

def test_teacher_submission_uses_domain_decision_vocabulary() -> None:
    submission = TeacherReviewSubmission(
        submission_id="decision_1",
        audit_id="audit_1",
        expected_audit_version=1,
        reviewer_id="pseudonym_teacher",
        decision="confirm",
        final_total_score=1.0,
        criterion_overrides=[],
        teacher_comment="confirmed",
        submitted_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
    )
    assert submission.decision == "confirm"
```

- [x] **Step 2: Run tests and confirm RED**

Run: `.conda/contract-audit/python.exe -m pytest tests/integration/test_submission_contract_boundaries.py -q`

Expected: failures because boolean answers and `confirm` are rejected and the services accept paths only.

- [x] **Step 3: Make the existing contracts symmetric**

```python
class AssessmentSubmission(ContractModel):
    answers: dict[str, str | bool | int | float]

class TeacherReviewSubmission(ContractModel):
    decision: Literal["confirm", "override", "reject"]
    final_total_score: float
    criterion_overrides: list[CriterionOverride]
    teacher_comment: str
```

Allow `M8.prepare_scoring` and `M9.record_teacher_review` to consume either their existing `Path` input or the matching existing M0 contract. Convert the M0 contract in memory to the already-established payload/decision shape; do not add another contract class.

- [x] **Step 4: Run focused tests and confirm GREEN**

Run: `.conda/contract-audit/python.exe -m pytest tests/integration/test_submission_contract_boundaries.py -q`

Expected: all tests pass.

### Task 2: Carry authoritative course-package identity through M4 to M6

**Files:**
- Modify: `src/course_insight/contracts/tasking.py`
- Modify: `src/course_insight/modules/m4_task_orchestration/service.py`
- Modify: `src/course_insight/modules/m6_tutoring_fsm/service.py`
- Test: `tests/contract/test_task_plan_alignment.py`

**Interfaces:**
- Consumes: `KnowledgeBundle.course_package_id` at M4.
- Produces: `TaskPlan.course_package_id` consumed unchanged by M6 when constructing `EvidenceQuery`.

- [x] **Step 1: Write failing task-plan tests**

```python
def test_task_plan_carries_course_package_id() -> None:
    bundle = KnowledgeBundle.model_construct(
        knowledge_bundle_id="bundle_without_naming_convention",
        course_package_id="authoritative_package_id",
        course_id="course_1",
        status="published",
        blueprints=[
            AssessmentBlueprint.model_construct(
                blueprint_id="blueprint_1",
                course_id="course_1",
                status="teacher_approved",
            )
        ],
    )
    plan = M4TaskOrchestrationServiceStub().create_task_plan(
        student_text="start assessment",
        task_type_hint="stage_assessment",
        course_id="course_1",
        class_id="class_1",
        learner_id="pseudonym_1",
        session_id="session_1",
        knowledge_bundle=bundle,
        learner_state_snapshot=None,
    )
    assert plan.course_package_id == bundle.course_package_id

def test_assessment_workflow_lists_every_participating_module() -> None:
    assert plan.workflow == ["M8", "M2", "M7", "M5", "M6", "M9"]
```

- [x] **Step 2: Run tests and confirm RED**

Run: `.conda/contract-audit/python.exe -m pytest tests/contract/test_task_plan_alignment.py -q`

Expected: `TaskPlan` has no `course_package_id` and M2 is absent from the assessment workflow.

- [x] **Step 3: Implement the minimal contract pass-through**

```python
class TaskPlan(ContractModel):
    course_package_id: str = Field(min_length=1)
```

Set it from `knowledge_bundle.course_package_id`, include it in the idempotency identity, change the distinct first-participation workflow to `M8, M2, M7, M5, M6, M9`, and have M6 copy `task_plan.course_package_id` instead of deriving it from `knowledge_bundle_id`.

- [x] **Step 4: Run focused tests and confirm GREEN**

Run: `.conda/contract-audit/python.exe -m pytest tests/contract/test_task_plan_alignment.py -q`

Expected: all tests pass.

### Task 3: Make provenance endpoints executable facts

**Files:**
- Modify: `contracts/contract_provenance.json`
- Test: `tests/contract/test_provenance_endpoints.py`

**Interfaces:**
- Consumes: existing public service methods and parameters.
- Produces: a provenance graph containing only real methods or explicitly external endpoints.

- [x] **Step 1: Write a failing reflection test**

```python
def test_internal_provenance_consumers_reference_public_methods() -> None:
    missing = internal_consumer_methods_missing_from_services(PROVENANCE_PATH)
    assert missing == []
```

- [x] **Step 2: Run the test and confirm RED**

Run: `.conda/contract-audit/python.exe -m pytest tests/contract/test_provenance_endpoints.py -q`

Expected: the five conceptual `consume_*` endpoints are reported as missing.

- [x] **Step 3: Remove only the fictitious whole-object edges**

Delete `M2.consume_scoring_preparation`, `M7.consume_scoring_preparation`, `M0.consume_scoring_result`, `M8.consume_state_update`, and `M4.consume_tutoring_control`. Preserve the real field-level edges such as `M2.retrieve`, `M7.score_subjective_answer`, and `M0.append_learning_events`.

- [x] **Step 4: Run the focused test and confirm GREEN**

Run: `.conda/contract-audit/python.exe -m pytest tests/contract/test_provenance_endpoints.py -q`

Expected: the graph loads and every internal consumer method exists.

### Task 4: Synchronize schemas and human-facing contract docs

**Files:**
- Modify: `README.md`
- Modify: `docs/interface_guide.md`
- Modify: `src/course_insight/modules/m0_platform/README.md`
- Modify: `src/course_insight/modules/m4_task_orchestration/README.md`
- Modify: `src/course_insight/modules/m6_tutoring_fsm/README.md`
- Modify: `src/course_insight/modules/m8_assessment_scoring/README.md`
- Modify: `src/course_insight/modules/m9_teacher_analytics/README.md`
- Regenerate: `contracts/schemas/AssessmentSubmission.schema.json`
- Regenerate: `contracts/schemas/TeacherReviewSubmission.schema.json`
- Regenerate: `contracts/schemas/TaskPlan.schema.json`

**Interfaces:**
- Consumes: the corrected Python contracts and provenance graph.
- Produces: matching generated JSON Schemas and documentation.

- [x] **Step 1: Update exact signatures, fields, flow descriptions, and output lists**

Document the dual Path/M0-contract inputs, `confirm` vocabulary, scalar answer values, `TaskPlan.course_package_id`, and the distinct first-participation meaning of `workflow`. Correct only nearby high-impact output omissions.

- [x] **Step 2: Regenerate all public schemas**

Run: `.conda/contract-audit/python.exe scripts/export_schemas.py`

Expected: 84 schema files remain, with only contracts affected by Python model changes differing.

- [x] **Step 3: Run complete verification**

Run: `.conda/contract-audit/python.exe -m pytest -q`

Run: `.conda/contract-audit/python.exe -m compileall -q src scripts tests`

Run: `.conda/contract-audit/python.exe scripts/export_schemas.py`

Expected: tests pass, compilation exits zero, and a second schema export is idempotent.
