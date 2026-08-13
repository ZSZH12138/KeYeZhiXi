from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
        ('m0_platform_web', '0001_initial'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='user',
            constraint=models.CheckConstraint(condition=models.Q(('username', models.F('actor_id')), ('email', ''), ('first_name', ''), ('last_name', '')), name='m0_user_identity_fields_safe'),
        ),
    ]
