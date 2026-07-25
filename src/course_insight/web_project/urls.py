"""Root URL configuration for the M0-owned Django shell."""

from django.urls import include, path

from course_insight.modules.m0_platform.django_app import error_mapping


urlpatterns = [
    path("", include("course_insight.modules.m0_platform.django_app.urls")),
]

handler400 = error_mapping.bad_request
handler403 = error_mapping.permission_denied
handler404 = error_mapping.page_not_found
handler500 = error_mapping.server_error
