from __future__ import annotations

import json
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand

from course_insight.infrastructure.deepseek_secrets import (
    public_deepseek_status,
)


class Command(BaseCommand):
    help = "Print safe DeepSeek configuration status without initializing runtime."

    def handle(self, *args: Any, **options: Any) -> None:
        status = public_deepseek_status(settings.COURSE_INSIGHT_RUNTIME_DIR)
        payload = {
            "configured": status.configured,
            "key_source": status.key_source,
            "model_name": status.model_name,
            "privacy_gate_ready": status.privacy_gate_ready,
            "privacy_gate_reason": status.privacy_gate_reason,
            "scoring_ready": status.scoring_ready,
            "thinking_enabled": status.thinking_enabled,
        }
        self.stdout.write(
            json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )
