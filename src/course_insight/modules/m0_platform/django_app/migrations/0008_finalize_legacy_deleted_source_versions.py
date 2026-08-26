from django.db import migrations


def finalize_legacy_deleted_source_versions(apps, schema_editor):
    del schema_editor
    CourseSourceVersion = apps.get_model(
        "m0_platform_web",
        "CourseSourceVersion",
    )
    CourseSourceVersion.objects.filter(source__status="deleted").exclude(
        status="deleted"
    ).update(status="deleted")


class Migration(migrations.Migration):

    dependencies = [
        ("m0_platform_web", "0007_course_source_pending_delete"),
    ]

    operations = [
        migrations.RunPython(
            finalize_legacy_deleted_source_versions,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
