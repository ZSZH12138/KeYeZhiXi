from __future__ import annotations

from django import forms


class KnowledgeReviewLookupForm(forms.Form):
    course_id = forms.CharField(max_length=128)
    class_id = forms.CharField(max_length=128)
    review_id = forms.CharField(max_length=128)

