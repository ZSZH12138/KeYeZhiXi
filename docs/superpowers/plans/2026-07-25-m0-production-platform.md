# M0 Production Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to execute this plan task-by-task. Every implementation task follows RED -> GREEN -> REFACTOR and receives an independent review.

**Goal:** Deliver the six requested production M0 capabilities after repairing the approved Web multi-request contract gap without breaking existing public contracts or module ownership.

**Architecture:** Keep M0 as the platform and Web shell, add module-owned durable recovery for existing contract objects, expose additive split coordinator use cases, assemble everything through one application factory, and support SQLite/PostgreSQL behind repository protocols.

**Tech Stack:** Python 3.11+, Pydantic v2, pydantic-settings 2.x, Django 5.2, Psycopg 3, SQLite, pytest, pytest-django.

## Global Constraints

- Preserve all 84 public contract classes, fields, semantics, and contract schema versions.
- Preserve every existing public Service method signature and return type.
- Add only the user-approved application use cases and minimal module-owned read/recovery methods.
- Do not implement DINA, BKT, IRT, adaptive selection, RAG, or external LLM behavior.
- M0 owns configuration, logging, auth, authorization, forms, Web, event/outbox, snapshots, health, and outer workflow metadata only.
- Django and Coordinator never query M1—M9 tables directly.
- Repositories only access tables prefixed for their owning module.
- Use exact Pydantic contracts across module boundaries; no duplicate public contracts.
- Write tests first and retain concrete RED/GREEN evidence.
- Target 80%+ coverage for new production modules and preserve the complete legacy suite.

---

### Task 1: Repair the multi-request application boundary

**Files:**
- Modify: `src/course_insight/application/coordinator.py`
- Modify: `src/course_insight/modules/m0_platform/repository.py`
- Modify: `src/course_insight/modules/m0_platform/service.py`
- Modify: `src/course_insight/modules/m4_task_orchestration/service.py`
- Modify: `src/course_insight/modules/m5_learner_class_state/repository.py`
- Modify: `src/course_insight/modules/m5_learner_class_state/service.py`
- Modify: `src/course_insight/modules/m7_local_model/repository.py`
- Modify: `src/course_insight/modules/m7_local_model/service.py`
- Modify: `src/course_insight/modules/m8_assessment_scoring/repository.py`
- Modify: `src/course_insight/modules/m8_assessment_scoring/service.py`
- Modify: `src/course_insight/modules/m9_teacher_analytics/repository.py`
- Modify: `src/course_insight/modules/m9_teacher_analytics/service.py`
- Modify: `src/course_insight/infrastructure/sqlite/migrations.py`
- Modify: `src/course_insight/infrastructure/sqlite/m0_repository.py`
- Add: `src/course_insight/infrastructure/sqlite/m5_repository.py`
- Add: `src/course_insight/infrastructure/sqlite/m7_repository.py`
- Add: `src/course_insight/infrastructure/sqlite/m8_repository.py`
- Add: `src/course_insight/infrastructure/sqlite/m9_repository.py`
- Test: `tests/unit/test_application_web_workflow.py`
- Test: `tests/integration/test_web_workflow_persistence.py`
- Test: `tests/contract/test_public_contract_stability.py`

- [ ] Write failing tests for start/submit/reload/review across fresh service instances.
- [ ] Add M0-owned assessment-run metadata with idempotency keys, CAS versions, short leases, and guarded checkpoints.
- [ ] Add module-owned save/get methods for complete existing contract objects.
- [ ] Persist and restore M8 paper execution scope so post-restart events retain real course/class IDs.
- [ ] Make M5 state history course/class aware and persist attempt-bound complete state results.
- [ ] Persist M5 state results, M7 feedback, M8 papers/scoring bundles, and M9 analytics/reviews.
- [ ] Add M4 plan lookup and the split Coordinator use cases.
- [ ] Prove duplicate/concurrent POSTs and failures after every module save can resume without divergent results.
- [ ] Prove a second assessment after restart and the same actor in two courses use the correct M5 history.
- [ ] Prove existing one-shot Coordinator methods remain unchanged.
- [ ] Prove Schema count remains 84 and public signatures remain compatible.

### Task 2: Implement configuration and role seed parsing

