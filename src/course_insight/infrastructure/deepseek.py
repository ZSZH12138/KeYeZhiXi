"""Network-free DeepSeek adapter used by the architecture scaffold.

The real HTTP client is deliberately absent at this stage.  This adapter does
not inspect environment variables and always returns an explicit empty result.
"""

from __future__ import annotations

from course_insight.contracts.intelligence import (
    LLMGenerationRequest,
    LLMGenerationResult,
)


class EmptyDeepSeekAdapter:
    """Represent the future DeepSeek API boundary without making API calls."""

    def generate(self, request: LLMGenerationRequest) -> LLMGenerationResult:
        """Return a valid empty result tied to the supplied governed request."""

        return LLMGenerationResult(
            request_id=request.request_id,
            provider="deepseek",
            status="empty",
            content="",
            structured_output={},
            citation_ids=[],
            finish_reason="not_run",
            generated_at=request.created_at,
        )
