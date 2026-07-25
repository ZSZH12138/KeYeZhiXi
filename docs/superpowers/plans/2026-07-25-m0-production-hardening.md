# M0 Production Hardening Implementation Plan

**Goal:** Close the remaining M0 correctness gaps while preserving module
ownership, the 84 public schemas, and existing public service signatures.

**Architecture:** Keep M0 as payload-free workflow metadata and infrastructure.
Freeze executable dependency identities on assessment rows, freeze exact M5 state
baselines before state computation, use CAS heartbeats for long work, and preserve
at-least-once outbox semantics.

**Tech stack:** Python 3.11, dataclasses, Pydantic v2, Django, SQLite, PostgreSQL
with psycopg 3, pytest.

---

### Task 1: Successful-login rate limiting

**Files**

- Modify: `tests/unit/test_m0_authorization.py`
- Modify: `tests/integration/test_django_health_login.py`
- Modify: `src/course_insight/modules/m0_platform/django_app/authz.py`
- Modify: `src/course_insight/modules/m0_platform/django_app/views/auth.py`

**Steps**

1. Add a failing helper test proving actor reset can preserve the IP bucket.
2. Add a failing HTTP test proving a valid login cannot erase cross-account IP
   failures.
3. Add the compatible reset option and select it on successful login.
4. Run focused unit and Django integration tests.

### Task 2: Workflow metadata and storage

**Files**

- Modify: `src/course_insight/modules/m0_platform/workflow.py`
- Modify: `src/course_insight/modules/m0_platform/repository.py`
- Modify: `src/course_insight/modules/m0_platform/service.py`
- Modify: `src/course_insight/infrastructure/sqlite/workflow_migration.py`
- Modify: `src/course_insight/infrastructure/sqlite/migrations.py`
- Modify: `src/course_insight/infrastructure/sqlite/m0_repository.py`
- Modify: `src/course_insight/infrastructure/postgresql/m0_repository.py`
- Add: `src/course_insight/infrastructure/postgresql/migrations/0009_m0_workflow_recovery_freeze.sql`
- Modify focused SQLite/PostgreSQL repository tests.

**Steps**

1. Add failing tests for dependency round trips, safe pre-v9 adoption,
   lease-renewal CAS, expired-owner rejection, and exact state-baseline fields.
2. Add immutable private fields and the `state_inputs_frozen` checkpoint.
3. Add SQLite v9 and PostgreSQL 0009 forward migrations.
4. Implement equivalent SQLite/PostgreSQL mapping, validation, narrow legacy
   adoption, renewal, and expired-lease guards.
5. Run repository and migration parity tests.

### Task 3: Exact M5 baseline recovery

**Files**

- Modify: `src/course_insight/modules/m5_learner_class_state/repository.py`
- Modify: `src/course_insight/modules/m5_learner_class_state/service.py`
- Modify: `src/course_insight/infrastructure/sqlite/m5_repository.py`
- Modify: `src/course_insight/infrastructure/postgresql/m5_repository.py`
- Modify focused M5 repository tests.

**Steps**

1. Add failing tests for exact scoped learner/class version reads.
2. Add non-breaking repository and service methods.
3. Implement parameterized SQLite and PostgreSQL queries.
4. Verify missing, matching, and cross-scope cases.

### Task 4: Assessment heartbeat and deterministic replay

**Files**

- Add: `src/course_insight/modules/m0_platform/lease_heartbeat.py`
- Add: `src/course_insight/application/assessment_dependencies.py`
- Modify: `src/course_insight/application/assessment_workflow.py`
- Modify: `tests/unit/test_application_web_workflow.py`
- Modify: `tests/integration/test_web_workflow_persistence.py`

**Steps**

1. Add failing tests for dependency mismatch, policy drift, lease loss, and a
   concurrent latest-state change during recovery.
2. Build canonical dependency fingerprints and validate them against the start row
   and replay row.
3. Make M5/M9 checksum and parse the same single-read policy bytes.
4. Freeze the pre-state before calling M5, then restore only exact snapshots.
5. Wrap long module calls in lease heartbeats and stop after lost ownership.
6. Keep stale failure reporting best-effort so it cannot hide the original error.
7. Run focused workflow unit and persistence tests.

### Task 5: Legacy outbox callback heartbeat

**Files**

- Modify: `src/course_insight/infrastructure/sqlite/m0_outbox_repository.py`
- Modify: `src/course_insight/infrastructure/postgresql/m0_outbox_repository.py`
- Modify: `tests/unit/test_m0_outbox_worker.py`
- Modify: `tests/unit/test_postgres_m0_outbox_parity.py`

**Steps**

1. Add failing tests for renewal during a slow callback.
2. Add failing tests proving a stale worker performs no terminal ack/fail write.
3. Reuse the shared heartbeat executor without changing the compatibility method
   signature.
4. Run SQLite/PostgreSQL parity and outbox concurrency tests.

### Task 6: Review and release gate

**Files**

- Update directly relevant M0 documentation and the external current-
  implementation report only after the gate passes.

**Steps**

1. Review the complete diff for public-contract, module-boundary, security, and
   migration correctness.
2. Re-read all repository Markdown and scan all M0 source paths.
3. Run formatting/lint/type checks configured by the repository.
4. Run full pytest with coverage and confirm at least 80%.
5. Run Django `check --deploy`, `makemigrations --check --dry-run`, role sync,
   schema export/provenance checks, migration checks, and secret scans.
6. Record live PostgreSQL tests as passed or explicitly skipped based on actual
   environment evidence.
7. Only if no major source defect remains: update
   `C:\Users\DELL\Desktop\tmp\M0_M4_M6_当前实现说明.md`, review the exact staged
   diff, create a conventional commit, and push the current branch.
8. Otherwise do not publish and report each issue as 主要问题 / 预期实现 /
   实际实现 / 证据 / 建议修正方法.
