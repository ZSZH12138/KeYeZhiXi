"""URL surface owned by the M0 Django boundary."""

from django.urls import path

from course_insight.modules.m0_platform.django_app.views import (
    auth,
    health,
    student,
    teacher,
)


urlpatterns = [
    path("health/live/", health.live, name="health-live"),
    path("health/ready/", health.ready, name="health-ready"),
    path("accounts/login/", auth.login, name="login"),
    path("accounts/logout/", auth.logout, name="logout"),
    path("student/", student.home, name="student-home"),
    path(
        "student/start/",
        student.select_scope,
        name="student-select-scope",
    ),
    path(
        (
            "student/courses/<str:course_id>/classes/<str:class_id>/"
            "assessments/start/"
        ),
        student.start,
        name="student-start",
    ),
    path(
        (
            "student/courses/<str:course_id>/classes/<str:class_id>/"
            "assessments/<str:paper_id>/"
        ),
        student.assessment,
        name="student-assessment",
    ),
    path(
        (
            "student/courses/<str:course_id>/classes/<str:class_id>/"
            "assessments/<str:paper_id>/submit/"
        ),
        student.submit,
        name="student-submit",
    ),
    path(
        (
            "student/courses/<str:course_id>/classes/<str:class_id>/"
            "results/<str:paper_id>/"
        ),
        student.result,
        name="student-result",
    ),
    path(
        (
            "student/courses/<str:course_id>/classes/<str:class_id>/"
            "feedback/<str:paper_id>/"
        ),
        student.feedback,
        name="student-feedback",
    ),
    path("teacher/", teacher.home, name="teacher-home"),
    path("teacher/reviews/", teacher.lookup, name="teacher-review-lookup"),
    path(
        "teacher/knowledge-reviews/",
        teacher.knowledge_lookup,
        name="teacher-knowledge-review-lookup",
    ),
    path(
        "teacher/courses/<str:course_id>/classes/<str:class_id>/",
        teacher.class_context,
        name="teacher-class",
    ),
    path(
        (
            "teacher/courses/<str:course_id>/classes/<str:class_id>/"
            "reviews/<str:paper_id>/"
        ),
        teacher.review_context,
        name="teacher-review-context",
    ),
    path(
        (
            "teacher/courses/<str:course_id>/classes/<str:class_id>/"
            "reviews/<str:paper_id>/audits/<str:audit_id>/"
        ),
        teacher.review,
        name="teacher-review",
    ),
    path(
        (
            "teacher/courses/<str:course_id>/classes/<str:class_id>/"
            "knowledge-reviews/<str:review_id>/"
        ),
        teacher.knowledge_review,
        name="teacher-knowledge-review",
    ),
]
