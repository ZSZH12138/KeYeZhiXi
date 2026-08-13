"""Strict, deterministic parsing and diffing for role seed CSV files."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from course_insight.infrastructure.config.errors import RoleSeedError


RoleName = Literal["student", "teacher", "course_admin", "system_admin"]
SyncMode = Literal["check", "dry-run", "apply"]
_ROLES = frozenset({"student", "teacher", "course_admin", "system_admin"})
_HEADER = ("actor_id", "role", "course_id", "class_id", "is_active")
_PSEUDONYM = re.compile(r"^pseudonym_[a-z0-9][a-z0-9_-]{0,116}$")
_SCOPE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True, slots=True, order=True)
class RoleGrantSeed:
    actor_id: str
    role: RoleName
    course_id: str | None
    class_id: str | None
    is_active: bool

    @property
    def identity(self) -> tuple[str, str, str | None, str | None]:
        return (self.actor_id, self.role, self.course_id, self.class_id)


@dataclass(frozen=True, slots=True)
class RoleSeedDocument:
    grants: tuple[RoleGrantSeed, ...]
    checksum: str


@dataclass(frozen=True, slots=True)
class RoleSyncPlan:
    mode: SyncMode
    source_checksum: str
    create: tuple[RoleGrantSeed, ...]
    activate: tuple[RoleGrantSeed, ...]
    revoke: tuple[RoleGrantSeed, ...]
    unchanged: tuple[RoleGrantSeed, ...]

    @property
    def has_changes(self) -> bool:
        return bool(self.create or self.activate or self.revoke)


def load_role_seeds(
    path: Path | str,
    *,
    config_root: Path | str,
) -> RoleSeedDocument:
    boundary = Path(config_root).resolve()
    supplied_path = Path(path)
    source_path = (
        supplied_path
        if supplied_path.is_absolute()
        else boundary / supplied_path
    ).resolve()
    if not source_path.is_relative_to(boundary):
        raise RoleSeedError(
            fields=("roles.path",),
            reason="path_boundary",
        )
    try:
        with source_path.open(
            encoding="utf-8-sig",
            newline="",
        ) as source:
            reader = csv.DictReader(source)
            if tuple(reader.fieldnames or ()) != _HEADER:
                raise RoleSeedError(
                    fields=("roles.header",),
                    reason="invalid_header",
                )
            parsed = tuple(
                _parse_row(row, line_number=reader.line_num)
                for row in reader
            )
    except RoleSeedError:
        raise
    except (OSError, UnicodeError, csv.Error):
        raise RoleSeedError(
            fields=("roles",),
            reason="unreadable",
        ) from None

    unique: dict[
        tuple[str, str, str | None, str | None],
        RoleGrantSeed,
    ] = {}
    for grant in parsed:
        previous = unique.get(grant.identity)
        if previous is not None and previous.is_active != grant.is_active:
            raise RoleSeedError(
                fields=("roles.is_active",),
                reason="conflicting_status",
            )
        unique[grant.identity] = grant

    grants = tuple(sorted(unique.values()))
    _validate_active_actor_conflicts(grants)
    checksum = _checksum(grants)
    return RoleSeedDocument(grants=grants, checksum=checksum)


def build_role_sync_plan(
    desired: RoleSeedDocument,
    existing: tuple[RoleGrantSeed, ...] = (),
    *,
    mode: SyncMode = "check",
) -> RoleSyncPlan:
    if mode not in {"check", "dry-run", "apply"}:
        raise RoleSeedError(
            fields=("mode",),
            reason="invalid_mode",
        )
    _validate_existing_grants(existing)
    current_by_key = {grant.identity: grant for grant in existing}
    desired_identities = {grant.identity for grant in desired.grants}
    create: list[RoleGrantSeed] = []
    activate: list[RoleGrantSeed] = []
    revoke: list[RoleGrantSeed] = []
    unchanged: list[RoleGrantSeed] = []
    for grant in desired.grants:
        current = current_by_key.get(grant.identity)
        if current is None:
            if grant.is_active:
                create.append(grant)
            else:
                revoke.append(grant)
        elif current.is_active == grant.is_active:
            unchanged.append(grant)
        elif grant.is_active:
            activate.append(grant)
        else:
            revoke.append(grant)
    for identity, current in current_by_key.items():
        if identity not in desired_identities and current.is_active:
            revoke.append(current)
    return RoleSyncPlan(
        mode=mode,
        source_checksum=desired.checksum,
        create=tuple(create),
        activate=tuple(activate),
        revoke=tuple(revoke),
        unchanged=tuple(unchanged),
    )


def _validate_existing_grants(
    existing: tuple[RoleGrantSeed, ...],
) -> None:
    seen: dict[
        tuple[str, str, str | None, str | None],
        RoleGrantSeed,
    ] = {}
    for grant in existing:
        _validate_grant(grant, field_prefix="existing")
        previous = seen.get(grant.identity)
        if previous is not None and previous.is_active != grant.is_active:
            raise RoleSeedError(
                fields=("existing.is_active",),
                reason="conflicting_status",
            )
        seen[grant.identity] = grant
    _validate_active_actor_conflicts(tuple(seen.values()))


def _validate_grant(
    grant: RoleGrantSeed,
    *,
    field_prefix: str,
) -> None:
    if not _PSEUDONYM.fullmatch(grant.actor_id):
        raise RoleSeedError(
            fields=(f"{field_prefix}.actor_id",),
            reason="non_pseudonymous_actor",
        )
    if grant.role not in _ROLES:
        raise RoleSeedError(
            fields=(f"{field_prefix}.role",),
            reason="invalid_role",
        )
    if (
        grant.course_id is not None
        and not _SCOPE_ID.fullmatch(grant.course_id)
    ):
        raise RoleSeedError(
            fields=(f"{field_prefix}.course_id",),
            reason="invalid_scope",
        )
    if (
        grant.class_id is not None
        and not _SCOPE_ID.fullmatch(grant.class_id)
    ):
        raise RoleSeedError(
            fields=(f"{field_prefix}.class_id",),
            reason="invalid_scope",
        )
    if not isinstance(grant.is_active, bool):
        raise RoleSeedError(
            fields=(f"{field_prefix}.is_active",),
            reason="invalid_boolean",
        )
    _validate_scope(grant, field=f"{field_prefix}.scope")


def _parse_row(
    row: dict[str, str | None],
    *,
    line_number: int,
) -> RoleGrantSeed:
    if None in row or any(value is None for value in row.values()):
        raise RoleSeedError(
            fields=(f"roles.line_{line_number}",),
            reason="invalid_column_count",
        )
    values = {key: value or "" for key, value in row.items()}
    if any(value != value.strip() for value in values.values()):
        raise RoleSeedError(
            fields=(f"roles.line_{line_number}",),
            reason="padded_value",
        )
    actor_id = values["actor_id"]
    role = values["role"]
    course_id = values["course_id"] or None
    class_id = values["class_id"] or None
    active_text = values["is_active"]
    if not _PSEUDONYM.fullmatch(actor_id):
        raise RoleSeedError(
            fields=(f"roles.line_{line_number}.actor_id",),
            reason="non_pseudonymous_actor",
        )
    if role not in _ROLES:
        raise RoleSeedError(
            fields=(f"roles.line_{line_number}.role",),
            reason="invalid_role",
        )
    if course_id is not None and not _SCOPE_ID.fullmatch(course_id):
        raise RoleSeedError(
            fields=(f"roles.line_{line_number}.course_id",),
            reason="invalid_scope",
        )
    if class_id is not None and not _SCOPE_ID.fullmatch(class_id):
        raise RoleSeedError(
            fields=(f"roles.line_{line_number}.class_id",),
            reason="invalid_scope",
        )
    if active_text not in {"true", "false"}:
        raise RoleSeedError(
            fields=(f"roles.line_{line_number}.is_active",),
            reason="invalid_boolean",
        )
    grant = RoleGrantSeed(
        actor_id=actor_id,
        role=role,  # type: ignore[arg-type]
        course_id=course_id,
        class_id=class_id,
        is_active=active_text == "true",
    )
    _validate_scope(grant, field=f"roles.line_{line_number}.scope")
    return grant


def _validate_scope(grant: RoleGrantSeed, *, field: str) -> None:
    if grant.role in {"student", "teacher"}:
        valid = grant.course_id is not None and grant.class_id is not None
    elif grant.role == "course_admin":
        valid = grant.course_id is not None and grant.class_id is None
    else:
        valid = grant.course_id is None and grant.class_id is None
    if not valid:
        raise RoleSeedError(fields=(field,), reason="invalid_scope")


def _validate_active_actor_conflicts(
    grants: tuple[RoleGrantSeed, ...],
) -> None:
    roles_by_actor: dict[str, set[str]] = {}
    student_scopes: dict[str, set[tuple[str | None, str | None]]] = {}
    for grant in grants:
        if not grant.is_active:
            continue
        roles_by_actor.setdefault(grant.actor_id, set()).add(grant.role)
        if grant.role == "student":
            student_scopes.setdefault(grant.actor_id, set()).add(
                (grant.course_id, grant.class_id)
            )
    if any(len(roles) > 1 for roles in roles_by_actor.values()):
        raise RoleSeedError(
            fields=("roles.role",),
            reason="conflicting_role",
        )
    if any(len(scopes) > 1 for scopes in student_scopes.values()):
        raise RoleSeedError(
            fields=("roles.scope",),
            reason="conflicting_scope",
        )


def _checksum(grants: tuple[RoleGrantSeed, ...]) -> str:
    canonical = [
        {
            "actor_id": grant.actor_id,
            "class_id": grant.class_id,
            "course_id": grant.course_id,
            "is_active": grant.is_active,
            "role": grant.role,
        }
        for grant in grants
    ]
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
