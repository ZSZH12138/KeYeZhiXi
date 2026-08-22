"""Teacher-managed DeepSeek settings stored outside git.

The environment variable ``DEEPSEEK_API_KEY`` always wins over the runtime
file.  The file never leaves ``runtime/secrets/`` and callers must not log,
print, or return the raw key.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from course_insight.infrastructure.deepseek import (
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_BASE_URL,
    SUPPORTED_DEEPSEEK_MODELS,
)


_SECRETS_RELATIVE = Path("secrets") / "deepseek.json"
_MAX_SECRET_BYTES = 8 * 1024
_MAX_KEY_LENGTH = 256
KeySource = Literal["env", "file", "missing"]


@dataclass(frozen=True, slots=True)
class DeepSeekPublicStatus:
    """Safe-to-render DeepSeek configuration for the teacher UI."""

    configured: bool
    key_source: KeySource
    masked_key: str
    endpoint: str
    model_name: str
    thinking_enabled: bool
    env_overrides_file: bool
    privacy_gate_ready: bool
    privacy_gate_reason: str
    scoring_ready: bool


def inspect_m7_privacy_artifacts(runtime_dir: Path) -> tuple[bool, str]:
    """Describe whether pinned local privacy artifacts are present.

    This does not deserialize model bytes.  Application startup still has to
    construct ``build_required_m7_privacy_reviewer`` before any outbound call.
    """

    model_sha = os.environ.get(
        "COURSE_INSIGHT_M7_PRIVACY_MODEL_SHA256",
        "",
    ).strip()
    manifest_sha = os.environ.get(
        "COURSE_INSIGHT_M7_PRIVACY_MANIFEST_SHA256",
        "",
    ).strip()
    if len(model_sha) != 64 or len(manifest_sha) != 64:
        return False, "missing_pinned_checksums"
    if any(
        character not in "0123456789abcdef"
        for character in model_sha + manifest_sha
    ):
        return False, "invalid_pinned_checksums"
    model_dir = privacy_model_dir(runtime_dir)
    if not (model_dir / "manifest.json").is_file() or not (
        model_dir / "model.joblib"
    ).is_file():
        return False, "missing_privacy_artifacts"
    return True, "artifacts_present"


def privacy_model_dir(runtime_dir: Path) -> Path:
    """Return the administrator-controlled privacy artifact directory."""

    configured = os.environ.get(
        "COURSE_INSIGHT_M7_PRIVACY_MODEL_DIR",
        "",
    ).strip()
    root = Path(runtime_dir).resolve()
    if configured:
        candidate = Path(configured)
        model_dir = candidate if candidate.is_absolute() else root / candidate
    else:
        model_dir = root / "m7_privacy" / "privacy-model"
    return model_dir.resolve()


def secrets_path(runtime_dir: Path) -> Path:
    """Return the runtime file used for teacher-supplied DeepSeek settings."""

    return Path(runtime_dir).resolve() / _SECRETS_RELATIVE


def resolve_deepseek_api_key(runtime_dir: Path | None = None) -> str:
    """Return the active key, preferring the environment over the runtime file."""

    env_key = os.environ.get(DEEPSEEK_API_KEY_ENV, "").strip()
    if env_key:
        return env_key
    if runtime_dir is None:
        return ""
    stored = _read_secrets_file(runtime_dir)
    raw = stored.get("api_key", "")
    return raw.strip() if isinstance(raw, str) else ""


def public_deepseek_status(runtime_dir: Path) -> DeepSeekPublicStatus:
    """Describe configuration without exposing the secret."""

    env_key = os.environ.get(DEEPSEEK_API_KEY_ENV, "").strip()
    stored = _read_secrets_file(runtime_dir)
    file_key = stored.get("api_key", "")
    file_key = file_key.strip() if isinstance(file_key, str) else ""
    model_name = stored.get("model_name", "deepseek-v4-flash")
    if model_name not in SUPPORTED_DEEPSEEK_MODELS:
        model_name = "deepseek-v4-flash"
    thinking_enabled = stored.get("thinking_enabled", False) is True
    if env_key:
        source: KeySource = "env"
        key = env_key
    elif file_key:
        source = "file"
        key = file_key
    else:
        source = "missing"
        key = ""
    privacy_ready, privacy_reason = inspect_m7_privacy_artifacts(runtime_dir)
    return DeepSeekPublicStatus(
        configured=bool(key),
        key_source=source,
        masked_key=_mask_key(key),
        endpoint=DEEPSEEK_BASE_URL,
        model_name=str(model_name),
        thinking_enabled=thinking_enabled,
        env_overrides_file=bool(env_key),
        privacy_gate_ready=privacy_ready,
        privacy_gate_reason=privacy_reason,
        scoring_ready=bool(key) and privacy_ready,
    )


def save_teacher_deepseek_settings(
    runtime_dir: Path,
    *,
    api_key: str | None,
    model_name: str,
    thinking_enabled: bool,
    clear_key: bool = False,
) -> DeepSeekPublicStatus:
    """Persist teacher-edited settings without echoing the secret."""

    if model_name not in SUPPORTED_DEEPSEEK_MODELS:
        raise ValueError("unsupported DeepSeek model")
    if type(thinking_enabled) is not bool:
        raise ValueError("DeepSeek thinking mode must be boolean")
    current = _read_secrets_file(runtime_dir)
    if clear_key:
        current.pop("api_key", None)
    elif api_key is not None:
        cleaned = api_key.strip()
        if not cleaned:
            raise ValueError("DeepSeek API key must not be blank")
        if len(cleaned) > _MAX_KEY_LENGTH:
            raise ValueError("DeepSeek API key is too long")
        if any(character.isspace() for character in cleaned):
            raise ValueError("DeepSeek API key must not contain whitespace")
        current["api_key"] = cleaned
    current["model_name"] = model_name
    current["thinking_enabled"] = thinking_enabled
    current["endpoint"] = DEEPSEEK_BASE_URL
    path = secrets_path(runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(encoded + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return public_deepseek_status(runtime_dir)


def _read_secrets_file(runtime_dir: Path) -> dict[str, object]:
    path = secrets_path(runtime_dir)
    try:
        if not path.is_file() or path.stat().st_size > _MAX_SECRET_BYTES:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return payload if type(payload) is dict else {}


def _mask_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••"
    return f"{value[:4]}••••{value[-4:]}"


__all__ = [
    "DeepSeekPublicStatus",
    "inspect_m7_privacy_artifacts",
    "privacy_model_dir",
    "public_deepseek_status",
    "resolve_deepseek_api_key",
    "save_teacher_deepseek_settings",
    "secrets_path",
]
