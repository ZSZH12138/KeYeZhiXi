"""Version-bound teacher review form."""

from __future__ import annotations

import hashlib
import math
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from django import forms

from course_insight.contracts.analytics import (
    CriterionOverride,
    TeacherReviewDecision,
)
from course_insight.contracts.assessment import ScoreAuditRecord
from course_insight.contracts.platform import TeacherReviewSubmission


_SCORE_TOLERANCE = 1e-9
_MAX_COMMENT_LENGTH = 4_000
_MAX_REASON_LENGTH = 2_000


class TeacherReviewForm(forms.Form):
    """Validate a complete optimistic review against server-owned audit data."""

    decision = forms.ChoiceField(
        choices=(
            ("confirm", "确认"),
            ("override", "改分"),
            ("reject", "驳回"),
        )
    )
    final_total_score = forms.FloatField(min_value=0.0)
    teacher_comment = forms.CharField(
        min_length=1,
        max_length=_MAX_COMMENT_LENGTH,
        strip=True,
        widget=forms.Textarea,
    )

    def __init__(
        self,
        *,
        audit: ScoreAuditRecord,
        reviewer_id: str,
        criterion_caps: Mapping[str, float],
        data=None,
        **kwargs,
    ) -> None:
        audit.validate_business_rules()
        if not reviewer_id:
            raise ValueError("reviewer identity is required")
        current_ids = {
            criterion.criterion_id for criterion in audit.criterion_scores
        }
        if set(criterion_caps) != current_ids:
            raise ValueError("criterion score caps are incomplete")
        normalized_caps: dict[str, float] = {}
        for criterion_id, cap in criterion_caps.items():
            if (
                type(cap) not in {int, float}
                or not math.isfinite(float(cap))
                or float(cap) < 0.0
            ):
                raise ValueError("criterion score cap is invalid")
            normalized_caps[criterion_id] = float(cap)
        if not math.isclose(
            math.fsum(normalized_caps.values()),
            audit.max_score,
            rel_tol=0.0,
            abs_tol=_SCORE_TOLERANCE,
        ):
            raise ValueError("criterion score caps do not match audit maximum")
        self._audit = audit.model_copy(deep=True)
        self._reviewer_id = reviewer_id
        self._criterion_caps = MappingProxyType(normalized_caps)
        super().__init__(data=data, **kwargs)
        if self._audit.is_rejected():
            self.fields["decision"].choices = (("override", "改分"),)
        for criterion in self._audit.criterion_scores:
            self.fields[self.score_field_name(criterion.criterion_id)] = (
                forms.FloatField(
                    min_value=0.0,
                    max_value=normalized_caps[criterion.criterion_id],
                    required=False,
                    label=criterion.criterion_id,
                )
            )
            self.fields[self.reason_field_name(criterion.criterion_id)] = (
                forms.CharField(
                    min_length=1,
                    max_length=_MAX_REASON_LENGTH,
                    required=False,
                    strip=True,
                    label=f"{criterion.criterion_id} reason",
                )
            )

    @staticmethod
    def score_field_name(criterion_id: str) -> str:
        return f"criterion_score_{_identifier_digest(criterion_id)}"

    @staticmethod
    def reason_field_name(criterion_id: str) -> str:
        return f"criterion_reason_{_identifier_digest(criterion_id)}"

    def clean(self) -> dict[str, object]:
        cleaned = super().clean()
        if self.is_bound:
            allowed = {*self.fields, "csrfmiddlewaretoken"}
            if set(self.data) - allowed:
                raise forms.ValidationError("Unexpected form field.")
            getlist = getattr(self.data, "getlist", None)
            if callable(getlist) and any(
                len(getlist(field_name)) != 1
                for field_name in self.data
            ):
                raise forms.ValidationError("Duplicate form field.")
        decision = cleaned.get("decision")
        total = cleaned.get("final_total_score")
        if type(total) is float and not math.isfinite(total):
            self.add_error("final_total_score", "Enter a finite number.")
            return cleaned
        if decision != "override":
            self._validate_non_override(cleaned, decision, total)
            return cleaned

        scores: list[float] = []
        for criterion in self._audit.criterion_scores:
            score_name = self.score_field_name(criterion.criterion_id)
            reason_name = self.reason_field_name(criterion.criterion_id)
            score = cleaned.get(score_name)
            reason = cleaned.get(reason_name)
            if type(score) not in {int, float} or not math.isfinite(
                float(score)
            ):
                self.add_error(score_name, "A finite score is required.")
                continue
            if float(score) > self._criterion_caps[criterion.criterion_id]:
                self.add_error(score_name, "Score exceeds criterion maximum.")
            if not isinstance(reason, str) or not reason.strip():
                self.add_error(reason_name, "A reason is required.")
            scores.append(float(score))
        if type(total) in {int, float} and math.isfinite(float(total)):
            if float(total) > self._audit.max_score:
                self.add_error(
                    "final_total_score",
                    "Total exceeds the audit maximum.",
                )
            if not math.isclose(
                math.fsum(scores),
                float(total),
                rel_tol=0.0,
                abs_tol=_SCORE_TOLERANCE,
            ):
                self.add_error(
                    "final_total_score",
                    "Criterion scores must equal the final total.",
                )
        return cleaned

    def to_submission(
        self,
        *,
        submission_id: str,
        submitted_at: datetime,
    ) -> TeacherReviewSubmission:
        if not self.is_valid():
            raise ValueError("teacher review form is invalid")
        decision = str(self.cleaned_data["decision"])
        overrides = (
            self._overrides() if decision == "override" else []
        )
        submission = TeacherReviewSubmission(
            submission_id=submission_id,
            audit_id=self._audit.audit_id,
            expected_audit_version=self._audit.audit_version,
            expected_audit_checksum=self._audit.content_checksum(),
            reviewer_id=self._reviewer_id,
            decision=decision,
            final_total_score=float(
                self.cleaned_data["final_total_score"]
            ),
            criterion_overrides=overrides,
            teacher_comment=str(
                self.cleaned_data["teacher_comment"]
            ).strip(),
            submitted_at=submitted_at,
        )
        decision_contract = TeacherReviewDecision(
            decision_id=submission.submission_id,
            audit_id=submission.audit_id,
            expected_audit_version=submission.expected_audit_version,
            expected_audit_checksum=submission.expected_audit_checksum,
            decision=submission.decision,
            final_total_score=submission.final_total_score,
            criterion_overrides=[
                item.model_copy(deep=True)
                for item in submission.criterion_overrides
            ],
            teacher_comment=submission.teacher_comment,
            reviewer_id=submission.reviewer_id,
            reviewed_at=submission.submitted_at,
        )
        decision_contract.validate_business_rules()
        decision_contract.assert_matches(self._audit)
        return submission.model_copy(deep=True)

    def _validate_non_override(
        self,
        cleaned: dict[str, object],
        decision: object,
        total: object,
    ) -> None:
        if decision not in {"confirm", "reject"}:
            return
        if type(total) in {int, float} and not math.isclose(
            float(total),
            self._audit.total_score,
            rel_tol=0.0,
            abs_tol=_SCORE_TOLERANCE,
        ):
            self.add_error(
                "final_total_score",
                "Non-override review must retain the current total.",
            )
        for criterion in self._audit.criterion_scores:
            for field_name in (
                self.score_field_name(criterion.criterion_id),
                self.reason_field_name(criterion.criterion_id),
            ):
                if cleaned.get(field_name) not in {None, ""}:
                    self.add_error(
                        field_name,
                        "Criterion overrides require the override decision.",
                    )

    def _overrides(self) -> list[CriterionOverride]:
        return [
            CriterionOverride(
                criterion_id=criterion.criterion_id,
                previous_score=criterion.score,
                new_score=float(
                    self.cleaned_data[
                        self.score_field_name(criterion.criterion_id)
                    ]
                ),
                reason=str(
                    self.cleaned_data[
                        self.reason_field_name(criterion.criterion_id)
                    ]
                ).strip(),
            )
            for criterion in self._audit.criterion_scores
        ]


def _identifier_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
