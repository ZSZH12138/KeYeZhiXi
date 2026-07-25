"""Django application declaration for the M0 Web boundary."""

from django.apps import AppConfig


class M0PlatformWebConfig(AppConfig):
    """Register M0 Web models without performing startup side effects."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "course_insight.modules.m0_platform.django_app"
    label = "m0_platform_web"
    verbose_name = "Course Insight M0 Web"

    def ready(self) -> None:
        """Deliberately avoid migrations, repositories, or worker startup."""

        return None
