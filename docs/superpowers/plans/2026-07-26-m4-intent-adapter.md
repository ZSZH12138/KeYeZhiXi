# M4 Intent Model and Replaceable Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a private, versioned, auditable M4 intent-decision pipeline with deterministic refusal, an optional replaceable scikit-learn adapter, SQLite/PostgreSQL persistence, and offline training/evaluation without changing any public contract or workflow.

**Architecture:** `M4TaskOrchestrationService.create_task_plan(...)` keeps its public signature and resolves intent in this order: exact persisted decision, legal hint, unambiguous high-precision rule, accepted active-model prediction, conflict-aware legacy fallback, then recoverable `UNSUPPORTED_TASK`. The private intent request key is the approved conflict fix: the six context fields from the specification plus normalized `task_type_hint` plus SHA-256 of normalized text, while TaskPlan identity remains the existing eight fields. Optional model code is lazy-loaded behind an immutable adapter protocol; rules mode remains the default and imports without the optional dependencies.

> **Final-review amendment (implemented 2026-07-27):** The original task
> sketches below record the initial TDD checkpoints. The final implementation
> additionally enforces the uploaded specification's shared
> `m4-text-normalization-v1`, exact five-task immutable score vectors,
> governed `fallback_to_rules`/`fail_closed` behavior, runtime-relative
> `model_ref` plus external model ID/version/SHA-256 pins, the eight-field
> governed JSONL row format, five-task macro-F1 with separate OOS/selective
> metrics, and durable SQLite-to-PostgreSQL checkpoints. Current callable
> arguments and operator commands are documented in
> `docs/m4_intent_operations.md` and `docs/postgresql_migration.md`; earlier
> `model_dir` and four-field dataset snippets in this historical plan are not
> the final runtime interface.

**Tech Stack:** Python 3.12, frozen dataclasses, Pydantic v2 settings, SQLite, PostgreSQL/psycopg 3, scikit-learn character TF-IDF and logistic regression, joblib, JSONL, pytest/pytest-cov.

## Global Constraints

- Preserve the 84 public Pydantic schemas, `TaskPlan`, all public service signatures, module workflows, M0 operation identity, and the eight-field TaskPlan idempotency identity.
- Keep all new intent types private to M4; do not subclass `ContractModel` or add public contract exports.
- Do not use an LLM, DeepSeek, network inference, embeddings, or raw text in persistent storage or logs.
- Use immutable value objects and defensive copies; reject malformed predictions, artifacts, rows, and checksums fail-closed.
- Default to `mode="rules"` with no optional import or model artifact load.
- Support only `qa`, `diagnostic`, `practice`, `correction`, and `stage_assessment`; `out_of_scope` is a training/evaluation label and can never become a `TaskPlan.task_type`.
- `shadow` records model output but never lets it change the rule/hint result; `active` accepts a model result only when both confidence and top-two margin satisfy configured thresholds.
- Persist exact decisions idempotently and atomically. A repeat of the same approved private request identity must reuse the first decision and must not run a different adapter version.
- SQLite schema version and PostgreSQL migration version advance from 9 to 10 with equivalent constraints and import parity.
- The optional `intent` dependency group contains scikit-learn and joblib. Base plus `dev` installation must continue to import and run rules mode without them.
- Training uses UTF-8 JSONL, required `group_id` separation across train/validation/test, deterministic seeds, atomic artifact publication, manifest schema versioning, and SHA-256 verification.
- Every implementation task follows RED → GREEN → REFACTOR, runs focused tests, receives specification and quality review, and ends in a conventional local commit.
- Final acceptance requires full tests, at least 80% coverage, schema-count regression, SQLite/PostgreSQL parity checks, Django checks, secret scan, and a clean reviewed diff. Live PostgreSQL may be skipped only when the repository's existing live-test guard proves no test database is configured.

---

## File Responsibility Map

- `src/course_insight/modules/m4_task_orchestration/intent.py`: immutable private intent value objects, adapter protocol, statuses, and validation.
- `src/course_insight/modules/m4_task_orchestration/intent_identity.py`: normalization, input checksum, and approved private request-key construction.
- `src/course_insight/modules/m4_task_orchestration/intent_policy.py`: thresholds and accepted/abstained/OOS/invalid decision policy.
- `src/course_insight/modules/m4_task_orchestration/intent_rules.py`: high-precision and legacy rule match collection with conflict detection.
- `src/course_insight/modules/m4_task_orchestration/intent_service.py`: persisted/hint/rule/model/fallback/refusal orchestration and shadow observation handling.
- `src/course_insight/modules/m4_task_orchestration/sklearn_adapter.py`: lazy trusted-artifact loading and `IntentAdapter` implementation.
- `src/course_insight/modules/m4_task_orchestration/intent_dataset.py`: strict JSONL row parsing, group split, metrics, and artifact-manifest helpers.
- `src/course_insight/modules/m4_task_orchestration/repository.py`: private decision persistence protocol added alongside the unchanged TaskPlan operations.
- `src/course_insight/modules/m4_task_orchestration/service.py`: delegates only intent selection to the private service, then preserves existing blueprint/workflow/TaskPlan behavior.
- `src/course_insight/infrastructure/sqlite/m4_repository.py` and `src/course_insight/infrastructure/postgresql/m4_repository.py`: equivalent atomic decision insert/get and checksum validation.
- `src/course_insight/infrastructure/config/models.py` and `src/course_insight/application/factory.py`: validated intent mode/backend/artifact configuration and composition.
- `scripts/train_m4_intent.py`: deterministic offline training/evaluation CLI.
- `data/m4_intent/example.jsonl`: neutral, synthetic, repository-safe format example.

---

### Task 1: Private Intent Primitives, Identity, Policy, and Conflict-Aware Rules

**Files:**

