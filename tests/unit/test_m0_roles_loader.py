from __future__ import annotations

from pathlib import Path

import pytest

from course_insight.infrastructure.config import RoleSeedError
from course_insight.infrastructure.config import RoleGrantSeed
from course_insight.infrastructure.config import build_role_sync_plan
from course_insight.infrastructure.config import load_role_seeds


HEADER = "actor_id,role,course_id,class_id,is_active\n"


def _write_roles(path: Path, *rows: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")


def _load_role_seeds(path: Path):
    return load_role_seeds(path, config_root=path.parent)


def test_exact_duplicate_role_rows_merge_idempotently(tmp_path: Path) -> None:
    roles_csv = tmp_path / "config" / "roles.csv"
    row = "pseudonym_student_001,student,course_network,class_01,true"
    _write_roles(roles_csv, row, row)

    first = _load_role_seeds(roles_csv)
    second = _load_role_seeds(roles_csv)

    assert len(first.grants) == 1
    assert first == second
    assert first.checksum == second.checksum


def test_conflicting_active_roles_for_same_actor_fail_closed(
    tmp_path: Path,
) -> None:
    roles_csv = tmp_path / "config" / "roles.csv"
    _write_roles(
        roles_csv,
        "pseudonym_actor_001,student,course_network,class_01,true",
        "pseudonym_actor_001,teacher,course_network,class_01,true",
    )

    with pytest.raises(RoleSeedError, match="conflicting_role"):
        _load_role_seeds(roles_csv)


def test_checksum_is_canonical_across_row_order_and_line_endings(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "config" / "roles-a.csv"
    second_path = tmp_path / "config" / "roles-b.csv"
    student = "pseudonym_student_001,student,course_network,class_01,true"
    teacher = "pseudonym_teacher_001,teacher,course_network,class_01,true"
    _write_roles(first_path, student, teacher)
    second_path.parent.mkdir(parents=True, exist_ok=True)
    second_path.write_bytes(
        (HEADER + teacher + "\r\n" + student + "\r\n").encode("utf-8")
    )

    first = _load_role_seeds(first_path)
    second = _load_role_seeds(second_path)

    assert first == second
    assert first.checksum == second.checksum


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (
            "real.person@example.com,student,course_network,class_01,true",
            "non_pseudonymous_actor",
        ),
        (
            "pseudonym_actor_001,owner,course_network,class_01,true",
            "invalid_role",
        ),
        (
            "pseudonym_actor_001,student,course_network,,true",
            "invalid_scope",
        ),
        (
            "pseudonym_actor_001,teacher,,class_01,true",
            "invalid_scope",
        ),
        (
            "pseudonym_actor_001,course_admin,course_network,class_01,true",
            "invalid_scope",
        ),
        (
            "pseudonym_actor_001,system_admin,course_network,,true",
            "invalid_scope",
        ),
        (
            "pseudonym_actor_001,student,course_network,class_01,yes",
            "invalid_boolean",
        ),
    ],
)
def test_invalid_role_seed_rows_fail_closed(
    tmp_path: Path,
    row: str,
    reason: str,
) -> None:
    roles_csv = tmp_path / "config" / "roles.csv"
    _write_roles(roles_csv, row)

    with pytest.raises(RoleSeedError, match=reason):
        _load_role_seeds(roles_csv)


def test_same_grant_cannot_be_both_active_and_revoked(
    tmp_path: Path,
) -> None:
    roles_csv = tmp_path / "config" / "roles.csv"
    base = "pseudonym_student_001,student,course_network,class_01"
    _write_roles(roles_csv, f"{base},true", f"{base},false")

    with pytest.raises(RoleSeedError, match="conflicting_status"):
        _load_role_seeds(roles_csv)


def test_student_cannot_have_multiple_active_own_scopes(
    tmp_path: Path,
) -> None:
    roles_csv = tmp_path / "config" / "roles.csv"
    _write_roles(
        roles_csv,
        "pseudonym_student_001,student,course_network,class_01,true",
        "pseudonym_student_001,student,course_network,class_02,true",
    )

    with pytest.raises(RoleSeedError, match="conflicting_scope"):
        _load_role_seeds(roles_csv)


@pytest.mark.parametrize("mode", ["check", "dry-run", "apply"])
def test_role_sync_diff_is_deterministic_for_all_modes(
    tmp_path: Path,
    mode: str,
) -> None:
    roles_csv = tmp_path / "config" / "roles.csv"
    _write_roles(
        roles_csv,
        "pseudonym_student_001,student,course_network,class_01,true",
        "pseudonym_teacher_001,teacher,course_network,class_01,false",
        "pseudonym_admin_001,course_admin,course_network,,true",
    )
    desired = _load_role_seeds(roles_csv)
    existing = (
        RoleGrantSeed(
            actor_id="pseudonym_student_001",
            role="student",
            course_id="course_network",
            class_id="class_01",
            is_active=False,
        ),
        RoleGrantSeed(
            actor_id="pseudonym_teacher_001",
            role="teacher",
            course_id="course_network",
            class_id="class_01",
            is_active=True,
        ),
    )

    first = build_role_sync_plan(desired, existing, mode=mode)
    second = build_role_sync_plan(desired, existing, mode=mode)

    assert first == second
    assert first.source_checksum == desired.checksum
    assert [grant.actor_id for grant in first.create] == [
        "pseudonym_admin_001"
    ]
    assert [grant.actor_id for grant in first.activate] == [
        "pseudonym_student_001"
    ]
    assert [grant.actor_id for grant in first.revoke] == [
        "pseudonym_teacher_001"
    ]
    assert first.has_changes is True


def test_repeating_sync_after_applying_plan_is_idempotent(
    tmp_path: Path,
) -> None:
    roles_csv = tmp_path / "config" / "roles.csv"
    _write_roles(
        roles_csv,
        "pseudonym_student_001,student,course_network,class_01,true",
        "pseudonym_teacher_001,teacher,course_network,class_01,false",
    )
    desired = _load_role_seeds(roles_csv)

    plan = build_role_sync_plan(desired, desired.grants, mode="apply")

    assert plan.has_changes is False
    assert plan.create == ()
    assert plan.activate == ()
    assert plan.revoke == ()
    assert plan.unchanged == desired.grants


def test_conflicting_existing_state_fails_closed_during_diff(
    tmp_path: Path,
) -> None:
    roles_csv = tmp_path / "config" / "roles.csv"
    _write_roles(
        roles_csv,
        "pseudonym_actor_001,student,course_network,class_01,true",
    )
    desired = _load_role_seeds(roles_csv)
    existing = (
        RoleGrantSeed(
            actor_id="pseudonym_actor_001",
            role="student",
            course_id="course_network",
            class_id="class_01",
            is_active=True,
        ),
        RoleGrantSeed(
            actor_id="pseudonym_actor_001",
            role="teacher",
            course_id="course_network",
            class_id="class_01",
            is_active=True,
        ),
    )

    with pytest.raises(RoleSeedError, match="conflicting_role"):
        build_role_sync_plan(desired, existing)


def test_repository_roles_example_is_valid_and_complete() -> None:
    project_root = Path(__file__).resolve().parents[2]

    document = load_role_seeds(
        project_root / "config" / "roles.example.csv",
        config_root=project_root / "config",
    )

    assert {grant.role for grant in document.grants} == {
        "student",
        "teacher",
        "course_admin",
        "system_admin",
    }
    assert len(document.checksum) == 64


def test_invalid_existing_grant_cannot_enter_a_sync_plan(
    tmp_path: Path,
) -> None:
    roles_csv = tmp_path / "config" / "roles.csv"
    _write_roles(
        roles_csv,
        "pseudonym_actor_001,student,course_network,class_01,true",
    )
    desired = _load_role_seeds(roles_csv)
    corrupt_existing = (
        RoleGrantSeed(
            actor_id="pseudonym_actor_002",
            role="system_admin",
            course_id="course_network",
            class_id=None,
            is_active=True,
        ),
    )

    with pytest.raises(RoleSeedError, match="invalid_scope"):
        build_role_sync_plan(desired, corrupt_existing)


def test_role_seed_path_cannot_escape_config_root(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    outside = tmp_path / "outside.csv"
    _write_roles(
        outside,
        "pseudonym_actor_001,student,course_network,class_01,true",
    )

    with pytest.raises(RoleSeedError, match="path_boundary"):
        load_role_seeds(outside, config_root=config_root)


def test_role_seed_symlink_cannot_escape_config_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "config"
    outside = tmp_path / "outside.csv"
    link = config_root / "roles.csv"
    _write_roles(
        outside,
        "pseudonym_actor_001,student,course_network,class_01,true",
    )
    config_root.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(outside)
    except OSError:
        path_type = type(link)
        original_resolve = path_type.resolve
        outside_resolved = outside.resolve()

        def simulated_symlink_resolve(
            candidate: Path,
            *args: object,
            **kwargs: object,
        ) -> Path:
            if candidate == link:
                return outside_resolved
            return original_resolve(candidate, *args, **kwargs)

        monkeypatch.setattr(path_type, "resolve", simulated_symlink_resolve)

    with pytest.raises(RoleSeedError, match="path_boundary"):
        load_role_seeds(link, config_root=config_root)


def test_explicit_inactive_seed_without_existing_row_is_actionable(
    tmp_path: Path,
) -> None:
    roles_csv = tmp_path / "config" / "roles.csv"
    _write_roles(
        roles_csv,
        "pseudonym_teacher_001,teacher,course_network,class_01,false",
    )
    desired = _load_role_seeds(roles_csv)

    plan = build_role_sync_plan(desired, ())

    assert plan.revoke == desired.grants
    assert plan.unchanged == ()
    assert plan.has_changes is True


def test_omitted_existing_grant_is_revoked_by_full_state_sync(
    tmp_path: Path,
) -> None:
    roles_csv = tmp_path / "config" / "roles.csv"
    _write_roles(
        roles_csv,
        "pseudonym_student_001,student,course_network,class_01,true",
    )
    desired = _load_role_seeds(roles_csv)
    omitted = RoleGrantSeed(
        actor_id="pseudonym_teacher_001",
        role="teacher",
        course_id="course_network",
        class_id="class_01",
        is_active=True,
    )

    plan = build_role_sync_plan(desired, (omitted,))

    assert plan.revoke == (omitted,)
    assert all(grant.actor_id != omitted.actor_id for grant in plan.create)
