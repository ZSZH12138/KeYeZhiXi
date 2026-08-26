"""Dynamic, paper-bound assessment submission form."""

from __future__ import annotations

import hashlib
import math
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

from django import forms

from course_insight.contracts.assessment import AssessmentPaper, ItemInstance
from course_insight.contracts.platform import AssessmentSubmission


_MAX_ANSWER_LENGTH = 12_000
_ANSWER_TYPES = frozenset({"str", "bool", "int", "float"})


class AssessmentSubmissionForm(forms.Form):
    """Accept only the exact answer set frozen into one server-owned paper."""

    def __init__(
        self,
        *,
        paper: AssessmentPaper,
        learner_id: str,
        data=None,
        **kwargs,
    ) -> None:
        if paper.immutable_checksum != paper.freeze():
            raise ValueError("assessment paper checksum is invalid")
        if learner_id != paper.learner_id:
            raise ValueError("assessment learner scope does not match")
        self._paper = paper.model_copy(deep=True)
        self._learner_id = learner_id
        super().__init__(data=data, **kwargs)
        field_to_item: dict[str, str] = {}
        for item in self._paper.all_items():
            field_name = self.answer_field_name(item.item_instance_id)
            if field_name in field_to_item:
                raise ValueError("assessment answer field identity collided")
            self.fields[field_name] = self._field_for(item)
            field_to_item[field_name] = item.item_instance_id
        self._field_to_item: Mapping[str, str] = MappingProxyType(
            field_to_item
        )

    @staticmethod
    def answer_field_name(item_instance_id: str) -> str:
        digest = hashlib.sha256(
            item_instance_id.encode("utf-8")
        ).hexdigest()[:24]
        return f"answer_{digest}"

    def clean(self) -> dict[str, object]:
        cleaned = super().clean()
        if self.is_bound:
            allowed = {*self.fields, "csrfmiddlewaretoken"}
            if set(self.data) - allowed:
                raise forms.ValidationError("Unexpected form field.")
            getlist = getattr(self.data, "getlist", None)
            if callable(getlist) and any(
                len(getlist(field_name)) > 1
                for field_name in self.fields
            ):
                raise forms.ValidationError("Duplicate form field.")
        for field_name, value in tuple(cleaned.items()):
            if type(value) is float and not math.isfinite(value):
                self.add_error(field_name, "Enter a finite number.")
            if isinstance(value, str):
                cleaned[field_name] = value.strip()
                if not cleaned[field_name]:
                    self.add_error(field_name, "This field is required.")
        return cleaned

    def to_submission(
        self,
        *,
        submission_id: str,
        attempt_id: str,
        submitted_at: datetime,
    ) -> AssessmentSubmission:
        if not self.is_valid():
            raise ValueError("assessment form is invalid")
        answers = {
            item_id: self.cleaned_data[field_name]
            for field_name, item_id in self._field_to_item.items()
        }
        submission = AssessmentSubmission(
            submission_id=submission_id,
            attempt_id=attempt_id,
            paper_id=self._paper.paper_id,
            learner_id=self._learner_id,
            answers=answers,
            submitted_at=submitted_at,
        )
        submission.validate_business_rules()
        return submission.model_copy(deep=True)

    @staticmethod
    def _field_for(item: ItemInstance) -> forms.Field:
        choice_options = item.parameters.get("_choice_options")
        if isinstance(choice_options, dict) and choice_options:
            choices = tuple(
                (label, f"{label}. {text}")
                for label, text in sorted(choice_options.items())
            )
            return forms.ChoiceField(
                choices=choices,
                required=True,
                label=item.stem,
                widget=forms.RadioSelect,
            )
        answer_type = (
            "str"
            if item.is_subjective()
            else item.parameters.get("answer_type", "str")
        )
        if answer_type not in _ANSWER_TYPES:
            raise ValueError("assessment answer type is invalid")
        common = {
            "required": True,
            "label": item.stem,
        }
        if answer_type == "bool":
            return forms.TypedChoiceField(
                choices=(("true", "是"), ("false", "否")),
                coerce=lambda value: value == "true",
                **common,
            )
        if answer_type == "int":
            return forms.IntegerField(**common)
        if answer_type == "float":
            return forms.FloatField(**common)
        return forms.CharField(
            max_length=_MAX_ANSWER_LENGTH,
            strip=True,
            widget=forms.Textarea if item.is_subjective() else forms.TextInput,
            **common,
        )