**Files:**
- Modify: `pyproject.toml`
- Modify: `.env.example`
- Modify: `config/app.example.json`
- Add: `config/roles.example.csv`
- Add: `src/course_insight/infrastructure/config/__init__.py`
- Add: `src/course_insight/infrastructure/config/models.py`
- Add: `src/course_insight/infrastructure/config/loader.py`
- Add: `src/course_insight/infrastructure/config/sources.py`
- Add: `src/course_insight/infrastructure/config/roles.py`
- Add: `src/course_insight/infrastructure/config/errors.py`
- Test: `tests/unit/test_m0_config_loader.py`
- Test: `tests/unit/test_m0_roles_loader.py`

- [ ] Write failing precedence, validation, path-boundary, secret, NaN/Infinity, and production-security tests.
- [ ] Implement immutable internal settings groups and deterministic source precedence.
- [ ] Reject secret values in `app.json` and redact configuration errors.
- [ ] Implement strict `roles.csv` parsing, duplicate/conflict detection, checksum, and idempotent diff plans.
- [ ] Verify tests do not read the developer's real `.env`.

### Task 3: Add the application factory and runtime context registry

**Files:**
- Add: `src/course_insight/application/factory.py`
- Add: `src/course_insight/application/runtime_context.py`
- Modify: `src/course_insight/application/__init__.py`
- Modify: `src/course_insight/modules/m0_platform/service.py`
- Modify: `src/course_insight/cli.py`
- Test: `tests/unit/test_application_factory.py`

- [ ] Write failing tests for SQLite/PostgreSQL selection, dependency identity, startup failure, and test overrides.
- [ ] Add optional repository injection to M0 without breaking its legacy constructor.
- [ ] Build one immutable `ApplicationContainer`.
- [ ] Load configured contract snapshots through M0 and restore M2's in-process index safely.
- [ ] Explicitly keep M1—M3 on the runtime-snapshot path in this milestone.
- [ ] Make CLI, Web, and Worker use the same factory.

### Task 4: Implement structured application logging

**Files:**
- Modify: `src/course_insight/infrastructure/logging.py`
- Add: `src/course_insight/infrastructure/log_context.py`
- Test: `tests/unit/test_m0_logging.py`

- [ ] Write failing tests for JSON lines, context propagation, recursive redaction, rotation, thread safety, and unavailable sinks.
- [ ] Implement contextvars-based correlation context.
- [ ] Implement recursive key/value redaction before formatting.
- [ ] Support single-process rotating files in development and stdout-only production mode.
- [ ] Prove DomainError output contains neither absolute paths nor secret values.

### Task 5: Replace request-triggered delivery with a permanent Outbox Worker

**Files:**
- Modify: `src/course_insight/modules/m0_platform/repository.py`
- Modify: `src/course_insight/modules/m0_platform/service.py`
- Modify: `src/course_insight/modules/m0_platform/event_store.py`
- Modify: `src/course_insight/infrastructure/sqlite/migrations.py`
- Modify: `src/course_insight/infrastructure/sqlite/m0_repository.py`
- Add: `src/course_insight/modules/m0_platform/outbox.py`
- Add: `src/course_insight/modules/m0_platform/outbox_worker.py`
- Test: `tests/unit/test_m0_outbox_worker.py`
- Test: `tests/integration/test_outbox_worker_concurrency.py`

- [ ] Write failing claim/lease/ack/retry/dead/reclaim/restart/concurrency tests.
- [ ] Add versioned outbox status, lease, attempt, and error-code columns.
- [ ] Move all JSONL I/O outside database transactions.
- [ ] Implement atomic batch claims and conditional acknowledgements.
- [ ] Implement an idempotent JSONL sink keyed by `event_id`.
- [ ] Implement bounded polling, jittered backoff, heartbeat, once mode, and signal-safe shutdown.
- [ ] Keep explicit legacy delivery behavior available as a compatibility wrapper.
- [ ] Remove all automatic delivery calls from M0 initialization and business-request paths.

### Task 6: Implement PostgreSQL migrations and repository adapters

