"""M6 and M7 tutoring contracts owned by 陈."""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from course_insight.contracts.base import ContractModel
from course_insight.contracts.errors import DomainError
from course_insight.contracts.evidence import EvidenceQuery
from course_insight.contracts.state import DiagnosisResult


_SESSION_STATES = frozenset({"S0", "S1", "S2", "S3", "S4", "S5"})
_ALLOWED_TRANSITIONS = frozenset(
    {
        ("S0", "S1"),
        ("S1", "S2"),
        ("S1", "S3"),
        ("S2", "S3"),
        ("S3", "S4"),
        ("S4", "S5"),
        ("S4", "S2"),
        ("S4", "S3"),
    }
)
_ANSWER_MARKERS = (
    "final_answer",
    "final answer",
    "最终答案",
    "标准答案",
    "正确答案",
    "参考答案",
    "参考解答",
)

# Frozen v1 requires a non-empty quote even though the current learner UI does
# not display course excerpts.  M7 persists this controlled value instead of
# copying EvidenceChunk.text into a learner-visible contract.
STUDENT_CITATION_QUOTE_PLACEHOLDER = "证据摘录暂不展示"

_SOURCE_CITATION_ID = re.compile(
    r"^(?:source_[0-9]{1,9}|source-version-(?:"
    r"[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
    r"))$"
)
_EVIDENCE_CITATION_ID = re.compile(
    r"^evidence_(?:[0-9]{1,9}|chunk_[0-9]{1,9}|chunk_[0-9a-f]{64})$"
)
_CITATION_PHONE = re.compile(
    r"(?<!\d)(?:(?:\+?86)[-]?)?1[3-9]\d{9}(?!\d)"
    r"|(?<!\d)0\d{2,3}-\d{7,8}(?!\d)"
)
_CITATION_PRC_ID = re.compile(
    r"(?<!\d)\d{17}[0-9Xx](?!\d)"
)
_APPROVED_LOCATORS = (
    re.compile(r"^paragraph:[1-9][0-9]{0,8}$"),
    re.compile(
        r"^paragraph:[1-9][0-9]{0,8};lines:[1-9][0-9]{0,8}-"
        r"[1-9][0-9]{0,8}$"
    ),
    re.compile(
        r"^table:[1-9][0-9]{0,8};row:[1-9][0-9]{0,8};"
        r"cell:[1-9][0-9]{0,8}$"
    ),
    re.compile(
        r"^page:[1-9][0-9]{0,8};block:[1-9][0-9]{0,8}$"
    ),
    re.compile(r"^slide:[1-9][0-9]{0,8}$"),
    re.compile(
        r"^slide:[1-9][0-9]{0,8};shape:[1-9][0-9]{0,8};"
        r"paragraph:[1-9][0-9]{0,8}$"
    ),
    re.compile(
        r"^slide:[1-9][0-9]{0,8};table:[1-9][0-9]{0,8};"
        r"row:[1-9][0-9]{0,8};cell:[1-9][0-9]{0,8}$"
    ),
    # Numeric-only legacy forms already present in repository fixtures.  They
    # remain compatibility aliases, not a promise that arbitrary locators are
    # part of the public v1 contract.
    re.compile(
        r"^section(?P<section_sep>[:.-])[1-9][0-9]{0,5}"
        r"(?:(?P=section_sep)[1-9][0-9]{0,5}){0,4}$"
    ),
    re.compile(r"^p\.[1-9][0-9]{0,8}$"),
)


def _student_safe_citation_metadata(
    value: str,
    *,
    pattern: re.Pattern[str],
) -> bool:
    return (
        type(value) is str
        and value == value.strip()
        and pattern.fullmatch(value) is not None
        and not _contains_structured_personal_identifier(value)
    )


def _student_safe_locator(value: str) -> bool:
    return (
        type(value) is str
        and value == value.strip()
        and any(pattern.fullmatch(value) is not None for pattern in _APPROVED_LOCATORS)
        and not _contains_structured_personal_identifier(value)
    )


def _contains_structured_personal_identifier(value: str) -> bool:
    return (
        _CITATION_PHONE.search(value) is not None
        or _CITATION_PRC_ID.search(value) is not None
    )


def _raise_invalid_transition(current_state: str, next_state: str) -> None:
    raise DomainError(
        code="INVALID_STATE_TRANSITION",
        module="m6",
        message="the requested tutoring state transition is not allowed",
        details={
            "current_state": current_state,
            "next_state": next_state,
        },
    )


def _require_unique(values: list[str], *, entity: str) -> None:
    if len(values) != len(set(values)):
        raise DomainError(
            code="TUTORING_REFERENCE_CONFLICT",
            module="m6",
            message=f"{entity} identifiers must be unique",
        )