- Create: `src/course_insight/modules/m4_task_orchestration/intent.py`
- Create: `src/course_insight/modules/m4_task_orchestration/intent_identity.py`
- Create: `src/course_insight/modules/m4_task_orchestration/intent_policy.py`
- Create: `src/course_insight/modules/m4_task_orchestration/intent_rules.py`
- Modify: `src/course_insight/modules/m4_task_orchestration/routing.py`
- Create: `tests/unit/test_m4_intent_primitives.py`
- Modify: `tests/unit/test_m4_task_orchestration.py`

**Interfaces:**

- Produces: `IntentPrediction(label: str | None, confidence: float, margin: float, status: IntentStatus, adapter_id: str, adapter_version: str, reason_codes: tuple[str, ...])`.
- Produces: `IntentAdapter.predict(text: str) -> IntentPrediction`.
- Produces: `IntentPolicy(min_confidence: float, min_margin: float).accept(prediction) -> IntentDecisionOutcome`.
- Produces: `build_intent_request_identity(..., task_type_hint: str | None, student_text: str) -> Mapping[str, str]` and `input_checksum(student_text: str) -> str`.
- Produces: `match_high_precision_rules(text: str) -> RuleMatch` and `match_legacy_rules(text: str) -> RuleMatch`, where `RuleMatch.labels` is a sorted immutable tuple and `RuleMatch.resolved_label` is non-null only for exactly one family.
- Consumes: the five values in `SUPPORTED_TASK_TYPES`; no contract types are created.

- [ ] **Step 1: Write failing immutable-value, validation, identity, and rule-conflict tests**

```python
def test_private_request_identity_separates_hint_and_normalized_text() -> None:
    base = dict(
        course_id="c1",
        class_id="cl1",
        learner_id="l1",
        session_id="s1",
        knowledge_bundle_id="kb1",
        course_package_id="cp1",
    )
    practice = build_intent_request_identity(
        **base, task_type_hint=" Practice ", student_text="  再做一题  "
    )
    diagnostic = build_intent_request_identity(
        **base, task_type_hint="diagnostic", student_text="再做一题"
    )
    changed_text = build_intent_request_identity(
        **base, task_type_hint="practice", student_text="换一道题"
    )
    assert practice["task_type_hint"] == "practice"
    assert practice["input_checksum"] == input_checksum("再做一题")
    assert practice != diagnostic
    assert practice != changed_text
    assert "再做一题" not in repr(practice)


def test_conflicting_rule_families_do_not_use_first_match_priority() -> None:
    match = match_high_precision_rules("请先给我诊断，再安排练习")
    assert match.labels == ("diagnostic", "practice")
    assert match.resolved_label is None


@pytest.mark.parametrize(
    ("prediction", "status"),
    [
        (IntentPrediction.accepted("qa", 0.89, 0.21, "fixture", "1"), "accepted"),
        (IntentPrediction.accepted("qa", 0.69, 0.21, "fixture", "1"), "abstained"),
        (IntentPrediction.accepted("qa", 0.89, 0.09, "fixture", "1"), "abstained"),
        (IntentPrediction.out_of_scope(0.92, 0.30, "fixture", "1"), "out_of_scope"),
    ],
)
def test_policy_requires_confidence_and_margin(prediction, status) -> None:
    assert IntentPolicy(0.70, 0.10).accept(prediction).status == status
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pytest tests/unit/test_m4_intent_primitives.py tests/unit/test_m4_task_orchestration.py -q
```

Expected: collection fails because the four private intent modules and their symbols do not exist.

- [ ] **Step 3: Implement frozen primitives, canonical identity, threshold policy, and match collection**

```python
class IntentStatus(StrEnum):
    ACCEPTED = "accepted"
    ABSTAINED = "abstained"
    OUT_OF_SCOPE = "out_of_scope"
    INVALID = "invalid"


@runtime_checkable
class IntentAdapter(Protocol):
    @property
    def adapter_id(self) -> str: ...

    @property
    def adapter_version(self) -> str: ...

    def predict(self, text: str) -> IntentPrediction: ...


def build_intent_request_identity(
    *,
    course_id: str,
    class_id: str,
    learner_id: str,
    session_id: str,
    knowledge_bundle_id: str,
    course_package_id: str,
    task_type_hint: str | None,
    student_text: str,
) -> Mapping[str, str]:
    return MappingProxyType({
        "course_id": course_id,
        "class_id": class_id,
        "learner_id": learner_id,
        "session_id": session_id,
        "knowledge_bundle_id": knowledge_bundle_id,
        "course_package_id": course_package_id,
        "task_type_hint": normalize_hint(task_type_hint),
        "input_checksum": input_checksum(student_text),
    })
```

Keep `resolve_blueprint_id(...)`, `workflow_for(...)`, their constants, and their behavior in `routing.py`. Make `resolve_task_type(...)` a compatibility wrapper around the rules-only resolver so existing direct callers preserve hint-first behavior while conflicting families now reject instead of silently using tuple order.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command.

Expected: all selected tests pass, including the updated conflict expectation and all existing blueprint/workflow tests.

- [ ] **Step 5: Refactor and commit**

Run:

```powershell
git diff --check
git add src/course_insight/modules/m4_task_orchestration tests/unit/test_m4_intent_primitives.py tests/unit/test_m4_task_orchestration.py
git commit -m "feat: add private M4 intent primitives"
```

Expected: commit succeeds with no public contract file change.

---

### Task 2: Persisted-First Decision Pipeline and Service Integration

**Files:**

- Create: `src/course_insight/modules/m4_task_orchestration/intent_service.py`
- Modify: `src/course_insight/modules/m4_task_orchestration/repository.py`
- Modify: `src/course_insight/modules/m4_task_orchestration/service.py`
- Modify: `src/course_insight/modules/m4_task_orchestration/stubs.py`
- Create: `tests/unit/test_m4_intent_service.py`
- Modify: `tests/unit/test_m4_task_orchestration.py`