**Files:**
- Add: `src/course_insight/infrastructure/postgresql/__init__.py`
- Add: `src/course_insight/infrastructure/postgresql/connection.py`
- Add: `src/course_insight/infrastructure/postgresql/pool.py`
- Add: `src/course_insight/infrastructure/postgresql/migration_runner.py`
- Add: `src/course_insight/infrastructure/postgresql/migrations/`
- Add: `src/course_insight/infrastructure/postgresql/m0_repository.py`
- Add: `src/course_insight/infrastructure/postgresql/m4_repository.py`
- Add: `src/course_insight/infrastructure/postgresql/m5_repository.py`
- Add: `src/course_insight/infrastructure/postgresql/m6_repository.py`
- Add: `src/course_insight/infrastructure/postgresql/m7_repository.py`
- Add: `src/course_insight/infrastructure/postgresql/m8_repository.py`
- Add: `src/course_insight/infrastructure/postgresql/m9_repository.py`
- Add: `scripts/migrate_sqlite_to_postgres.py`
- Test: `tests/integration/test_postgres_m0_repository.py`
- Test: `tests/integration/test_postgresql_m4_m6_repository_parity.py`
- Test: `tests/integration/test_postgres_m5_m9_repositories.py`
- Test: `tests/integration/test_sqlite_to_postgres_migration.py`

- [ ] Write PostgreSQL tests that skip only when the documented test URL is absent.
- [ ] Implement pooled Psycopg 3 connections without leaking driver types.
- [ ] Implement ordered checksum migrations guarded by an advisory lock.
- [ ] Match SQLite atomicity, uniqueness, optimistic-version, and outbox lease semantics.
- [ ] Implement dry-run and resumable batched SQLite export/import.
- [ ] Revalidate every payload with its existing Pydantic contract and compare IDs, versions, row counts, and checksums.
- [ ] Write a safe migration report, retain the source SQLite file, roll back failed batches, and prove repeat execution is idempotent.
- [ ] Report PostgreSQL tests as untested unless a real server executes them.

### Task 7: Implement Django identity, authorization, forms, views, and templates

**Files:**
- Add: `manage.py`
- Add: `src/course_insight/web_project/__init__.py`
- Add: `src/course_insight/web_project/settings.py`
- Add: `src/course_insight/web_project/urls.py`
- Add: `src/course_insight/web_project/asgi.py`
- Add: `src/course_insight/web_project/wsgi.py`
- Add: `src/course_insight/modules/m0_platform/django_app/`
- Add: `src/course_insight/modules/m0_platform/templates/m0_platform/`
- Add: `src/course_insight/modules/m0_platform/templates/registration/login.html`
- Test: `tests/unit/test_m0_authorization.py`
- Test: `tests/unit/test_m0_forms.py`
- Test: `tests/integration/test_django_student_flow.py`
- Test: `tests/integration/test_django_teacher_review.py`

- [ ] Write failing auth scope, form tampering, CSRF, PRG, error mapping, rate-limit, and health tests.
- [ ] Add custom pseudonymous User, ActorGrant, role-sync state, and Django migrations.
- [ ] Convert active grants into the existing `ActorContext`.
- [ ] Implement centralized permission plus actor/course/class/learner authorization.
- [ ] Build dynamic student and teacher forms that return the existing submission contracts.
- [ ] Add student assessment/result and teacher analytics/review views with no direct business-table access.
- [ ] Add login/logout, secure errors, live/ready health, middleware, and safe templates.
- [ ] Add `sync_roles` and `run_outbox_worker` management commands.

### Task 8: Integrate, document, and verify the production surface

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/interface_guide.md`
- Modify: `src/course_insight/modules/m0_platform/README.md`
- Modify when required: `contracts/contract_provenance.json`
- Add: `docs/deployment.md`
- Add: `docs/postgresql_migration.md`
- Add: `docs/outbox_worker.md`
- Add: `tests/e2e/test_m0_golden_path.py`

- [ ] Add a deterministic SQLite golden path covering roles, student submission, worker delivery, teacher override, and refreshed analytics.
- [ ] Add only truthful provenance edges for new public application/service methods.
- [ ] Document precedence, secrets, backend switching, worker semantics, Web startup, migrations, rollback, and manual acceptance.
- [ ] Run the full pytest suite with coverage.
- [ ] Run compileall, schema export twice, pip check, Django checks/migrations/commands, and diff checks.
- [ ] Run security and whole-branch code reviews; fix all critical/high findings.
- [ ] Record exact command, exit code, pass/fail/skip counts, and duration in the final report.
