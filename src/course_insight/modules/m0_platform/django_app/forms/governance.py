"""Bounded forms for account and teacher-owned class governance."""

from __future__ import annotations

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator

from course_insight.modules.m0_platform.django_app.models import (
    PSEUDONYMOUS_ACTOR_PATTERN,
)


class ManagedAccountCreationForm(forms.Form):
    actor_id = forms.CharField(
        label="账户名",
        min_length=12,
        max_length=128,
        validators=[RegexValidator(PSEUDONYMOUS_ACTOR_PATTERN, "账户名格式无效")],
    )
    password1 = forms.CharField(label="密码", widget=forms.PasswordInput)
    password2 = forms.CharField(label="再次输入密码", widget=forms.PasswordInput)

    def clean(self) -> dict[str, object]:
        cleaned = super().clean()
        password1 = cleaned.get("password1")
        password2 = cleaned.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error("password2", "两次输入的密码不一致")
        if password1:
            try:
                validate_password(str(password1))
            except ValidationError as error:
                self.add_error("password1", error)
        return cleaned


class AccountDeletionConfirmationForm(forms.Form):
    confirmed_actor_id = forms.CharField(label="再次输入要注销的账户名", max_length=128)


class OpenClassForm(forms.Form):
    course_name = forms.CharField(label="课程名称", min_length=1, max_length=255)
    class_name = forms.CharField(label="班级名称", min_length=1, max_length=255)
    request_token = forms.CharField(widget=forms.HiddenInput, min_length=16, max_length=128)


class ClassMemberForm(forms.Form):
    student_account = forms.CharField(
        label="学生账户名",
        max_length=128,
        validators=[RegexValidator(PSEUDONYMOUS_ACTOR_PATTERN, "学生账户名格式无效")],
    )
