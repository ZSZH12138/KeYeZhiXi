"""Public local-infrastructure helpers."""

from course_insight.infrastructure.deepseek import (
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_CHAT_COMPLETIONS_URL,
    DeepSeekClient,
    DeepSeekClientPolicy,
    EmptyDeepSeekAdapter,
    SUPPORTED_DEEPSEEK_MODELS,
)
from course_insight.infrastructure.json_io import dumps_json, read_json, write_json
from course_insight.infrastructure.logging import append_json_log

__all__ = [
    "DEEPSEEK_API_KEY_ENV",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_CHAT_COMPLETIONS_URL",
    "DeepSeekClient",
    "DeepSeekClientPolicy",
    "EmptyDeepSeekAdapter",
    "SUPPORTED_DEEPSEEK_MODELS",
    "append_json_log",
    "dumps_json",
    "read_json",
    "write_json",
]
