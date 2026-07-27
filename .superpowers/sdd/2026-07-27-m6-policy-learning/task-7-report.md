# Task 7 Report

## Status

COMPLETE

Completed the cross-mode, concurrency, security, provenance, and compatibility
regression for M6 policy learning. The public four-input
`M6TutoringControlService.decide_next_action(...)` contract and all 84 public
schemas remain unchanged.

## Delivered

- Rules mode remains the authoritative byte-for-byte public baseline.
- Shadow mode logs the baseline as the public/chosen action and stores the
  learned recommendation separately; active mode may choose only a candidate
  accepted by the existing safety envelope.
- New executions bind the authoritative M6 input fingerprint before policy
  selection. Their policy-execution fingerprint uses the approved literal
  seven-field ordered preimage, including explicit `null` for rules artifacts.
- Legacy policy execution, observation, and reward payloads retain their exact
  canonical JSON and checksum identities.
- Rich observations persist decision/input/request/execution identity, context
  and candidate checksums, complete logging probabilities and model scores,
  uncertainty, reasons, shadow action, full policy identity, and an aware
  timestamp.
- Learned epsilon selection records the real full action distribution; rules,
  fallback, forbidden exploration, and single-candidate selection remain
  deterministic one-hot logging policies.
- Rewards preserve raw outcome components and distinguish pending, censored,
  observed, and safety-invalid records. Safety-invalid outcomes cannot enter
  ordinary scalar-reward evaluation.
- Outcome event IDs and watermarks are constrained to path-free structured
  audit identifiers and reject common secret-like prefixes.
- The private JSONL export contains the approved minimum fields, HMACs
  learner/session/group identifiers, excludes raw identity and shadow action,
  and preserves valid logging-policy OPE semantics.
- SQLite, PostgreSQL, and SQLite-to-PostgreSQL validation bind a rich
  observation's embedded `decision_id` to its row and parent decision while
  preserving legacy slim observations.
- Same-request and competing-request concurrency, restart/recovery, M0 frozen
  policy identity, M4-to-M6-to-M2/M7 provenance, and zero-maximum scoring are
  covered.
- No DDL, migration, public contract, or generated schema was changed.

## TDD Evidence

Observed RED before GREEN for:

- Existing M4-to-M6 cross-module zero-maximum scoring raised
  `ZeroDivisionError`; the focused regression then passed with a deterministic
  zero score ratio.
- New policy execution fingerprinting initially rejected
  `input_fingerprint`; legacy literal checksum compatibility already passed.
- Rich observation/action-distribution tests initially failed because the
  runtime omitted input provenance and LinUCB omitted full probabilities.
- Raw reward/status/timezone tests initially failed while the legacy literal
  reward checksum remained stable.
- JSONL minimum-field and shadow logging-policy tests initially failed.
- SQLite import initially compared the new execution fingerprint against the
  full payload checksum.
- Checksum-valid rich observations with a mismatched embedded `decision_id`
  were accepted independently by SQLite, PostgreSQL, and source import:
  **3 failed**, then **3 passed** after row/parent binding validation.
- Path-like, free-text, secret-like, and email-like outcome provenance was
  accepted by outcome and reward records: **8 RED cases**, then the complete
  reward file passed (**19 passed**) after structured identifier validation.

## Verification

- Focused runtime/persistence/PostgreSQL/import/mode/concurrency batch:
  **100 passed**
- Final complete suite: **900 passed, 13 skipped**
- Final coverage suite: **898 passed, 13 skipped; 86% total coverage**
- Relevant M6 coverage: policy types **90%**, runtime **91%**, service **91%**,
  offline dataset **81%**, LinUCB **84%**, rewards **88%**
- `python -m compileall -q src tests`: passed
- `python -m pip check`: no broken requirements
- `manage.py check`: no issues
- `manage.py makemigrations --check --dry-run`: no changes detected
- `git diff --check`: passed
- Public schema count: **84**; `contracts/` diff: empty
- Independent correctness review found one high provenance binding gap; fixed
  with three corruption regressions.
- Independent security review confirmed no hardcoded secrets, SQL
  interpolation, or unsafe path resolution. Its one medium audit-identifier
  finding was fixed with six negative tests.

## Residual Items

- The 13 skipped tests require external/live PostgreSQL facilities unavailable
  in this task environment; fake-PostgreSQL parity and import tests passed.
- `pip_audit` is not installed in the isolated task environment. It was not
  installed because the task instructions prohibit modifying the environment;
  `pip check` passed.
- Django's dry-run migration check reported no changes and exited successfully,
  with its existing warning that the configured default SQLite database could
  not be opened for migration-history consistency checking.
- This task does not train, approve, deploy, or roll out a learned policy.