class SessionStateSnapshot(ContractModel):
    """Mutable session cursor constrained by the exact tutoring FSM."""

    session_id: str = Field(min_length=1)
    current_state: Literal["S0", "S1", "S2", "S3", "S4", "S5"]
    turn_count: int = Field(ge=0)
    completed_action_ids: list[str]
    updated_at: datetime

    def validate_business_rules(self) -> None:
        """Keep completed action history unique and synchronized with turns."""

        if any(not action_id for action_id in self.completed_action_ids):
            raise DomainError(
                code="SESSION_ACTION_ID_INVALID",
                module="m6",
                message="completed action identifiers must not be empty",
                details={"session_id": self.session_id},
            )
        _require_unique(self.completed_action_ids, entity="completed action")
        if self.turn_count != len(self.completed_action_ids):
            raise DomainError(
                code="SESSION_TURN_COUNT_MISMATCH",
                module="m6",
                message="session turn count must equal completed action count",
                details={
                    "turn_count": self.turn_count,
                    "completed_action_count": len(self.completed_action_ids),
                },
            )

    def can_transition_to(self, next_state: str) -> bool:
        """Return whether the exact S0-S5 matrix permits the transition."""

        return (self.current_state, next_state) in _ALLOWED_TRANSITIONS

    def transition(self, next_state: str, action_id: str) -> None:
        """Validate a complete candidate before atomically replacing state."""

        if not self.can_transition_to(next_state):
            _raise_invalid_transition(self.current_state, next_state)
        if not action_id:
            raise DomainError(
                code="SESSION_ACTION_ID_INVALID",
                module="m6",
                message="completed action identifier must not be empty",
                details={"session_id": self.session_id},
            )
        candidate_payload = {
            **self.model_dump(mode="python"),
            "current_state": next_state,
            "turn_count": self.turn_count + 1,
            "completed_action_ids": [*self.completed_action_ids, action_id],
        }
        candidate = type(self).model_validate(candidate_payload)
        object.__setattr__(self, "__dict__", dict(candidate.__dict__))
        object.__setattr__(
            self,
            "__pydantic_fields_set__",
            set(candidate.__pydantic_fields_set__),
        )


class TeachingAction(ContractModel):
    """One auditable action selected by the tutoring controller."""

    action_id: str = Field(min_length=1)
    state_before: str = Field(min_length=1)
    action_type: str = Field(min_length=1)
    target_concept_ids: list[str] = Field(min_length=1)
    prompt_template_id: str = Field(min_length=1)
    must_not_reveal_answer: bool
    next_state: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    def validate_business_rules(self) -> None:
        """Require a valid state pair and unambiguous concept targets."""

        if (
            self.state_before not in _SESSION_STATES
            or self.next_state not in _SESSION_STATES
            or (self.state_before, self.next_state) not in _ALLOWED_TRANSITIONS
        ):
            _raise_invalid_transition(self.state_before, self.next_state)
        _require_unique(self.target_concept_ids, entity="teaching target concept")

    def is_safe_hint(self) -> bool:
        """Return whether this action explicitly prohibits answer disclosure."""

        return self.must_not_reveal_answer


class FeedbackGenerationTask(ContractModel):
    """Evidence-bound local feedback generation request."""

    feedback_task_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    learner_id: str = Field(min_length=1)
    teaching_action: TeachingAction
    diagnosis_result: DiagnosisResult
    score_summary: dict[str, float]
    learner_state_snapshot_id: str = Field(min_length=1)
    evidence_query_id: str = Field(min_length=1)
    created_at: datetime

    @field_validator("score_summary")
    @classmethod
    def _validate_score_summary(
        cls,
        value: dict[str, float],
    ) -> dict[str, float]:
        if any(not key for key in value) or any(
            not math.isfinite(score) or score < 0.0 for score in value.values()
        ):
            raise ValueError("score summary requires nonnegative finite values")
        return dict(value)

    def validate_business_rules(self) -> None:
        """Align task learner and teaching targets with diagnosis evidence."""

        if self.learner_id != self.diagnosis_result.learner_id:
            raise DomainError(
                code="FEEDBACK_REFERENCE_MISMATCH",
                module="m6",
                message="feedback task learner must match the diagnosis learner",
                details={"feedback_task_id": self.feedback_task_id},
            )
        diagnosed_concepts = {
            concept_id
            for item in self.diagnosis_result.item_diagnoses
            for concept_id in item.concept_ids
        }
        if not set(self.teaching_action.target_concept_ids) <= diagnosed_concepts:
            raise DomainError(
                code="FEEDBACK_REFERENCE_MISMATCH",
                module="m6",
                message="teaching targets must be supported by the diagnosis",
                details={"feedback_task_id": self.feedback_task_id},
            )

    def target_concept_ids(self) -> list[str]:
        """Return an independent copy of the selected concept targets."""

        return list(self.teaching_action.target_concept_ids)

    def must_hide_answer(self) -> bool:
        """Return the action's explicit answer-disclosure restriction."""

        return self.teaching_action.must_not_reveal_answer


