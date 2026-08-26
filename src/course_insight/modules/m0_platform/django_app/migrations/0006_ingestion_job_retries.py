import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("m0_platform_web", "0005_releaseconceptsource_chunk_text"),
    ]

    operations = [
        migrations.AlterField(
            model_name="knowledgeingestionjob",
            name="change_set_checksum",
            field=models.CharField(
                db_index=True,
                max_length=64,
                validators=[
                    django.core.validators.RegexValidator(
                        regex="^[0-9a-f]{64}$"
                    )
                ],
            ),
        ),
    ]
