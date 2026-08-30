"""Persist readable labels for legacy course/class workspaces."""

from __future__ import annotations

import re

from django.db import migrations


_CLASS_NUMBER = re.compile(r"^class_(\d+)$")


def backfill_workspace_display_names(apps, schema_editor):
    del schema_editor
    Workspace = apps.get_model("m0_platform_web", "CourseClassWorkspace")
    for workspace in Workspace.objects.all().iterator():
        updates = {}
        if not str(workspace.course_display_name or "").strip():
            if workspace.course_id == "course_network":
                updates["course_display_name"] = "计算机网络核心原理与故障诊断"
        if not str(workspace.class_display_name or "").strip():
            matched = _CLASS_NUMBER.fullmatch(str(workspace.class_id))
            if matched is not None:
                updates["class_display_name"] = f"{matched.group(1)}班"
        if updates:
            Workspace.objects.filter(pk=workspace.pk).update(**updates)


class Migration(migrations.Migration):
    dependencies = [("m0_platform_web", "0025_erasure_file_cleanup")]

    operations = [
        migrations.RunPython(
            backfill_workspace_display_names,
            migrations.RunPython.noop,
        )
    ]
