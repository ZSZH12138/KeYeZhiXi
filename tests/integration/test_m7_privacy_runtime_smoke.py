"""Opt-in smoke for the real Presidio Chinese NLP integration."""

from __future__ import annotations

import importlib.metadata
import os
import platform
import sys

import pytest

from course_insight.modules.m7_local_model.privacy_reviewer import (
    DenyAllPrivacyReviewer,
    build_presidio_spacy_reviewer,
)


@pytest.mark.skipif(
    os.environ.get("M7_PRESIDIO_ZH_SMOKE") != "1",
    reason="set M7_PRESIDIO_ZH_SMOKE=1 in the approved target runtime",
)
def test_real_presidio_zh_core_web_sm_runtime() -> None:
    """Prove the optional packages and Chinese model really initialize."""

    assert (3, 11) <= sys.version_info[:2] <= (3, 13), (
        "the approved M7 Presidio runtime must use Python 3.11-3.13; "
        f"found {platform.python_version()}"
    )
    reviewer = build_presidio_spacy_reviewer(
        model_name="zh_core_web_sm",
        expected_model_version=importlib.metadata.version("zh_core_web_sm"),
        expected_presidio_version=importlib.metadata.version(
            "presidio-analyzer"
        ),
        expected_spacy_version=importlib.metadata.version("spacy"),
    )

    assert not isinstance(reviewer, DenyAllPrivacyReviewer)
    assert reviewer.review("二次函数在定义域内单调递增。").decision == "allow"
    assert reviewer.review("我叫张三，住在北京市朝阳区。").decision == "block"
