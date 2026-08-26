from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("m0_platform_web", "0004_knowledge_ingestion"),
    ]

    operations = [
        migrations.AddField(
            model_name="releaseconceptsource",
            name="chunk_text",
            field=models.TextField(default=""),
            preserve_default=False,
        ),
    ]