**Interfaces:**

- Produces: frozen `StoredIntentDecision` with final decision fields, optional shadow observation fields, canonical payload checksum, `created_at`, and `schema_version=1`.
- Produces repository methods `get_intent_decision(request_key: str) -> StoredIntentDecision | None` and `insert_or_get_intent_decision(decision: StoredIntentDecision) -> StoredIntentDecision`.
- Produces `M4IntentService.resolve(*, student_text, task_type_hint, six context fields) -> str`.
- Modifies `M4TaskOrchestrationService.__init__` only by adding keyword-only optional `intent_service: M4IntentService | None = None`; `create_task_plan(...)` remains byte-for-byte signature compatible.
- Consumes Task 1 `IntentAdapter`, policy, identities, and rule matchers.

- [ ] **Step 1: Write failing precedence, mode, replay, shadow, malformed-adapter, and privacy tests**

```python
def test_persisted_decision_wins_before_new_hint_or_adapter_execution() -> None:
    repository = InMemoryM4Repository()
    adapter = RecordingAdapter(IntentPrediction.accepted(
        "diagnostic", 0.99, 0.50, "fixture", "2"
    ))
    service = make_intent_service(repository, adapter=adapter, mode="active")
    first = service.resolve(**request(student_text="请安排练习", task_type_hint=None))
    second = service.resolve(**request(student_text="  请安排练习  ", task_type_hint=None))
    assert first == second == "practice"
    assert adapter.calls == ()


def test_same_context_different_hint_or_text_has_distinct_private_decision() -> None:
    service = make_intent_service(InMemoryM4Repository(), mode="rules")
    assert service.resolve(**request("练习", "practice")) == "practice"
    assert service.resolve(**request("诊断", "diagnostic")) == "diagnostic"
    assert len(service.repository.intent_decisions) == 2


def test_shadow_prediction_is_audited_but_cannot_override_rule() -> None:
    adapter = RecordingAdapter(IntentPrediction.accepted(
        "diagnostic", 0.99, 0.50, "fixture", "7"
    ))
    service = make_intent_service(
        InMemoryM4Repository(), adapter=adapter, mode="shadow"
    )
    assert service.resolve(**request("请给我练习", None)) == "practice"
    stored = only_decision(service.repository)
    assert stored.resolved_task_type == "practice"
    assert stored.decision_source == "high_precision_rule"
    assert stored.shadow_label == "diagnostic"
    assert stored.shadow_agrees is False


def test_active_low_confidence_and_conflicting_fallback_refuse() -> None:
    adapter = RecordingAdapter(IntentPrediction.accepted(
        "qa", 0.51, 0.02, "fixture", "1"
    ))
    with pytest.raises(DomainError) as captured:
        make_intent_service(
            InMemoryM4Repository(), adapter=adapter, mode="active"
        ).resolve(**request("我要练习，也要诊断", None))
    assert captured.value.code == "UNSUPPORTED_TASK"
    assert captured.value.recoverable is True
```

Also assert: blank text refuses before adapter execution; invalid hints refuse; rules mode never calls an injected adapter; adapter exceptions become auditable abstention/refusal without leaking raw text; invalid labels, NaN/Infinity, out-of-range probabilities, blank versions, and label/status mismatches are rejected; OOS never creates a plan; exact replay returns the first adapter version.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pytest tests/unit/test_m4_intent_service.py tests/unit/test_m4_task_orchestration.py -q
```

Expected: collection fails on missing `M4IntentService` and repository decision methods.

- [ ] **Step 3: Implement persisted-first orchestration and integrate without changing the public call surface**

```python
def resolve(self, request: IntentRequest) -> str:
    normalized_text = normalize_text(request.student_text)
    if not normalized_text:
        raise_unsupported("student task text must not be blank")
    request_key = self._request_key_factory(
        build_intent_request_identity(..., student_text=normalized_text)
    )
    persisted = self._repository.get_intent_decision(request_key)
    if persisted is not None:
        return self._validate_persisted(persisted, request_key)
    outcome = self._decide(request, normalized_text)
    winner = self._repository.insert_or_get_intent_decision(
        StoredIntentDecision.from_outcome(
            request_key=request_key,
            input_checksum=input_checksum(normalized_text),
            outcome=outcome,
        )
    )
    return self._validate_persisted(winner, request_key)
```

Decision order inside `_decide`: legal hint; a single high-precision rule family; `shadow` audit with rule/hint final; accepted `active` model; single legacy family; recoverable refusal. On refusal, persist an abstained/OOS/invalid decision row before raising `UNSUPPORTED_TASK`, so the same exact request remains reproducible. Persist only normalized-label metadata, checksums, reason codes, versions, scores, and timestamps.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command.

Expected: all new decision-pipeline tests and all pre-existing M4 service tests pass.

- [ ] **Step 5: Prove TaskPlan and M0 identities remain unchanged**

Run:

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pytest tests/integration/test_m4_cross_module.py tests/integration/test_m4_persistence.py -q
```

Expected: same logical request replays; same session with different hint/task still produces the pre-existing distinct TaskPlans; M0 uses the same `start:{task_id}` operation identity.

- [ ] **Step 6: Refactor and commit**

Run:

```powershell
git diff --check
git add src/course_insight/modules/m4_task_orchestration tests/unit/test_m4_intent_service.py tests/unit/test_m4_task_orchestration.py
git commit -m "feat: orchestrate auditable M4 intent decisions"
```

Expected: commit succeeds and `src/course_insight/contracts/` remains unchanged.

---

### Task 3: SQLite Schema v10 and Atomic Decision Persistence

**Files:**

