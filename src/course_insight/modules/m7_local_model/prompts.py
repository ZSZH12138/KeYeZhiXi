"""Prompt identifiers and learner-safe placeholder feedback text."""

from __future__ import annotations


SCORING_PROMPT_ID = "placeholder-rubric-scoring-v1"
FEEDBACK_PROMPT_ID = "placeholder-evidence-hint-v1"


def feedback_message(concept_ids: list[str]) -> str:
    """Build a deterministic hint that asks the learner to reason from evidence."""

    targets = ", ".join(concept_ids) if concept_ids else "the cited course rule"
    return (
        f"Review the cited passage for {targets}. Then compare its conditions "
        "with your reasoning and explain one correction in your own words."
    )
