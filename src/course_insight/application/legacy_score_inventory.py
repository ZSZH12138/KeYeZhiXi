"""Detect completed submits that posted pending or rejected scores before review."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from course_insight.contracts.assessment import ScoringResultBundle
from course_insight.contracts.errors import DomainError
from course_insight.modules.m0_platform.workflow import (
    WAITING_WORKFLOW_STATUSES,
    AssessmentRun,
)

LEGACY_SCORE_FLAG = "legacy_score_posted_before_review"
_POSTED_CHECKPOINTS = frozenset(
    {
        "events_appended",
        "state_inputs_frozen",
        "state_saved",
        "policy_frozen",
        "tutoring_saved",
        "feedback_saved",
        "analytics_saved",
        "completed",
    }
)


@dataclass(frozen=True, slots=True)
class LegacyScoreFlag:
    """One completed submit that must not be treated as a clean waiting-room run."""

    operation_id: str
    paper_id: str
    attempt_id: str | None
    flag: Literal["legacy_score_posted_before_review"]
    rebuildable: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "paper_id": self.paper_id,
            "attempt_id": self.attempt_id,
            "flag": self.flag,
            "rebuildable": self.rebuildable,
            "reason": self.reason,
            "clean": False,
        }


def inspect_legacy_score_posting(
    run: AssessmentRun,
    scoring: ScoringResultBundle,
) -> LegacyScoreFlag | None:
    """Mark polluted completed submits; waiting-room runs are not legacy."""

    if run.operation != "submit" or run.status in WAITING_WORKFLOW_STATUSES:
        return None
    dirty = scoring.requires_teacher_review() or scoring.has_rejected_score()
    posted = run.checkpoint in _POSTED_CHECKPOINTS or run.state_version is not None
    if run.status != "completed" or not dirty or not posted:
        return None
    return LegacyScoreFlag(
        operation_id=run.operation_id,
        paper_id=run.paper_id,
        attempt_id=run.attempt_id,
        flag=LEGACY_SCORE_FLAG,
        rebuildable=run.previous_state_frozen is True,
        reason="completed_submit_posted_pending_or_rejected_score",
    )


def rebuild_legacy_score_posting(
    flag: LegacyScoreFlag,
    *,
    apply: bool,
) -> LegacyScoreFlag:
    """Fail closed unless a frozen pre-score baseline exists; never auto-rewind M5."""

    if flag.flag != LEGACY_SCORE_FLAG or flag.reason == "":
        raise DomainError(
            code="LEGACY_SCORE_FLAG_INVALID",
            module="application",
            message="legacy score inventory flag is invalid",
            recoverable=True,
        )
    if not flag.rebuildable:
        raise DomainError(
            code="LEGACY_BASELINE_MISSING",
            module="application",
            message="legacy polluted attempt cannot be rebuilt without a frozen baseline",
            recoverable=True,
        )
    if apply:
        raise DomainError(
            code="LEGACY_REBUILD_NOT_AUTOMATIC",
            module="application",
            message=(
                "automatic M5 rewind is disabled; replay from the frozen baseline "
                "with only accepted scores"
            ),
            recoverable=True,
        )
    return flag