- Modify: `src/course_insight/infrastructure/sqlite/migrations.py`
- Modify: `src/course_insight/infrastructure/sqlite/m4_repository.py`
- Modify: `tests/integration/test_m4_persistence.py`
- Modify: `tests/unit/test_application_factory.py`

**Interfaces:**

- Consumes Task 2 repository methods and `StoredIntentDecision`.
- Produces SQLite table `m4_intent_decisions` keyed by `request_key`, with final and shadow metadata, `input_checksum`, `payload_checksum`, `schema_version`, and `created_at`.
- Preserves current `m4_task_plans` schema, indexes, and operations.

- [ ] **Step 1: Write failing migration, round-trip, replay, corruption, privacy, and concurrency tests**

```python
def test_sqlite_v10_intent_decision_replay_is_atomic(tmp_path: Path) -> None:
    repository = SQLiteM4Repository(open_database(tmp_path / "m4.sqlite3"))
    decisions = run_concurrently(
        12,
        lambda: repository.insert_or_get_intent_decision(
            decision(request_key="same-key", adapter_version="v1")
        ),
    )
    assert {item.payload_checksum for item in decisions} == {
        decisions[0].payload_checksum
    }
    row = repository.connection.execute(
        "SELECT COUNT(*), MAX(input_checksum) FROM m4_intent_decisions"
    ).fetchone()
    assert row[0] == 1
    assert "原始文本" not in dump_database(repository.connection)


def test_sqlite_rejects_tampered_intent_payload_checksum(tmp_path: Path) -> None:
    repository = SQLiteM4Repository(open_database(tmp_path / "m4.sqlite3"))
    repository.insert_or_get_intent_decision(decision(request_key="key"))
    repository.connection.execute(
        "UPDATE m4_intent_decisions SET resolved_task_type = 'qa'"
    )
    with pytest.raises(RuntimeError, match="corrupt"):
        repository.get_intent_decision("key")
```

Also test clean v9→v10 migration, repeated migration, all CHECK constraints, first-writer replay with different adapter versions, refusal-row replay, and factory-backed SQLite service restart.

- [ ] **Step 2: Run SQLite tests and verify RED**

Run:

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pytest tests/integration/test_m4_persistence.py tests/unit/test_application_factory.py -q
```

Expected: failures report schema version 9 and missing `m4_intent_decisions`.

- [ ] **Step 3: Add forward-only v10 migration and parameterized repository mapping**

```sql
CREATE TABLE IF NOT EXISTS m4_intent_decisions (
    request_key TEXT PRIMARY KEY,
    resolved_task_type TEXT NULL,
    decision_status TEXT NOT NULL,
    decision_source TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    confidence REAL NULL,
    margin REAL NULL,
    input_checksum TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    shadow_json TEXT NULL,
    schema_version INTEGER NOT NULL,
    payload_checksum TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (decision_status IN ('accepted','abstained','out_of_scope','invalid')),
    CHECK (resolved_task_type IS NULL OR resolved_task_type IN (
        'qa','diagnostic','practice','correction','stage_assessment'
    )),
    CHECK (length(input_checksum) = 64),
    CHECK (length(payload_checksum) = 64)
);
```

Use `BEGIN IMMEDIATE` and `INSERT ... ON CONFLICT DO NOTHING`, then read and validate the authoritative row. Never interpolate values into SQL.

- [ ] **Step 4: Run SQLite tests and verify GREEN**

Run the Step 2 command.

Expected: selected tests pass and fresh databases report schema version 10.

- [ ] **Step 5: Refactor and commit**

Run:

```powershell
git diff --check
git add src/course_insight/infrastructure/sqlite tests/integration/test_m4_persistence.py tests/unit/test_application_factory.py
git commit -m "feat: persist M4 intent decisions in SQLite"
```

Expected: commit succeeds with no raw-text column.

---

### Task 4: PostgreSQL v10, Repository Parity, and SQLite Import

**Files:**

- Create: `src/course_insight/infrastructure/postgresql/migrations/0010_m4_intent_decisions.sql`
- Modify: `src/course_insight/infrastructure/postgresql/migration_runner.py`
- Modify: `src/course_insight/infrastructure/postgresql/m4_repository.py`
- Modify: `src/course_insight/infrastructure/postgresql/sqlite_import_source.py`
- Modify: `src/course_insight/infrastructure/postgresql/sqlite_import_destination.py`
- Modify: `src/course_insight/infrastructure/postgresql/sqlite_import.py`
- Modify: `tests/unit/test_postgresql_m4_repository_parity.py`
- Modify: `tests/integration/test_postgresql_m4_m6_repository_parity.py`
- Modify: `tests/unit/test_sqlite_to_postgres_import.py`
- Modify: `tests/integration/test_sqlite_to_postgres_migration.py`

**Interfaces:**

- Consumes Task 2 `StoredIntentDecision` and Task 3 SQLite row shape.
- Produces PostgreSQL schema version 10 and atomic `INSERT ... ON CONFLICT DO NOTHING RETURNING`.
- Extends importer allowlists, source batch iteration, destination upsert, count/checksum verification, resume ledger, and parity report with `m4_intent_decisions`.

- [ ] **Step 1: Write failing fake-repository, migration-SQL, import, and live-parity tests**

```python
def test_postgres_intent_insert_uses_conflict_safe_parameterized_sql() -> None:
    repository, connection = repository_with_fake_connection()
    authoritative = repository.insert_or_get_intent_decision(decision())
    statement, parameters = connection.executions[0]
    assert "ON CONFLICT (request_key) DO NOTHING" in statement
    assert "%s" in statement
    assert "学生原文" not in statement
    assert authoritative.request_key == decision().request_key


