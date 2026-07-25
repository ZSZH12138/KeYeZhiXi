import django.contrib.auth.models
import django.contrib.auth.validators
import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.CreateModel(
            name='User',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('password', models.CharField(max_length=128, verbose_name='password')),
                ('last_login', models.DateTimeField(blank=True, null=True, verbose_name='last login')),
                ('is_superuser', models.BooleanField(default=False, help_text='Designates that this user has all permissions without explicitly assigning them.', verbose_name='superuser status')),
                ('username', models.CharField(error_messages={'unique': 'A user with that username already exists.'}, help_text='Required. 150 characters or fewer. Letters, digits and @/./+/-/_ only.', max_length=150, unique=True, validators=[django.contrib.auth.validators.UnicodeUsernameValidator()], verbose_name='username')),
                ('first_name', models.CharField(blank=True, max_length=150, verbose_name='first name')),
                ('last_name', models.CharField(blank=True, max_length=150, verbose_name='last name')),
                ('email', models.EmailField(blank=True, max_length=254, verbose_name='email address')),
                ('is_staff', models.BooleanField(default=False, help_text='Designates whether the user can log into this admin site.', verbose_name='staff status')),
                ('is_active', models.BooleanField(default=True, help_text='Designates whether this user should be treated as active. Unselect this instead of deleting accounts.', verbose_name='active')),
                ('date_joined', models.DateTimeField(default=django.utils.timezone.now, verbose_name='date joined')),
                ('actor_id', models.CharField(max_length=128, unique=True, validators=[django.core.validators.RegexValidator(code='invalid_actor_id', message='actor_id must be pseudonymous', regex='^pseudonym_[a-z0-9][a-z0-9_-]{0,116}$')])),
                ('groups', models.ManyToManyField(blank=True, help_text='The groups this user belongs to. A user will get all permissions granted to each of their groups.', related_name='user_set', related_query_name='user', to='auth.group', verbose_name='groups')),
                ('user_permissions', models.ManyToManyField(blank=True, help_text='Specific permissions for this user.', related_name='user_set', related_query_name='user', to='auth.permission', verbose_name='user permissions')),
            ],
            options={
                'verbose_name': 'user',
                'verbose_name_plural': 'users',
                'abstract': False,
            },
            managers=[
                ('objects', django.contrib.auth.models.UserManager()),
            ],
        ),
        migrations.CreateModel(
            name='ActorGrant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('role', models.CharField(choices=[('student', 'Student'), ('teacher', 'Teacher'), ('course_admin', 'Course administrator'), ('system_admin', 'System administrator')], max_length=32)),
                ('course_id', models.CharField(blank=True, max_length=128, null=True, validators=[django.core.validators.RegexValidator(code='invalid_scope_id', message='scope identifier is invalid', regex='^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')])),
                ('class_id', models.CharField(blank=True, max_length=128, null=True, validators=[django.core.validators.RegexValidator(code='invalid_scope_id', message='scope identifier is invalid', regex='^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')])),
                ('is_active', models.BooleanField(default=True)),
                ('source_checksum', models.CharField(max_length=64, validators=[django.core.validators.RegexValidator(code='invalid_source_checksum', message='source checksum is invalid', regex='^[0-9a-f]{64}$')])),
                ('valid_from', models.DateTimeField(default=django.utils.timezone.now)),
                ('valid_until', models.DateTimeField(blank=True, null=True)),
                ('revoked_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='actor_grants', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'permissions': [('start_assessment', 'Can start an assessment'), ('submit_assessment', 'Can submit an assessment'), ('view_own_result', 'Can view own assessment result'), ('view_own_feedback', 'Can view own assessment feedback'), ('view_class_analytics', 'Can view class analytics'), ('view_student_report', 'Can view student reports'), ('review_score', 'Can review assessment scores'), ('manage_course_roles', 'Can manage course roles'), ('manage_platform', 'Can manage the platform')],
            },
        ),
        migrations.CreateModel(
            name='LoginFailureBucket',
            fields=[
                ('bucket_key', models.CharField(max_length=64, primary_key=True, serialize=False, validators=[django.core.validators.RegexValidator(code='invalid_source_checksum', message='source checksum is invalid', regex='^[0-9a-f]{64}$')])),
                ('window_started_at', models.DateTimeField()),
                ('failure_count', models.PositiveIntegerField()),
                ('locked_until', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'indexes': [models.Index(fields=['locked_until'], name='m0_login_bucket_lock_idx')],
                'constraints': [models.CheckConstraint(condition=models.Q(('bucket_key__regex', '^[0-9a-f]{64}$')), name='m0_login_bucket_key_shape'), models.CheckConstraint(condition=models.Q(('failure_count__gte', 1)), name='m0_login_bucket_positive_count'), models.CheckConstraint(condition=models.Q(('locked_until__isnull', True), ('locked_until__gt', models.F('window_started_at')), _connector='OR'), name='m0_login_bucket_lock_interval')],
            },
        ),
        migrations.CreateModel(
            name='RoleSyncState',
            fields=[
                ('source_name', models.CharField(default='roles', editable=False, max_length=64, primary_key=True, serialize=False)),
                ('source_checksum', models.CharField(max_length=64, validators=[django.core.validators.RegexValidator(code='invalid_source_checksum', message='source checksum is invalid', regex='^[0-9a-f]{64}$')])),
                ('grant_count', models.PositiveIntegerField(default=0)),
                ('synchronized_at', models.DateTimeField()),
            ],
            options={
                'constraints': [models.CheckConstraint(condition=models.Q(('source_checksum__regex', '^[0-9a-f]{64}$')), name='m0_role_sync_checksum_shape')],
            },
        ),
        migrations.AddConstraint(
            model_name='user',
            constraint=models.CheckConstraint(condition=models.Q(('actor_id__regex', '^pseudonym_[a-z0-9][a-z0-9_-]{0,116}$')), name='m0_user_actor_id_pseudonymous'),
        ),
        migrations.AddIndex(
            model_name='actorgrant',
            index=models.Index(fields=['user', 'is_active', 'role', 'course_id', 'class_id'], name='m0_grant_auth_scope_idx'),
        ),
        migrations.AddIndex(
            model_name='actorgrant',
            index=models.Index(fields=['valid_until', 'revoked_at'], name='m0_grant_validity_idx'),
        ),
        migrations.AddConstraint(
            model_name='actorgrant',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('role__in', ('student', 'teacher')), ('course_id__isnull', False), ('class_id__isnull', False)), models.Q(('role', 'course_admin'), ('course_id__isnull', False), ('class_id__isnull', True)), models.Q(('role', 'system_admin'), ('course_id__isnull', True), ('class_id__isnull', True)), _connector='OR'), name='m0_grant_role_scope_shape'),
        ),
        migrations.AddConstraint(
            model_name='actorgrant',
            constraint=models.CheckConstraint(condition=models.Q(('valid_until__isnull', True), ('valid_until__gt', models.F('valid_from')), _connector='OR'), name='m0_grant_valid_interval'),
        ),
        migrations.AddConstraint(
            model_name='actorgrant',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('is_active', True), ('revoked_at__isnull', True)), models.Q(('is_active', False), ('revoked_at__isnull', False)), _connector='OR'), name='m0_grant_active_revocation'),
        ),
        migrations.AddConstraint(
            model_name='actorgrant',
            constraint=models.CheckConstraint(condition=models.Q(('source_checksum__regex', '^[0-9a-f]{64}$')), name='m0_grant_checksum_shape'),
        ),
        migrations.AddConstraint(
            model_name='actorgrant',
            constraint=models.UniqueConstraint(condition=models.Q(('role__in', ('student', 'teacher'))), fields=('user', 'role', 'course_id', 'class_id'), name='m0_grant_unique_class_scope'),
        ),
        migrations.AddConstraint(
            model_name='actorgrant',
            constraint=models.UniqueConstraint(condition=models.Q(('role', 'course_admin')), fields=('user', 'role', 'course_id'), name='m0_grant_unique_course_scope'),
        ),
        migrations.AddConstraint(
            model_name='actorgrant',
            constraint=models.UniqueConstraint(condition=models.Q(('role', 'system_admin')), fields=('user', 'role'), name='m0_grant_unique_platform_scope'),
        ),
    ]
