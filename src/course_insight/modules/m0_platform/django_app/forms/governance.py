"""Bounded forms for account and teacher-owned class governance."""

from __future__ import annotations

from django import forms


class ManagedAccountCreationForm(forms.Form):
    account_name = forms.CharField(
        label="账户名",
        strip=False,
    )
    password1 = forms.CharField(
        label="密码",
        strip=False,
        widget=forms.PasswordInput,
    )
    password2 = forms.CharField(
        label="再次输入密码",
        strip=False,
        widget=forms.PasswordInput,
    )

    def clean(self) -> dict[str, object]:
        cleaned = super().clean()
        account_name = cleaned.get("account_name")
        password1 = cleaned.get("password1")
        password2 = cleaned.get("password2")
        if isinstance(account_name, str) and not account_name.strip():
            self.add_error("account_name", "账户名不能为空或全为空白")
        if isinstance(password1, str) and not password1.strip():
            self.add_error("password1", "密码不能为空或全为空白")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", "两次输入的密码不一致")
        return cleaned


class AccountDeletionConfirmationForm(forms.Form):
    confirmed_account_name = forms.CharField(
        label="再次输入要注销的账户名",
        strip=False,
    )


class OpenClassForm(forms.Form):
    course_name = forms.CharField(label="课程名称", min_length=1, max_length=255)
    class_name = forms.CharField(label="班级名称", min_length=1, max_length=255)
    request_token = forms.CharField(widget=forms.HiddenInput, min_length=16, max_length=128)


class ClassMemberForm(forms.Form):
    student_account = forms.CharField(
        label="学生账户名",
        strip=False,
    )

    def clean_student_account(self) -> str:
        account_name = self.cleaned_data["student_account"]
        if not account_name.strip():
            raise forms.ValidationError("学生账户名不能为空或全为空白")
        return account_name