def test_import_manifest_includes_m4_intent_decisions() -> None:
    assert "m4_intent_decisions" in import_table_order()
    assert source_table_columns("m4_intent_decisions") == (
        "request_key",
        "resolved_task_type",
        "decision_status",
        "decision_source",
        "adapter_id",
        "adapter_version",
        "policy_version",
        "confidence",
        "margin",
        "input_checksum",
        "reason_codes_json",
        "shadow_json",
        "schema_version",
        "payload_checksum",
        "created_at",
    )
```

Also test check constraints in SQL text, v10 migration checksums, first-writer mismatch replay, tamper rejection, empty/partial/resumed imports, count/checksum parity, and real PostgreSQL round-trip through the existing guarded fixture.

- [ ] **Step 2: Run PostgreSQL and importer tests and verify RED**

Run:

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pytest tests/unit/test_postgresql_m4_repository_parity.py tests/unit/test_sqlite_to_postgres_import.py tests/integration/test_sqlite_to_postgres_migration.py tests/integration/test_postgresql_m4_m6_repository_parity.py -q
```

Expected: failures identify missing migration 0010, repository methods, and importer table metadata; live tests may be skipped by the existing explicit guard.

- [ ] **Step 3: Implement schema and parity-safe persistence/import**

PostgreSQL 0010 must use `DOUBLE PRECISION`, `TIMESTAMPTZ`, equivalent label/status/checksum checks, and a primary key on `request_key`. The repository maps JSON through canonical serializers, validates checksums after reads, and returns the first committed row. Import preserves the stored `created_at` and checksums and verifies source/destination aggregates using the established importer checksum algorithm.

- [ ] **Step 4: Run PostgreSQL and importer tests and verify GREEN**

Run the Step 2 command.

Expected: all non-live tests pass; live result is recorded exactly as pass or guarded skip.

- [ ] **Step 5: Refactor and commit**

Run:

```powershell
git diff --check
git add src/course_insight/infrastructure/postgresql tests/unit/test_postgresql_m4_repository_parity.py tests/unit/test_sqlite_to_postgres_import.py tests/integration/test_sqlite_to_postgres_migration.py tests/integration/test_postgresql_m4_m6_repository_parity.py
git commit -m "feat: add PostgreSQL M4 intent parity"
```

Expected: commit succeeds and SQLite/PostgreSQL column mappings are equal.

---

### Task 5: Trusted scikit-learn Artifact Adapter

**Files:**

- Modify: `pyproject.toml`
- Create: `src/course_insight/modules/m4_task_orchestration/sklearn_adapter.py`
- Create: `tests/unit/test_m4_sklearn_adapter.py`
- Create: `tests/unit/test_m4_optional_dependencies.py`

**Interfaces:**

- Produces `load_sklearn_intent_adapter(model_dir: Path) -> IntentAdapter`.
- Model directory contains `model.joblib` and `manifest.json`; manifest contains `schema_version`, `adapter_id`, `adapter_version`, `model_sha256`, `labels`, `vectorizer`, `classifier`, and training provenance.
- Consumes Task 1 prediction validation; outputs only immutable `IntentPrediction`.

- [ ] **Step 1: Add optional dependencies and install them only in the isolated environment**

Add:

```toml
intent = [
    "scikit-learn>=1.5,<2",
    "joblib>=1.4,<2",
]
```

Run:

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pip install -e '.[dev,intent]'
```

Expected: installation succeeds inside `course_insight_m4_intent_20260726`; no existing Conda environment is modified.

- [ ] **Step 2: Write failing lazy-import, checksum, manifest, score, OOS, and optional-missing tests**

```python
def test_rules_import_does_not_import_optional_packages() -> None:
    code = (
        "import sys; "
        "import course_insight.modules.m4_task_orchestration.service; "
        "assert 'sklearn' not in sys.modules; "
        "assert 'joblib' not in sys.modules"
    )
    subprocess.run([sys.executable, "-I", "-c", code], check=True)


def test_adapter_rejects_model_checksum_mismatch(tmp_path: Path) -> None:
    write_fixture_artifact(tmp_path)
    (tmp_path / "model.joblib").write_bytes(b"tampered")
    with pytest.raises(IntentArtifactError, match="checksum"):
        load_sklearn_intent_adapter(tmp_path)


def test_adapter_maps_out_of_scope_to_non_task_prediction(artifact_dir) -> None:
    adapter = load_sklearn_intent_adapter(artifact_dir)
    prediction = adapter.predict("帮我预订明天的机票")
    assert prediction.status == IntentStatus.OUT_OF_SCOPE
    assert prediction.label is None
```

Also reject path traversal/symlink escape, missing files, unsupported manifest schema, unknown labels, duplicate/missing expected labels, non-finite probabilities, non-normalized class arrays, blank versions, and classifier/vectorizer shape mismatch.

- [ ] **Step 3: Run adapter tests and verify RED**

Run:

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pytest tests/unit/test_m4_sklearn_adapter.py tests/unit/test_m4_optional_dependencies.py -q
```

Expected: collection fails because the loader module does not exist.

- [ ] **Step 4: Implement lazy trusted-artifact loading and prediction**

```python
def load_sklearn_intent_adapter(model_dir: Path) -> IntentAdapter:
    root = model_dir.resolve(strict=True)
    manifest_path = _contained_regular_file(root, "manifest.json")
    model_path = _contained_regular_file(root, "model.joblib")
    manifest = _parse_manifest(manifest_path)
    if sha256(model_path.read_bytes()).hexdigest() != manifest.model_sha256:
        raise IntentArtifactError("model checksum mismatch")
    import joblib
    artifact = joblib.load(model_path)
    return SklearnIntentAdapter(artifact=artifact, manifest=manifest)
```

Treat joblib as trusted local runtime input but still require a configured directory, containment checks, regular files, bounded manifest size, exact checksum, supported schema, and expected classes before deserialization. Compute confidence and top-two margin from `predict_proba`; map top `out_of_scope` to `label=None`.

