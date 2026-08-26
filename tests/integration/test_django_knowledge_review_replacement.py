from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from tests.integration._django_web_support import make_user


pytestmark = pytest.mark.django_db


def _teacher():
    return make_user(
        actor_id="pseudonym_teacher_new_knowledge",
        role="teacher",
        permissions=(
            "view_class_analytics",
            "review_score",
            "manage_course_knowledge",
        ),
        course_id="course-1",
        class_id="class-1",
    )


def test_teacher_home_no_longer_asks_for_knowledge_review_id(client: Client) -> None:
    client.force_login(_teacher())

    response = client.get(reverse("teacher-home"))

    body = response.content.decode()
    assert response.status_code == 200
    assert "知识包审核" not in body
    assert "review_id" not in body
    assert "课程知识与题目文件" in body


def test_old_knowledge_lookup_redirects_to_course_file_page(client: Client) -> None:
    client.force_login(_teacher())

    response = client.get(
        reverse("teacher-knowledge-review-lookup"),
        {"course_id": "course-1", "class_id": "class-1", "review_id": "obsolete"},
    )

    assert response.status_code == 302
    assert response["Location"].endswith("/teacher/courses/course-1/knowledge/")


def test_old_detail_never_looks_up_review_record(client: Client) -> None:
    client.force_login(_teacher())

    response = client.get(
        reverse(
            "teacher-knowledge-review",
            kwargs={
                "course_id": "course-1",
                "class_id": "class-1",
                "review_id": "record-does-not-exist",
            },
        )
    )

    assert response.status_code == 302
    assert response["Location"].endswith("/teacher/courses/course-1/knowledge/")
    assert reverse("teacher-review-lookup") == "/teacher/reviews/"
