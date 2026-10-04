"""Viewport-only text cannot contradict a caller check when the observation is partial."""

import pytest

from jev_ultrafast.planning import Check, evidence, verify


@pytest.mark.parametrize("page", [
    {"text": "Visible", "h": 600, "scroll": {"y": 0, "height": 1200}},
    {"text": "x" * 6000, "h": 600, "scroll": {"y": 0, "height": 600}},
])
def test_missing_text_in_partial_viewport_is_unknown(page):
    check = Check("text", "Target")
    assert evidence(page, (check,)) == [{"state": "unknown", "reason": "partial_text"}]
    assert verify(page, (check,)) == [False]


@pytest.mark.parametrize("page", [
    {"text": "Target", "h": 600, "scroll": {"y": 0, "height": 1200}},
    {"text": "Target" + "x" * 5994, "h": 600, "scroll": {"y": 0, "height": 600}},
])
def test_found_text_is_met_even_in_partial_viewport(page):
    assert evidence(page, (Check("text", "Target"),)) == [{"state": "met", "reason": "observed"}]


@pytest.mark.parametrize("page", [
    {"text": "Visible", "h": 600, "scroll": {"y": 0, "height": 600}},
    {"text": "x" * 5999, "h": 600, "scroll": {"y": 0, "height": 600}},
    {"text": "Visible"},  # Legacy callers without coverage metadata retain their prior result.
])
def test_missing_text_in_complete_or_unspecified_viewport_is_unmet(page):
    assert evidence(page, (Check("text", "Target"),)) == [{"state": "unmet", "reason": "observed"}]


def test_title_is_not_a_viewport_predicate():
    page = {"title": "Other", "text": "x" * 6000, "h": 600,
            "scroll": {"y": 0, "height": 1200}}
    assert evidence(page, (Check("title", "Target"),)) == [{"state": "unmet", "reason": "observed"}]