- [ ] **Step 5: Run adapter tests and base-install import probe**

Run the Step 3 command, then:

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pytest tests/unit/test_m4_optional_dependencies.py -q
```

Expected: all pass and subprocess probes show no eager sklearn/joblib import.

- [ ] **Step 6: Refactor and commit**

Run:

```powershell
git diff --check
git add pyproject.toml src/course_insight/modules/m4_task_orchestration/sklearn_adapter.py tests/unit/test_m4_sklearn_adapter.py tests/unit/test_m4_optional_dependencies.py
git commit -m "feat: add replaceable sklearn intent adapter"
```

Expected: commit succeeds without adding binary model artifacts.

---

### Task 6: Intent Settings and Composition Root

**Files:**

- Modify: `src/course_insight/infrastructure/config/models.py`
- Modify: `src/course_insight/infrastructure/config/__init__.py`
- Modify: `src/course_insight/application/factory.py`
- Modify: `tests/unit/test_m0_config_loader.py`
- Modify: `tests/unit/test_application_factory.py`

**Interfaces:**

- Produces frozen `IntentSettings(mode: Literal["rules","shadow","active"], backend: Literal["none","sklearn"], model_dir: Path | None, min_confidence: float, min_margin: float, policy_version: str)`.
- Adds `PlatformSettings.intent` with a rules-only default.
- Factory creates no adapter in rules mode and lazy-loads the configured adapter in shadow/active mode.
- Keeps `build_application(...)`, `ApplicationContainer`, `AppCoordinator`, and service call signatures unchanged.

- [ ] **Step 1: Write failing default, invalid-combination, environment, and factory tests**

```python
def test_intent_defaults_are_rules_only() -> None:
    settings = load_test_settings()
    assert settings.intent.mode == "rules"
    assert settings.intent.backend == "none"
    assert settings.intent.model_dir is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": "active", "backend": "none"},
        {"mode": "shadow", "backend": "sklearn", "model_dir": None},
        {"mode": "rules", "backend": "sklearn", "model_dir": "model"},
        {"min_confidence": float("nan")},
        {"min_margin": 1.01},
    ],
)
def test_invalid_intent_settings_fail_closed(overrides) -> None:
    with pytest.raises((ValidationError, ConfigurationError)):
        IntentSettings(**overrides)


def test_rules_factory_never_loads_model(monkeypatch) -> None:
    monkeypatch.setattr(
        sklearn_adapter,
        "load_sklearn_intent_adapter",
        lambda path: pytest.fail("rules mode loaded a model"),
    )
    with build_application(settings=rules_settings()) as app:
        assert app.m4_service is not None
```

Also test nested environment overrides, relative model directory resolution against `config_dir`, unreadable artifact startup failure for active/shadow, thresholds at 0 and 1, and injected repository/service overrides.

- [ ] **Step 2: Run config/factory tests and verify RED**

Run:

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pytest tests/unit/test_m0_config_loader.py tests/unit/test_application_factory.py -q
```

Expected: failures identify missing `IntentSettings` and `PlatformSettings.intent`.

- [ ] **Step 3: Implement validated settings and private service composition**

```python
class IntentSettings(_FrozenModel):
    mode: Literal["rules", "shadow", "active"] = "rules"
    backend: Literal["none", "sklearn"] = "none"
    model_dir: Path | None = None
    min_confidence: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.70
    min_margin: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.10
    policy_version: str = Field(default="m4-intent-policy-v1", min_length=1)
```

Require `rules/none/no model_dir` together and require `shadow|active/sklearn/model_dir` together. In the factory, create `M4IntentService` with the same M4 repository and `canonical_idempotency_key`; pass it to the existing M4 service through the new optional constructor keyword.

- [ ] **Step 4: Run config/factory and cross-module tests and verify GREEN**

