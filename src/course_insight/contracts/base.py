from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence, Set
from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, ClassVar, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    ModelWrapValidatorHandler,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import to_jsonable_python

from course_insight.contracts.errors import DomainError


_DEFAULT_CHECKSUM_EXCLUDED_FIELDS = frozenset({"checksum", "immutable_checksum"})
_MAPPING_KEY_ESCAPE = "\x00"
_MAPPING_COLLISION_TAG = f"{_MAPPING_KEY_ESCAPE}m"


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _canonical_mapping_key(value: Any) -> str:
    normalized = _canonical_checksum_value(value)
    if isinstance(value, str):
        if value.startswith(_MAPPING_KEY_ESCAPE):
            return f"{_MAPPING_KEY_ESCAPE}s:{value}"
        return value
    value_type = f"{type(value).__module__}.{type(value).__qualname__}"
    return f"{_MAPPING_KEY_ESCAPE}k:{value_type}:{_stable_json(normalized)}"


def _canonical_mapping(value: Mapping[Any, Any]) -> dict[str, Any]:
    entries = [
        (
            _canonical_mapping_key(key),
            _canonical_checksum_value(nested_value),
        )
        for key, nested_value in value.items()
    ]
    if len({key for key, _ in entries}) == len(entries):
        return dict(entries)

    collision_entries = [[key, nested_value] for key, nested_value in entries]
    return {
        _MAPPING_COLLISION_TAG: sorted(
            collision_entries,
            key=_stable_json,
        )
    }


def _canonical_checksum_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return _canonical_checksum_value(value.value)

    if isinstance(value, BaseModel):
        return _canonical_checksum_value(value.model_dump(mode="python"))

    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_checksum_value(getattr(value, field.name))
            for field in dataclass_fields(value)
        }

    if isinstance(value, Mapping):
        return _canonical_mapping(value)

    if isinstance(value, Set):
        normalized_items = [_canonical_checksum_value(item) for item in value]
        return sorted(normalized_items, key=_stable_json)

    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_canonical_checksum_value(item) for item in value]

    return to_jsonable_python(value)


def _validate_aware_datetimes(
    value: Any,
    *,
    path: str,
    seen: set[int],
) -> None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise DomainError(
                code="TIMEZONE_REQUIRED",
                module="contracts",
                message="datetime values must include timezone information",
                details={"path": path},
            )
        return

    if isinstance(value, (str, bytes, bytearray)):
        return

    if isinstance(value, BaseModel):
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        for field_name in type(value).model_fields:
            _validate_aware_datetimes(
                getattr(value, field_name),
                path=f"{path}.{field_name}",
                seen=seen,
            )
        computed_exclusions = getattr(
            type(value),
            "checksum_excluded_fields",
            _DEFAULT_CHECKSUM_EXCLUDED_FIELDS,
        )
        for field_name in type(value).model_computed_fields:
            if field_name in computed_exclusions:
                continue
            _validate_aware_datetimes(
                getattr(value, field_name),
                path=f"{path}.{field_name}",
                seen=seen,
            )
        return

    if is_dataclass(value) and not isinstance(value, type):
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        for field in dataclass_fields(value):
            _validate_aware_datetimes(
                getattr(value, field.name),
                path=f"{path}.{field.name}",
                seen=seen,
            )
        return

    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        for key, nested_value in value.items():
            _validate_aware_datetimes(
                key,
                path=f"{path}.<key>",
                seen=seen,
            )
            _validate_aware_datetimes(
                nested_value,
                path=f"{path}[{key!r}]",
                seen=seen,
            )
        return

    if isinstance(value, (Sequence, Set)):
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        for index, nested_value in enumerate(value):
            _validate_aware_datetimes(
                nested_value,
                path=f"{path}[{index}]",
                seen=seen,
            )


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        validate_default=True,
    )

    checksum_excluded_fields: ClassVar[frozenset[str]] = (
        _DEFAULT_CHECKSUM_EXCLUDED_FIELDS
    )

    schema_version: str = "1.0.0"

    @field_validator("*", mode="after", check_fields=False)
    @classmethod
    def _validate_field_datetimes(cls, value: Any, info: ValidationInfo) -> Any:
        _validate_aware_datetimes(
            value,
            path=f"$.{info.field_name}",
            seen=set(),
        )
        return value

    @model_validator(mode="wrap")
    @classmethod
    def _validate_contract(
        cls,
        value: Any,
        handler: ModelWrapValidatorHandler[Self],
    ) -> Self:
        original_state: tuple[dict[str, Any], set[str]] | None = None
        if isinstance(value, cls):
            original_state = (
                dict(value.__dict__),
                set(value.__pydantic_fields_set__),
            )

        try:
            validated = handler(value)
            _validate_aware_datetimes(validated, path="$", seen=set())
            validated.validate_business_rules()
        except Exception:
            if original_state is not None:
                original_values, original_fields_set = original_state
                object.__setattr__(value, "__dict__", original_values)
                object.__setattr__(
                    value,
                    "__pydantic_fields_set__",
                    original_fields_set,
                )
            raise

        return validated

    def validate_business_rules(self) -> None:
        """Validate rules that are specific to a contract subclass."""

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            ensure_ascii=False,
            indent=indent,
        )

    def to_json_file(self, path: Path) -> None:
        target_path = Path(path)
        temporary_path: Path | None = None

        try:
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target_path.parent,
                prefix=f".{target_path.name}.",
                suffix=".tmp",
                delete=False,
                newline="",
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(self.to_json())
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            os.replace(temporary_path, target_path)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

    def content_checksum(self) -> str:
        payload = self.model_dump(
            mode="python",
            exclude=self.checksum_excluded_fields,
        )
        serialized = _stable_json(_canonical_checksum_value(payload))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls.model_validate(data)

    @classmethod
    def from_json_file(cls, path: Path) -> Self:
        with path.open(encoding="utf-8") as source:
            data = json.load(source)
        return cls.from_dict(data)
