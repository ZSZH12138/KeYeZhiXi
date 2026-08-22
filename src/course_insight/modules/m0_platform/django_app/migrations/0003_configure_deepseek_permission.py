from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("m0_platform_web", "0002_user_m0_user_identity_fields_safe"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="actorgrant",
            options={
                "permissions": [
                    ("start_assessment", "Can start an assessment"),
                    ("submit_assessment", "Can submit an assessment"),
                    ("view_own_result", "Can view own assessment result"),
                    ("view_own_feedback", "Can view own assessment feedback"),
                    ("view_class_analytics", "Can view class analytics"),
                    ("view_student_report", "Can view student reports"),
                    ("review_score", "Can review assessment scores"),
                    ("configure_deepseek", "Can configure the DeepSeek API"),
                    ("manage_course_roles", "Can manage course roles"),
                    ("manage_platform", "Can manage the platform"),
                ]
            },
        ),
    ]