Run:

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pytest tests/unit/test_m0_config_loader.py tests/unit/test_application_factory.py tests/integration/test_m4_cross_module.py -q
```

Expected: all pass in default rules mode without an artifact.

- [ ] **Step 5: Refactor and commit**

Run:

```powershell
git diff --check
git add src/course_insight/infrastructure/config src/course_insight/application/factory.py tests/unit/test_m0_config_loader.py tests/unit/test_application_factory.py
git commit -m "feat: configure M4 intent runtime modes"
```

Expected: commit succeeds and default application startup remains backward compatible.

---

### Task 7: Deterministic JSONL Training, Evaluation, and Atomic Artifacts

**Files:**

- Create: `src/course_insight/modules/m4_task_orchestration/intent_dataset.py`
- Create: `scripts/train_m4_intent.py`
- Create: `data/m4_intent/example.jsonl`
- Create: `tests/unit/test_m4_intent_dataset.py`
- Create: `tests/integration/test_m4_intent_training.py`

**Interfaces:**

- Produces strict `IntentExample(text, label, group_id, split)` parsing for six labels including `out_of_scope`.
- Produces deterministic group-disjoint split and reports `macro_f1`, per-class precision/recall/F1/support, OOS recall, coverage, selective accuracy, thresholds, seed, counts, and group-overlap checks.
- CLI accepts `--input`, `--output-dir`, `--seed`, `--min-confidence`, and `--min-margin`; publishes `model.joblib`, `manifest.json`, and `metrics.json` atomically.
- Artifact is directly loadable through Task 5.

- [ ] **Step 1: Write failing parser, split, determinism, metrics, and end-to-end CLI tests**

```python
def test_jsonl_parser_rejects_unknown_fields_and_blank_values(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(
        '{"text":"练习","label":"practice","group_id":"","extra":1}\n',
        encoding="utf-8",
    )
    with pytest.raises(IntentDatasetError):
        load_examples(path)


def test_group_split_has_no_cross_partition_leakage(examples) -> None:
    partitions = split_by_group(examples, seed=20260726)
    assert set(partitions.train_groups).isdisjoint(partitions.validation_groups)
    assert set(partitions.train_groups).isdisjoint(partitions.test_groups)
    assert set(partitions.validation_groups).isdisjoint(partitions.test_groups)


def test_training_cli_is_reproducible_and_loadable(tmp_path: Path) -> None:
    first = run_training(tmp_path / "a", seed=17)
    second = run_training(tmp_path / "b", seed=17)
    assert comparable_metrics(first / "metrics.json") == comparable_metrics(
        second / "metrics.json"
    )
    assert load_sklearn_intent_adapter(first).predict("请给我练习").status in {
        IntentStatus.ACCEPTED,
        IntentStatus.ABSTAINED,
    }
```

Also test duplicate rows, invalid UTF-8, malformed JSON, oversized rows, empty files, missing labels, one-group-only data, explicit split leakage, output directory already present, interrupted temporary output cleanup, manifest checksum, OOS metric, and threshold-dependent coverage/selective accuracy.

- [ ] **Step 2: Run dataset/training tests and verify RED**

Run:

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pytest tests/unit/test_m4_intent_dataset.py tests/integration/test_m4_intent_training.py -q
```

Expected: collection fails on missing dataset module and training script.

- [ ] **Step 3: Implement strict loading and deterministic group partitioning**

```python
def split_by_group(
    examples: Sequence[IntentExample],
    *,
    seed: int,
) -> DatasetPartitions:
    groups = sorted({example.group_id for example in examples})
    shuffled = list(groups)
    random.Random(seed).shuffle(shuffled)
    train_ids, validation_ids, test_ids = _balanced_group_slices(shuffled)
    return DatasetPartitions.from_group_ids(
        examples, train_ids, validation_ids, test_ids
    )
```

If every row has an explicit split, validate group disjointness and use it; otherwise require no row to specify a split and generate all splits. Require every model label in training and every evaluation partition to contain enough truth labels for defined metrics.

- [ ] **Step 4: Implement character TF-IDF, logistic regression, evaluation, and atomic publication**

```python
pipeline = Pipeline([
    ("tfidf", TfidfVectorizer(
        analyzer="char",
        ngram_range=(2, 5),
        lowercase=True,
        sublinear_tf=True,
    )),
    ("classifier", LogisticRegression(
        max_iter=2000,
        class_weight="balanced",
        random_state=args.seed,
    )),
])
```

Fit on train, select/report thresholds against validation without silently changing CLI thresholds, evaluate once on test, serialize to a sibling temporary directory, fsync files, calculate `model.joblib` SHA-256, write canonical JSON manifest/metrics, then atomically rename only when the target does not exist.

- [ ] **Step 5: Add a neutral synthetic format example**

Add 18 UTF-8 JSONL rows: one train, validation, and test group for each of `qa`, `diagnostic`, `practice`, `correction`, `stage_assessment`, and `out_of_scope`. Each line contains exactly `text`, `label`, `group_id`, and `split`; text is generic and contains no real learner or institution data.

- [ ] **Step 6: Run tests and a manual training command**

Run:

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pytest tests/unit/test_m4_intent_dataset.py tests/integration/test_m4_intent_training.py -q
$artifactDir = Join-Path $env:TEMP ('m4-intent-' + [guid]::NewGuid().ToString('N'))
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' scripts/train_m4_intent.py --input data/m4_intent/example.jsonl --output-dir $artifactDir --seed 20260726 --min-confidence 0.70 --min-margin 0.10
Get-Content -LiteralPath (Join-Path $artifactDir 'metrics.json')
```

Expected: tests pass; CLI exits 0; all three artifact files exist; metrics explicitly identify the tiny example as sample-only evidence.

- [ ] **Step 7: Refactor and commit**

Run:

```powershell
git diff --check
git add src/course_insight/modules/m4_task_orchestration/intent_dataset.py scripts/train_m4_intent.py data/m4_intent/example.jsonl tests/unit/test_m4_intent_dataset.py tests/integration/test_m4_intent_training.py
git commit -m "feat: add offline M4 intent training"
```

Expected: commit succeeds and no generated binary artifact is tracked.

---

### Task 8: Documentation, Contract Regression, and Operational Acceptance

**Files:**

- Modify: `src/course_insight/modules/m4_task_orchestration/README.md`
- Modify: `README.md`
- Modify: `docs/postgresql_migration.md`
- Create: `docs/m4_intent_operations.md`
- Create: `tests/integration/test_m4_intent_acceptance.py`
- Modify: existing schema/provenance tests only if they enumerate infrastructure tables or migration versions.

**Interfaces:**

- Documents mode semantics, precedence, approved private identity B, refusal/OOS behavior, artifact trust boundary, training command, deployment/rollback, SQLite→PostgreSQL import, and monitoring fields.
- Acceptance test uses public `M4TaskOrchestrationService.create_task_plan(...)` and proves all five task types, refusal, replay across restart, modes, no public schema growth, and unchanged workflows.

- [ ] **Step 1: Write the failing stage-1 acceptance test**

```python
@pytest.mark.parametrize(
    ("text", "expected_type", "expected_workflow"),
    [
        ("请解释这个概念", "qa", ["M2", "M7", "M6"]),
        ("请进行诊断", "diagnostic", ["M8", "M2", "M7", "M5", "M6", "M9"]),
        ("请安排练习", "practice", ["M8", "M2", "M7", "M5", "M6", "M9"]),
        ("请订正错题", "correction", ["M8", "M2", "M7", "M5", "M6", "M9"]),
        ("请进行阶段测评", "stage_assessment", ["M8", "M2", "M7", "M5", "M6", "M9"]),
    ],
)
def test_stage1_task_plan_contract_is_unchanged(
    sqlite_application, text, expected_type, expected_workflow
) -> None:
    plan = create_plan(sqlite_application, student_text=text)
    assert plan.task_type == expected_type
    assert plan.workflow == expected_workflow
```

Add assertions that the public schema export remains exactly 84, TaskPlan fields are unchanged, rules mode imports without sklearn, repeated exact requests survive a process restart, distinct hint/text requests in one session remain distinct, OOS and conflicts raise recoverable `UNSUPPORTED_TASK`, and no raw student text exists in database rows.

- [ ] **Step 2: Run acceptance and schema tests and verify RED or missing-coverage evidence**

Run:

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pytest tests/integration/test_m4_intent_acceptance.py tests/test_contracts.py tests/test_contract_integrity.py -q
```

Expected: the new acceptance file initially fails where documentation/configuration/integration wiring is incomplete; schema tests remain green at 84.

- [ ] **Step 3: Write operational documentation with exact commands and rollback**

Document these safe transitions:

```text
rules -> shadow: install [intent], deploy verified artifact, set mode/backend/model_dir, restart, inspect shadow agreement and refusal metrics.
shadow -> active: retain the same verified artifact, set active only after offline and shadow gates pass.
active -> rules rollback: set mode=rules, backend=none, clear model_dir, restart; persisted exact-request decisions remain authoritative by design.
```

Explain that artifact directories are trusted administrative inputs because joblib deserialization can execute code; operators must publish read-only verified artifacts and never accept user-uploaded model paths.

- [ ] **Step 4: Run acceptance, M4, config, migration, and importer suites**

Run:

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pytest tests/unit/test_m4_*.py tests/integration/test_m4_*.py tests/unit/test_application_factory.py tests/unit/test_m0_config_loader.py tests/unit/test_sqlite_to_postgres_import.py tests/integration/test_sqlite_to_postgres_migration.py -q
```

Expected: all selected tests pass; only environment-guarded PostgreSQL live tests may skip.

- [ ] **Step 5: Refactor and commit**

Run:

```powershell
git diff --check
git add README.md src/course_insight/modules/m4_task_orchestration/README.md docs/postgresql_migration.md docs/m4_intent_operations.md tests/integration/test_m4_intent_acceptance.py
git add tests
git commit -m "docs: add M4 intent operations and acceptance"
```

Expected: commit succeeds with no generated artifact or secret.

---

### Task 9: Final Review, Security Gate, and Stage-1 Verification

**Files:**

- Review: every file changed since the plan commit.
- Modify: only files required to fix review or verification findings.

**Interfaces:**

- Produces an evidence-backed final report; no new product interface.

- [ ] **Step 1: Run specification-compliance and code-quality reviews**

Inspect:

```powershell
git diff --stat main...HEAD
git diff --check main...HEAD
git diff main...HEAD -- src/course_insight/contracts
git status --short
```

Expected: no whitespace errors, no public contract diff, and only intentional files in the branch.

- [ ] **Step 2: Run security checks**

Run:

```powershell
git grep -n -I -E '(api[_-]?key|password|secret|token)[[:space:]]*=[[:space:]]*[\"''][^\"'']+[\"'']' -- ':!tests/**' ':!docs/**'
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pip check
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pip audit
```

Expected: no hard-coded secret; `pip check` passes. If `pip audit` is unavailable, install it only into the isolated task environment and rerun; record network or advisory-service failure distinctly from a vulnerability finding.

- [ ] **Step 3: Run full tests and coverage**

Run:

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pytest tests -q
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m pytest tests --cov=course_insight --cov-report=term-missing --cov-fail-under=80 -q
```

Expected: all tests pass, only guarded skips remain, and total coverage is at least 80%.

- [ ] **Step 4: Run framework, schema, migration, and manual artifact gates**

Run:

```powershell
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m django check
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m django check --deploy
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' -m django makemigrations --check --dry-run
& 'D:\software\MyAnaconda\envs\course_insight_m4_intent_20260726\python.exe' scripts/export_schemas.py --check
```

Expected: standard check passes; deploy check has no new M4-origin warning; no Django migrations are generated; exported public schema count remains 84 and provenance tests pass.

- [ ] **Step 5: Fix every review/gate failure with a focused RED/GREEN cycle**

For each finding, add or tighten the smallest reproducing test, run it to demonstrate failure, patch only the responsible module, rerun the focused test, then rerun the failed gate. Do not weaken a correct test to make a defect disappear.

- [ ] **Step 6: Review final branch and create the final local commit**

Run:

```powershell
git status --short
git diff --check main...HEAD
git log --oneline --decorate main..HEAD
```

Expected: working tree clean, all planned commits present, and no push performed without separate explicit user authorization.

---

## Plan Self-Review

- Specification coverage: Tasks 1–2 cover types, policy, precedence, conflict detection, modes, refusal, privacy, replay, and the approved request-key repair. Tasks 3–4 cover SQLite/PostgreSQL v10 and importer parity. Tasks 5–7 cover optional dependencies, trusted runtime adapter, training, evaluation, and artifacts. Tasks 8–9 cover operations, all five task types, public-contract stability, security, and final stage-1 evidence.
- Placeholder scan: the plan contains no deferred implementation markers. Every code-producing task names concrete interfaces, tests, commands, expected RED/GREEN evidence, and commit boundaries.
- Type consistency: `IntentPrediction`, `IntentAdapter`, `IntentPolicy`, `StoredIntentDecision`, `M4IntentService`, and the two repository methods are introduced before any later task consumes them. The same six context fields plus normalized hint and text checksum define the private request identity throughout.
- Scope guard: TaskPlan keeps the existing eight-field identity and workflows; M0 operation identity is untouched; no public Pydantic contract is added; model loading is optional and lazy.
