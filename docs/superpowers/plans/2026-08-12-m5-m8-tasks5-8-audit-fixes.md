# M5/M8 Task 5–8 Audit Fixes Implementation Plan

> **For agentic workers:** Execute inline and single-threaded because the user explicitly prohibited subagents. Follow each checkbox in order with a RED/GREEN checkpoint.

**Goal:** Repair the four independently reproduced Task 5–8 defects without changing the public M5/M8 service entry points.

**Architecture:** DINA fits independent Q-matrix components and merges their immutable artifacts. M5 normalizes authoritative audit evidence at ingestion and historical replay, then runs both diagnosis and tracing over the learner's complete history. IRT separates stable parameter-set identity from timestamped calibration-run identity.

**Tech Stack:** Python 3.12, Pydantic, NumPy, SciPy, SQLite, PostgreSQL 16, pytest.

## Global Constraints

- Use `D:\software\MyAnaconda\envs\course_insight_m5m8_tasks1_4_20260812\python.exe` for every Python command.
- Do not change public M5/M8 service method signatures.
- Same `(source_audit_id, source_audit_version)` is consumed once; conflicting content fails closed.
- Keep append-only persistence and canonical checksum validation.
- Write and run each failing regression test before production changes.
- Do not use subagents, push, merge, or modify unrelated user changes.

---

### Task 1: DINA connected-component fitting

**Files:**
- Modify: `tests/unit/test_m5_dina.py`
- Modify: `src/course_insight/modules/m5_learner_class_state/dina.py`

**Interfaces:**
- Consumes: existing `DinaEngine.fit(cohort, requested_at)` and the item-to-concept Q-matrix in `LearningObservation`.
- Produces: one `DinaModelArtifact` whose item parameters and priors cover every component.

- [ ] Add a regression test with 13 disconnected one-concept items and sufficient learners; assert `inference_mode == "exact"` and all 13 item/concept parameters are present.
- [ ] Run `python -m pytest tests/unit/test_m5_dina.py -k disconnected -q` and confirm it fails because the current engine selects variational mode from the total concept count.
- [ ] Build deterministic Q-matrix connected components, prepare one component-specific cohort per component, run exact or variational EM by component size, and merge results in stable identifier order.
- [ ] Aggregate learner/observation counts from the governed full cohort without double counting; set overall mode to variational only when at least one component is variational.
- [ ] Run `python -m pytest tests/unit/test_m5_dina.py tests/model_validation/test_dina_recovery.py -q` and confirm all DINA tests pass.

### Task 2: Authoritative audit deduplication

**Files:**
- Modify: `tests/unit/test_m5_dina.py`
- Modify: `tests/unit/test_postgres_m5_repository.py`
- Modify: `tests/integration/test_postgres_m5_m9_repositories.py`
- Modify: `src/course_insight/modules/m5_learner_class_state/dina.py`
- Modify: `src/course_insight/modules/m5_learner_class_state/service.py`
- Modify: `src/course_insight/infrastructure/sqlite/m5_repository.py`
- Modify: `src/course_insight/infrastructure/sqlite/m5_m8_model_runtime_schema.py`
- Modify: `src/course_insight/infrastructure/postgresql/m5_repository.py`
- Create: `src/course_insight/infrastructure/postgresql/migrations/0015_m5_learning_observation_audit_identity.sql`

**Interfaces:**
- Consumes: immutable `LearningObservation` records.
- Produces: deterministic governed evidence with one record per audit identity.

- [ ] Add tests proving an identical audit replay does not change DINA sample size and a conflicting replay is rejected.
- [ ] Add SQLite and PostgreSQL repository tests proving the same audit identity cannot be stored under a second observation ID.
- [ ] Run the new tests and confirm failures come from observation-ID-only identity handling.
- [ ] Add one canonical audit normalization helper used by DINA and M5 historical replay. Compare full contract payloads when an audit key repeats; ignore exact duplicates and raise a recoverable domain conflict for different payloads.
- [ ] Persist audit ID/version as indexed columns and add a unique scope-safe database constraint in SQLite and PostgreSQL. Preserve insert-or-get behavior for exact retries.
- [ ] Run repository, DINA, schema, and PostgreSQL unit tests and confirm they pass.

### Task 3: Complete-history M5 inference

**Files:**
- Modify: `tests/integration/test_m8_m5_model_chain.py`
- Modify: `src/course_insight/modules/m5_learner_class_state/service.py`

**Interfaces:**
- Consumes: `M5Repository.list_learning_observations(course_id, class_id)` after the current batch is persisted.
- Produces: `LearningModelRun` whose DINA diagnosis and BKT trace use the learner's complete governed history.

- [ ] Add an integration regression test with a correct first attempt and an incorrect second attempt; compare the second result with a direct full-history BKT replay.
- [ ] Add a restart assertion using a new service over the same repository; the mastery, observation count, audit keys, and watermark must match uninterrupted execution.
- [ ] Run the new integration test and confirm it fails because the current service restarts BKT from the model prior.
- [ ] Load all observations for the current course/class, filter the current learner, normalize audit identities, sort by event order, and build a deterministic history batch.
- [ ] Run DINA and every BKT concept sequence against that same history batch. Build run IDs, counts, audit keys, and watermarks from the history rather than the current batch.
- [ ] Run M5 BKT unit tests and the M8→M5 integration tests and confirm they pass.

### Task 4: IRT immutable identity separation

**Files:**
- Modify: `tests/unit/test_m8_irt_2pl.py`
- Modify: `src/course_insight/modules/m8_assessment_scoring/irt_2pl.py`

**Interfaces:**
- Consumes: normalized calibration observations plus `requested_at`.
- Produces: stable `IRTParameterSet` identity and request-specific `CalibrationRunResult` identity.

- [ ] Add one test proving identical observations and identical request time return an equal result.
- [ ] Add one test proving identical observations at a later request time retain an equal parameter set but use a different run ID and generated time.
- [ ] Run both tests and confirm the later-time case fails because the current run ID stays the same while timestamps change.
- [ ] Derive parameter-set identity and `created_at` from normalized evidence and its maximum `occurred_at`; derive run identity from the parameter-set identity, status, and normalized UTC request time.
- [ ] Apply the same no-conflict rule to insufficient-data and non-convergence results.
- [ ] Run IRT unit and fixed-seed recovery tests and confirm they pass.

### Task 5: Cross-storage and full verification

**Files:**
- Modify only files required by failures exposed in this verification task.

**Interfaces:**
- Consumes: completed Task 1–4 repairs.
- Produces: evidence that Task 5–8 satisfy the approved design.

- [ ] Run targeted Task 5–8 unit, integration, and model-validation suites.
- [ ] Initialize fresh SQLite schema twice and verify idempotence plus the audit unique constraint.
- [ ] Run the real PostgreSQL 16 round-trip suite including migration 0015 and audit conflict checks.
- [ ] Run the entire pytest suite with coverage and require at least 80%.
- [ ] Run `compileall`, `pip check`, configured lint/type checks when available, and a secret-pattern scan over the diff.
- [ ] Review `git diff --check`, `git status`, and every changed file against the design acceptance list.
- [ ] Commit the repair in small conventional commits only after fresh verification evidence.