class TutoringControlResult(ContractModel):
    """Controller output joining action, feedback task, query, and session."""

    teaching_action: TeachingAction
    feedback_generation_task: FeedbackGenerationTask
    evidence_query: EvidenceQuery
    session_state_snapshot: SessionStateSnapshot
    created_at: datetime

    def validate_business_rules(self) -> None:
        """Apply query and state alignment at construction boundaries."""

        self.assert_query_alignment()

    def assert_query_alignment(self) -> None:
        """Require one action and feedback query across all result members."""

        task = self.feedback_generation_task
        action = self.teaching_action
        session = self.session_state_snapshot
        if (
            task.teaching_action != action
            or task.evidence_query_id != self.evidence_query.query_id
            or self.evidence_query.use_case != "feedback"
            or session.current_state != action.next_state
            or not session.completed_action_ids
            or session.completed_action_ids[-1] != action.action_id
        ):
            raise DomainError(
                code="TUTORING_QUERY_MISMATCH",
                module="m6",
                message="tutoring action, feedback task, query, and session must align",
                details={"action_id": action.action_id},
            )

    def next_state(self) -> str:
        """Return the validated next state selected by the action."""

        return self.teaching_action.next_state


class RubricFeedback(ContractModel):
    """Student-facing feedback for one scoring criterion."""

    criterion_id: str = Field(min_length=1)
    earned_score: float = Field(ge=0.0, allow_inf_nan=False)
    max_score: float = Field(ge=0.0, allow_inf_nan=False)
    message: str = Field(min_length=1)
    student_evidence: str

    def validate_business_rules(self) -> None:
        """Keep earned criterion credit within the declared maximum."""

        if self.earned_score > self.max_score:
            raise DomainError(
                code="RUBRIC_FEEDBACK_SCORE_INVALID",
                module="m7",
                message="earned feedback score cannot exceed its maximum",
                details={"criterion_id": self.criterion_id},
            )

    def is_full_score(self) -> bool:
        """Return whether earned credit equals the criterion maximum."""

        return math.isclose(
            self.earned_score,
            self.max_score,
            rel_tol=0.0,
            abs_tol=1e-9,
        )


class EvidenceCitation(ContractModel):
    """Course-source citation shown with generated feedback."""

    evidence_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    quote: str = Field(min_length=1)

    def safe_for_student(self) -> bool:
        """Reject free-form or personally identifying citation metadata."""

        values = (self.evidence_id, self.source_id, self.locator, self.quote)
        return (
            _student_safe_citation_metadata(
                self.evidence_id,
                pattern=_EVIDENCE_CITATION_ID,
            )
            and _student_safe_citation_metadata(
                self.source_id,
                pattern=_SOURCE_CITATION_ID,
            )
            and _student_safe_locator(self.locator)
            and self.quote == STUDENT_CITATION_QUOTE_PLACEHOLDER
            and all(
                marker not in value.casefold()
                for value in values
                for marker in _ANSWER_MARKERS
            )
        )

    def label(self) -> str:
        """Return a controlled label without echoing untrusted source text."""

        if not self.safe_for_student():
            return "课程证据"
        return f"{self.source_id}@{self.locator}"


class StudentFeedbackPackage(ContractModel):
    """Citation-backed feedback safe for direct learner display."""

    feedback_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    learner_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    rubric_feedback: list[RubricFeedback]
    missing_concept_ids: list[str]
    evidence_citations: list[EvidenceCitation]
    next_practice_item_ids: list[str]
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    generated_at: datetime

    def validate_business_rules(self) -> None:
        """Require unique criterion, concept, citation, and practice links."""

        groups = (
            (
                [feedback.criterion_id for feedback in self.rubric_feedback],
                "rubric criterion",
            ),
            (self.missing_concept_ids, "missing concept"),
            (
                [citation.evidence_id for citation in self.evidence_citations],
                "feedback citation",
            ),
            (self.next_practice_item_ids, "next practice item"),
        )
        for values, entity in groups:
            _require_unique(values, entity=entity)

    def has_citations(self) -> bool:
        """Return whether at least one course-evidence citation is present."""

        return bool(self.evidence_citations)

    def safe_for_student(self) -> bool:
        """Check every learner-visible string for answer disclosure markers."""

        visible_text = (
            self.feedback_id,
            self.task_id,
            self.learner_id,
            self.message,
            *(
                value
                for feedback in self.rubric_feedback
                for value in (
                    feedback.criterion_id,
                    feedback.message,
                    feedback.student_evidence,
                )
            ),
            *self.missing_concept_ids,
            *(
                value
                for citation in self.evidence_citations
                for value in (
                    citation.evidence_id,
                    citation.source_id,
                    citation.locator,
                    citation.quote,
                )
            ),
            *self.next_practice_item_ids,
        )
        return (
            self.has_citations()
            and all(
                citation.safe_for_student()
                for citation in self.evidence_citations
            )
            and not any(
                marker in value.casefold()
                for value in visible_text
                for marker in _ANSWER_MARKERS
            )
        )

    def citation_ids(self) -> list[str]:
        """Return citation identifiers without exposing nested mutable state."""

        return [citation.evidence_id for citation in self.evidence_citations]
