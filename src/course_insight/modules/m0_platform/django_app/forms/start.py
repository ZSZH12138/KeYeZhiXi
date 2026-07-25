"""Bounded HTTP input for starting an assessment."""

from __future__ import annotations

from django import forms
from django.core.validators import RegexValidator


ASSESSMENT_TASK_TYPES = (
    ("diagnostic", "诊断测评"),
    ("practice", "练习"),
    ("correction", "订正"),
    ("stage_assessment", "阶段测评"),
)
_MAX_STUDENT_TEXT_LENGTH = 12_000
_SCOPE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_scope_validator = RegexValidator(
    regex=_SCOPE_ID_PATTERN,
    message="Invalid scope identifier.",
)


class AssessmentStartForm(forms.Form):
    """Validate learner intent while all identities remain server-owned."""

    student_text = forms.CharField(
        min_length=1,
        max_length=_MAX_STUDENT_TEXT_LENGTH,
        strip=True,
        widget=forms.Textarea,
    )
    task_type_hint = forms.ChoiceField(choices=ASSESSMENT_TASK_TYPES)
    flow_token = forms.CharField(
        min_length=1,
        max_length=2_048,
        strip=True,
        widget=forms.HiddenInput,
    )

    def clean(self) -> dict[str, object]:
        cleaned = super().clean()
        if self.is_bound:
            allowed = {*self.fields, "csrfmiddlewaretoken"}
            if set(self.data) - allowed:
                raise forms.ValidationError("Unexpected form field.")
            getlist = getattr(self.data, "getlist", None)
            if callable(getlist):
                for field_name in self.fields:
                    if len(getlist(field_name)) != 1:
                        self.add_error(
                            field_name,
                            "Submit each field exactly once.",
                        )
        return cleaned


class ScopeSelectionForm(forms.Form):
    """Validate an explicit target before exact-grant authorization."""

    course_id = forms.CharField(
        min_length=1,
        max_length=128,
        strip=True,
        validators=[_scope_validator],
    )
    class_id = forms.CharField(
        min_length=1,
        max_length=128,
        strip=True,
        validators=[_scope_validator],
    )

    def clean(self) -> dict[str, object]:
        cleaned = super().clean()
        if self.is_bound:
            if set(self.data) - set(self.fields):
                raise forms.ValidationError("Unexpected form field.")
            getlist = getattr(self.data, "getlist", None)
            if callable(getlist):
                for field_name in self.fields:
                    if len(getlist(field_name)) != 1:
                        self.add_error(
                            field_name,
                            "Submit each field exactly once.",
                        )
        return cleaned


class ReviewLookupForm(ScopeSelectionForm):
    """Validate teacher-owned scope plus an opaque paper identity."""

    paper_id = forms.CharField(
        min_length=1,
        max_length=128,
        strip=True,
        validators=[_scope_validator],
    )
