from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("m0_platform_web", "0006_ingestion_job_retries"),
    ]

    operations = [
        migrations.AlterField(
            model_name="coursesource",
            name="status",
            field=models.CharField(
                choices=[
                    ("staged", "Staged"),
                    ("active", "Active"),
                    ("pending_delete", "Pending deletion"),
                    ("deleted", "Deleted"),
                ],
                default="staged",
                max_length=16,
            ),
        ),
    ]
