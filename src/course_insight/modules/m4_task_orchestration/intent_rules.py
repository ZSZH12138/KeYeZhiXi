"""Conflict-aware deterministic task-type rules for the private intent layer."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


_SUPPORTED_LABELS = frozenset(
    {"qa", "diagnostic", "practice", "correction", "stage_assessment"}
)

_HIGH_PRECISION_RULES = (
    ("diagnostic", ("diagnostic", "诊断")),
    ("correction", ("correction", "订正", "纠错")),
    ("practice", ("practice", "练习")),
    ("stage_assessment", ("stage assessment", "阶段测评")),
    ("qa", ("question", "explain", "why", "how", "what", "问答", "提问")),
)

_LEGACY_RULES = (
    ("diagnostic", ("diagnosis", "摸底")),
    ("correction", ("correct my", "错题")),
    ("practice", ("exercise", "训练")),
    (
        "stage_assessment",
        (
            "assessment",
            "exam",
            "阶段测试",
            "测评",
            "考试",
            "考核",
        ),
    ),
    (
        "qa",
        (
            "解释",
            "为什么",
            "怎么",
            "如何",
            "?",
            "？",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class RuleMatch:
    """All matching task families, never an arbitrary first-match selection."""

    labels: tuple[str, ...]
    resolved_label: str | None = field(init=False)

    def __post_init__(self) -> None:
        if tuple(sorted(set(self.labels))) != self.labels:
            raise ValueError("rule labels must be sorted and unique")
        if any(label not in _SUPPORTED_LABELS for label in self.labels):
            raise ValueError("rule labels must be supported")
        object.__setattr__(
            self,
            "resolved_label",
            self.labels[0] if len(self.labels) == 1 else None,
        )


def match_high_precision_rules(text: str) -> RuleMatch:
    """Collect all high-precision families matching normalized learner text."""

    return _match(text, _HIGH_PRECISION_RULES)


def match_legacy_rules(text: str) -> RuleMatch:
    """Collect all legacy fallback families matching normalized learner text."""

    return _match(text, _LEGACY_RULES)


def resolve_task_type_from_rules(text: str) -> str | None:
    """Resolve one family, rejecting conflicts instead of using precedence."""

    high_precision = match_high_precision_rules(text)
    legacy = match_legacy_rules(text)
    return RuleMatch(
        labels=tuple(sorted(set(high_precision.labels) | set(legacy.labels)))
    ).resolved_label


def _match(
    text: str,
    rules: tuple[tuple[str, tuple[str, ...]], ...],
) -> RuleMatch:
    normalized_text = _normalize_text(text)
    labels = tuple(
        sorted(
            label
            for label, keywords in rules
            if any(_contains_keyword(normalized_text, keyword) for keyword in keywords)
        )
    )
    return RuleMatch(labels=labels)


def _normalize_text(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("student task text must be a string")
    return " ".join(text.split()).casefold()


def _contains_keyword(text: str, keyword: str) -> bool:
    if keyword.isascii() and keyword not in {"?"}:
        return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text) is not None
    return keyword in text
