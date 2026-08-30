"""Authentication fields for unrestricted administrator-issued account names."""

from __future__ import annotations

from django import forms
from django.contrib.auth.forms import AuthenticationForm


class AccountAuthenticationForm(AuthenticationForm):
    """Preserve exact credentials while rejecting all-whitespace values."""

    username = forms.CharField(label="账户名", strip=False)
    password = forms.CharField(
        label="密码",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        # AuthenticationForm copies USERNAME_FIELD.max_length to this field.
        # USERNAME_FIELD is our fixed-width digest, not the externally entered
        # account name, so carrying that value into HTML would be incorrect.
        account_name = self.fields["username"]
        account_name.max_length = None
        account_name.widget.attrs.pop("maxlength", None)

    def clean_username(self) -> str:
        account_name = self.cleaned_data["username"]
        if not account_name.strip():
            raise forms.ValidationError("账户名不能为空或全为空白")
        return account_name

    def clean_password(self) -> str:
        password = self.cleaned_data["password"]
        if not password.strip():
            raise forms.ValidationError("密码不能为空或全为空白")
        return password
