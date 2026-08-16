from __future__ import annotations

from django import forms


class KnowledgeReviewForm(forms.Form):
    _ACTION_LABELS = {
        "submit": "提交审核",
        "approve": "批准审核",
        "reject": "退回修改",
        "recall": "撤回结果",
    }

    def __init__(self, *args, allowed_actions: tuple[str, ...] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        actions = (
            tuple(self._ACTION_LABELS)
            if allowed_actions is None
            else allowed_actions
        )
        self.fields["action"].choices = tuple(
            (action, self._ACTION_LABELS[action])
            for action in actions
            if action in self._ACTION_LABELS
        )

    action = forms.ChoiceField(choices=(), required=True)
    reason = forms.CharField(
        required=True,
        max_length=2_000,
        strip=True,
        widget=forms.Textarea(attrs={"rows": 4}),
    )
