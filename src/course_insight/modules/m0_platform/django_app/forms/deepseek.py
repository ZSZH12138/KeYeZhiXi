"""Teacher-facing DeepSeek API configuration form.

The raw key is write-only.  The page never redisplays a previously stored
secret; teachers paste a new value or clear the stored file key.
"""

from __future__ import annotations

from django import forms

from course_insight.infrastructure.deepseek import SUPPORTED_DEEPSEEK_MODELS


class DeepSeekSettingsForm(forms.Form):
    """Collect the DeepSeek key and governed generation switches."""

    api_key = forms.CharField(
        label="DeepSeek API 密钥",
        required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}),
        help_text="密钥只用于调用官方接口，页面不会回显已保存的完整密钥。",
        max_length=256,
    )
    model_name = forms.ChoiceField(
        label="模型",
        choices=tuple((name, name) for name in sorted(SUPPORTED_DEEPSEEK_MODELS)),
    )
    thinking_enabled = forms.BooleanField(
        label="启用 thinking",
        required=False,
        help_text="对应 DeepSeek 官方 thinking 开关，默认关闭。",
    )
    clear_stored_key = forms.BooleanField(
        label="清除本机已保存的密钥",
        required=False,
        help_text="不影响已经写在服务器环境变量中的 DEEPSEEK_API_KEY。",
    )
