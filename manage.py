#!/usr/bin/env python
"""Django management entry point without implicit dotenv loading."""

from __future__ import annotations

import os
import sys


def main() -> None:
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE",
        "course_insight.web_project.settings",
    )
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
