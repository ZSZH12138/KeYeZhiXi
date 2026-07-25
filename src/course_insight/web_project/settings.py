"""Django settings derived from the single validated M0 configuration."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from course_insight.infrastructure.config import (
    PlatformSettings,
    load_platform_settings,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _load_settings() -> PlatformSettings:
    kwargs: dict[str, object] = {
        "project_root": _PROJECT_ROOT,
        "environment": os.environ,
    }
    dotenv = os.environ.get("DJANGO_COURSE_INSIGHT_DOTENV_PATH")
    app_json = os.environ.get("DJANGO_COURSE_INSIGHT_APP_JSON_PATH")
    if dotenv:
        kwargs["dotenv_path"] = Path(dotenv)
    if app_json:
        kwargs["app_json_path"] = Path(app_json)
    return load_platform_settings(**kwargs)


def _database_configuration(
    platform: PlatformSettings,
) -> dict[str, object]:
    if platform.database.backend == "sqlite":
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": platform.database.sqlite_path,
            "OPTIONS": {"timeout": platform.database.connect_timeout_seconds},
        }
    if platform.database.url is None:
        raise RuntimeError("validated PostgreSQL configuration is unavailable")
    parsed = urlsplit(platform.database.url.get_secret_value())
    query = parse_qs(parsed.query, keep_blank_values=False)
    options = {
        key: values[-1]
        for key, values in query.items()
        if key in {"sslmode", "target_session_attrs"} and values
    }
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parsed.path.lstrip("/")),
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname or "",
        "PORT": parsed.port or "",
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            **options,
            "connect_timeout": int(
                platform.database.connect_timeout_seconds
            ),
        },
    }


PLATFORM_SETTINGS = _load_settings()
COURSE_INSIGHT_RUNTIME_DIR = PLATFORM_SETTINGS.runtime_dir
COURSE_INSIGHT_CONFIG_DIR = PLATFORM_SETTINGS.config_dir
COURSE_INSIGHT_ROLES_PATH = PLATFORM_SETTINGS.config_dir / "roles.csv"
COURSE_INSIGHT_STATE_POLICY_PATH = (
    PLATFORM_SETTINGS.config_dir / "state.json"
)
COURSE_INSIGHT_TEACHER_THRESHOLD_POLICY_PATH = (
    PLATFORM_SETTINGS.config_dir / "teacher.json"
)
COURSE_INSIGHT_OUTBOX_STATUS_DIR = (
    PLATFORM_SETTINGS.runtime_dir / "outbox_worker"
)
COURSE_INSIGHT_LOGIN_FAILURE_LIMIT = (
    PLATFORM_SETTINGS.security.login_failure_limit
)
COURSE_INSIGHT_LOGIN_FAILURE_WINDOW_SECONDS = (
    PLATFORM_SETTINGS.security.login_failure_window_seconds
)
_configured_secret = (
    None
    if PLATFORM_SETTINGS.web.secret_key is None
    else PLATFORM_SETTINGS.web.secret_key.get_secret_value()
)

SECRET_KEY = _configured_secret or "course-insight-development-only-key"
DEBUG = PLATFORM_SETTINGS.environment == "development"
ALLOWED_HOSTS = list(PLATFORM_SETTINGS.web.allowed_hosts)
CSRF_TRUSTED_ORIGINS = list(
    PLATFORM_SETTINGS.web.csrf_trusted_origins
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "course_insight.modules.m0_platform.django_app.apps.M0PlatformWebConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    (
        "course_insight.modules.m0_platform.django_app.middleware."
        "M0RequestMiddleware"
    ),
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "course_insight.web_project.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]
WSGI_APPLICATION = "course_insight.web_project.wsgi.application"
ASGI_APPLICATION = "course_insight.web_project.asgi.application"

DATABASES = {"default": _database_configuration(PLATFORM_SETTINGS)}
AUTH_USER_MODEL = "m0_platform_web.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "UserAttributeSimilarityValidator"
        )
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "MinimumLengthValidator"
        )
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "CommonPasswordValidator"
        )
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "NumericPasswordValidator"
        )
    },
]

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"

SESSION_COOKIE_HTTPONLY = (
    PLATFORM_SETTINGS.security.session_cookie_httponly
)
SESSION_COOKIE_SECURE = PLATFORM_SETTINGS.web.secure_cookie
SESSION_COOKIE_SAMESITE = (
    PLATFORM_SETTINGS.security.session_cookie_samesite
)
SESSION_COOKIE_AGE = PLATFORM_SETTINGS.web.session_timeout_seconds
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
CSRF_COOKIE_SECURE = PLATFORM_SETTINGS.web.secure_cookie
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = PLATFORM_SETTINGS.security.session_cookie_samesite
DATA_UPLOAD_MAX_MEMORY_SIZE = (
    PLATFORM_SETTINGS.security.max_request_body_bytes
)
FILE_UPLOAD_MAX_MEMORY_SIZE = (
    PLATFORM_SETTINGS.security.max_request_body_bytes
)
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
SECURE_SSL_REDIRECT = PLATFORM_SETTINGS.web.secure_cookie
SECURE_HSTS_SECONDS = 31_536_000 if PLATFORM_SETTINGS.web.secure_cookie else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = PLATFORM_SETTINGS.web.secure_cookie
SECURE_HSTS_PRELOAD = PLATFORM_SETTINGS.web.secure_cookie

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "student-home"
LOGOUT_REDIRECT_URL = "login"
