from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("m0_platform_web", "0021_workspace_creation_token"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="scopeddeepseekconfiguration",
            name="updated_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name="scoped_deepseek_updates",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
