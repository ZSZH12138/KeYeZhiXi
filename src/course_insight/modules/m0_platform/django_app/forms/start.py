"""Bounded HTTP input for starting an assessment."""

from __future__ import annotations

from collections.abc import Iterable

from django import forms
from django.core.validators import RegexValidator


ASSESSMENT_TASK_TYPES = (
    ("diagnostic", "诊断测评"),
    ("practice", "练习"),
    ("correction", "订正"),
    ("stage_assessment", "阶段测评"),
)
_SCOPE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_scope_validator = RegexValidator(
    regex=_SCOPE_ID_PATTERN,
    message="Invalid scope identifier.",
)


class AssessmentStartForm(forms.Form):
    """Validate a bounded task choice while all free text stays server-owned."""

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
        label="课程",
        min_length=1,
        max_length=128,
        strip=True,
        validators=[_scope_validator],
    )
    class_id = forms.CharField(
        label="班级",
        min_length=1,
        max_length=128,
        strip=True,
        validators=[_scope_validator],
    )

    def __init__(
        self,
        *args: object,
        scope_choices: Iterable[tuple[str, str, str, str]] | None = None,
        hide_scope: bool = False,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        choices = None if scope_choices is None else tuple(scope_choices)
        if choices is not None:
            self.fields["course_id"].widget = forms.Select(
                choices=_unique_choices(
                    (course_id, course_name)
                    for course_id, course_name, _, _ in choices
                )
            )
            self.fields["class_id"].widget = forms.Select(
                choices=_unique_choices(
                    (
                        class_id,
                        f"{course_name} / {class_name}",
                    )
                    for _, course_name, class_id, class_name in choices
                )
            )
        if hide_scope:
            self.fields["course_id"].widget = forms.HiddenInput()
            self.fields["class_id"].widget = forms.HiddenInput()

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


class StudentWorkspaceSelectionForm(forms.Form):
    """Let students choose an invited class by its display label.

    The submitted value is an opaque workspace primary key, not a course or
    class name.  The view independently verifies the active membership before
    resolving it to the exact authorization scope.
    """

    workspace_id = forms.ChoiceField(label="课程和班级")

    def __init__(
        self,
        *args: object,
        workspace_choices: Iterable[tuple[str, str]],
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.fields["workspace_id"].choices = tuple(workspace_choices)

    def clean(self) -> dict[str, object]:
        cleaned = super().clean()
        if self.is_bound:
            if set(self.data) - set(self.fields):
                raise forms.ValidationError("Unexpected form field.")
            getlist = getattr(self.data, "getlist", None)
            if callable(getlist) and len(getlist("workspace_id")) != 1:
                self.add_error(
                    "workspace_id",
                    "Submit each field exactly once.",
                )
        return cleaned


class ReviewLookupForm(ScopeSelectionForm):
    """Validate teacher-owned scope plus one student account identity."""

    learner_account = forms.CharField(
        label="学生账号",
        strip=False,
    )

    def __init__(
        self,
        *args: object,
        learner_choices: Iterable[tuple[str, str]] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        if learner_choices is not None:
            self.fields["learner_account"] = forms.ChoiceField(
                label="学生账号",
                choices=tuple(learner_choices),
            )

    def clean_learner_account(self) -> str:
        account_name = self.cleaned_data["learner_account"]
        if not account_name.strip():
            raise forms.ValidationError("学生账户名不能为空或全为空白")
        return account_name


def _unique_choices(
    choices: Iterable[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """Keep the first readable label for each submitted opaque value."""

    unique: dict[str, str] = {}
    for value, label in choices:
        unique.setdefault(value, label)
    return tuple(unique.items())
