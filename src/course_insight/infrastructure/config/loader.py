"""Platform configuration assembly with explicit source precedence."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from course_insight.infrastructure.config.errors import ConfigurationError
from course_insight.infrastructure.config.models import PlatformSettings
from course_insight.infrastructure.config.sources import (
    deep_merge,
    environment_to_settings,
    load_app_json,
    load_dotenv,
    safe_defaults,
)


_DEFAULT_PATH = object()


def load_platform_settings(
    *,
    project_root: Path | str | None = None,
    app_json_path: Path | str | None | object = _DEFAULT_PATH,
    dotenv_path: Path | str | None = None,
    environment: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> PlatformSettings:
    """Load settings; dotenv use is always explicit and project-bounded."""

    root = Path(project_root or Path.cwd()).resolve()
    config_root = (root / "config").resolve()
    app_json_is_explicit = (
        app_json_path is not _DEFAULT_PATH and app_json_path is not None
    )
    dotenv_is_explicit = dotenv_path is not None
    selected_app_path = _select_path(
        app_json_path,
        default=config_root / "app.json",
        allowed_root=config_root,
        field="app_json",
    )
    selected_dotenv_path = _select_path(
        dotenv_path,
        default=root / ".env",
        allowed_root=root,
        field="dotenv",
        direct_child=True,
    )
    _require_explicit_source(
        selected_app_path,
        explicit=app_json_is_explicit,
        field="app_json",
    )
    _require_explicit_source(
        selected_dotenv_path,
        explicit=dotenv_is_explicit,
        field="dotenv",
    )
    environment_values = dict(os.environ if environment is None else environment)
    dotenv_values = load_dotenv(selected_dotenv_path)
    dotenv_settings = environment_to_settings(dotenv_values)
    environment_settings = environment_to_settings(environment_values)

    merged = safe_defaults()
    merged = deep_merge(merged, load_app_json(selected_app_path))
    merged = deep_merge(merged, dotenv_settings)
    merged = deep_merge(merged, environment_settings)
    merged = deep_merge(merged, dict(overrides or {}))
    _validate_group_structure(merged)
    merged = _resolve_indirect_values(
        merged,
        dotenv_values=dotenv_values,
        environment_values=environment_values,
        dotenv_settings=dotenv_settings,
        environment_settings=environment_settings,
        overrides=dict(overrides or {}),
    )
    merged = _resolve_paths(merged, root=root)

    try:
        return PlatformSettings.model_validate(merged)
    except ConfigurationError:
        raise
    except ValidationError as error:
        fields = {
            ".".join(str(part) for part in item["loc"]) or "configuration"
            for item in error.errors(include_input=False, include_url=False)
        }
        raise ConfigurationError(
            code="INVALID_CONFIGURATION",
            fields=fields,
            reason="validation_failed",
        ) from None


def _require_explicit_source(
    path: Path | None,
    *,
    explicit: bool,
    field: str,
) -> None:
    if explicit and (path is None or not path.is_file()):
        raise ConfigurationError(
            code="CONFIGURATION_SOURCE_MISSING",
            fields=(field,),
            reason="missing",
        )


def _validate_group_structure(merged: Mapping[str, Any]) -> None:
    for group in ("database", "logging", "outbox", "web", "security"):
        if not isinstance(merged.get(group), Mapping):
            raise ConfigurationError(
                code="INVALID_CONFIGURATION",
                fields=(group,),
                reason="object_required",
            )
    if "m6_policy" in merged and not isinstance(
        merged["m6_policy"],
        Mapping,
    ):
        raise ConfigurationError(
            code="INVALID_CONFIGURATION",
            fields=("m6_policy",),
            reason="object_required",
        )


def _select_path(
    supplied: Path | str | None | object,
    *,
    default: Path,
    allowed_root: Path,
    field: str,
    direct_child: bool = False,
) -> Path | None:
    if supplied is None:
        return None
    candidate = default if supplied is _DEFAULT_PATH else Path(supplied)
    if not candidate.is_absolute():
        base = allowed_root if direct_child else allowed_root.parent
        candidate = base / candidate
    resolved = candidate.resolve()
    allowed = allowed_root.resolve()
    if not resolved.is_relative_to(allowed):
        raise ConfigurationError(
            code="UNSAFE_CONFIGURATION_PATH",
            fields=(field,),
            reason="path_boundary",
        )
    if direct_child and resolved.parent != allowed:
        raise ConfigurationError(
            code="UNSAFE_CONFIGURATION_PATH",
            fields=(field,),
            reason="path_boundary",
        )
    return resolved


def _resolve_indirect_values(
    merged: dict[str, Any],
    *,
    dotenv_values: Mapping[str, str],
    environment_values: Mapping[str, str],
    dotenv_settings: Mapping[str, Any],
    environment_settings: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> dict[str, Any]:
    result = deep_merge({}, merged)
    _resolve_indirect_field(
        result,
        group="database",
        field="url",
        env_name_field="url_env",
        dotenv_values=dotenv_values,
        environment_values=environment_values,
        dotenv_settings=dotenv_settings,
        environment_settings=environment_settings,
        overrides=overrides,
    )
    _resolve_indirect_field(
        result,
        group="web",
        field="secret_key",
        env_name_field="secret_key_env",
        dotenv_values=dotenv_values,
        environment_values=environment_values,
        dotenv_settings=dotenv_settings,
        environment_settings=environment_settings,
        overrides=overrides,
    )
    _resolve_indirect_field(
        result,
        group="web",
        field="allowed_hosts",
        env_name_field="allowed_hosts_env",
        dotenv_values=dotenv_values,
        environment_values=environment_values,
        dotenv_settings=dotenv_settings,
        environment_settings=environment_settings,
        overrides=overrides,
        split_csv=True,
    )
    _resolve_indirect_field(
        result,
        group="web",
        field="csrf_trusted_origins",
        env_name_field="csrf_trusted_origins_env",
        dotenv_values=dotenv_values,
        environment_values=environment_values,
        dotenv_settings=dotenv_settings,
        environment_settings=environment_settings,
        overrides=overrides,
        split_csv=True,
    )
    return result


def _resolve_indirect_field(
    result: dict[str, Any],
    *,
    group: str,
    field: str,
    env_name_field: str,
    dotenv_values: Mapping[str, str],
    environment_values: Mapping[str, str],
    dotenv_settings: Mapping[str, Any],
    environment_settings: Mapping[str, Any],
    overrides: Mapping[str, Any],
    split_csv: bool = False,
) -> None:
    group_values = result[group]
    env_name = group_values[env_name_field]
    dotenv_group = dotenv_settings.get(group)
    environment_group = environment_settings.get(group)
    dotenv_has_direct = (
        isinstance(dotenv_group, Mapping) and field in dotenv_group
    )
    environment_has_direct = (
        isinstance(environment_group, Mapping) and field in environment_group
    )
    if (
        not dotenv_has_direct
        and not environment_has_direct
        and env_name in dotenv_values
    ):
        group_values[field] = _split_if_needed(
            dotenv_values[env_name],
            split_csv=split_csv,
        )
    if not environment_has_direct and env_name in environment_values:
        group_values[field] = _split_if_needed(
            environment_values[env_name],
            split_csv=split_csv,
        )
    explicit_group = overrides.get(group)
    if isinstance(explicit_group, Mapping) and field in explicit_group:
        group_values[field] = explicit_group[field]


def _split_if_needed(value: str, *, split_csv: bool) -> str | list[str]:
    if not split_csv:
        return value
    if not value:
        return []
    return [part.strip() for part in value.split(",")]


def _resolve_paths(
    merged: dict[str, Any],
    *,
    root: Path,
) -> dict[str, Any]:
    result = deep_merge({}, merged)
    runtime = _bounded_path(
        result["runtime_dir"],
        base=root,
        allowed_root=root,
        field="runtime_dir",
        require_descendant=True,
    )
    config = _bounded_path(
        result["config_dir"],
        base=root,
        allowed_root=root,
        field="config_dir",
        require_descendant=True,
    )
    result["runtime_dir"] = runtime
    result["config_dir"] = config
    result["database"]["sqlite_path"] = _runtime_path(
        result["database"]["sqlite_path"],
        root=root,
        runtime=runtime,
        field="database.sqlite_path",
    )
    result["logging"]["directory"] = _runtime_path(
        result["logging"]["directory"],
        root=root,
        runtime=runtime,
        field="logging.directory",
    )
    policy = result.get("m6_policy")
    if isinstance(policy, dict):
        policy["runtime_directory"] = _runtime_path(
            policy.get("runtime_directory", "m6_policy"),
            root=root,
            runtime=runtime,
            field="m6_policy.runtime_directory",
        )
    return result


def _runtime_path(
    value: Any,
    *,
    root: Path,
    runtime: Path,
    field: str,
) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError):
        raise ConfigurationError(
            code="INVALID_CONFIGURATION",
            fields=(field,),
            reason="path_type",
        ) from None
    if path.is_absolute():
        candidate = path
    else:
        project_candidate = (root / path).resolve()
        candidate = (
            project_candidate
            if project_candidate.is_relative_to(runtime)
            else runtime / path
        )
    return _bounded_path(
        candidate,
        base=runtime,
        allowed_root=runtime,
        field=field,
    )


def _bounded_path(
    value: Any,
    *,
    base: Path,
    allowed_root: Path,
    field: str,
    require_descendant: bool = False,
) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError):
        raise ConfigurationError(
            code="INVALID_CONFIGURATION",
            fields=(field,),
            reason="path_type",
        ) from None
    candidate = path if path.is_absolute() else base / path
    resolved = candidate.resolve()
    boundary = allowed_root.resolve()
    if (
        not resolved.is_relative_to(boundary)
        or (require_descendant and resolved == boundary)
    ):
        raise ConfigurationError(
            code="UNSAFE_CONFIGURATION_PATH",
            fields=(field,),
            reason="path_boundary",
        )
    return resolved
