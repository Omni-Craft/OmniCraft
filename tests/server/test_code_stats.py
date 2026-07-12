"""Tests for the Code stats dashboard helpers."""

from __future__ import annotations

from datetime import datetime, timedelta

from omnicraft.server.routes.code_stats import _pretty_model, _streaks


def _days_ago(n: int) -> str:
    return (datetime.now().date() - timedelta(days=n)).strftime("%Y-%m-%d")


def test_pretty_model() -> None:
    assert _pretty_model("claude-opus-4-8") == "Opus 4.8"
    assert _pretty_model("claude-sonnet-4-6") == "Sonnet 4.6"
    assert _pretty_model("anthropic/claude-haiku-4-5") == "Haiku 4.5"
    assert _pretty_model("gpt-5") == "Gpt 5"
    assert _pretty_model("") == ""


def test_streaks_empty() -> None:
    assert _streaks(set()) == (0, 0)


def test_streaks_current_and_longest() -> None:
    # Active today, yesterday, and day-before → current streak 3.
    days = {_days_ago(0), _days_ago(1), _days_ago(2)}
    current, longest = _streaks(days)
    assert current == 3
    assert longest == 3


def test_streaks_broken_current_is_zero() -> None:
    # Last activity 3 days ago → no current streak, but a past run of 2.
    days = {_days_ago(3), _days_ago(4)}
    current, longest = _streaks(days)
    assert current == 0
    assert longest == 2


def test_streaks_longest_is_a_past_run() -> None:
    # A 4-day run in the past, plus a lone active day today.
    days = {_days_ago(10), _days_ago(11), _days_ago(12), _days_ago(13), _days_ago(0)}
    current, longest = _streaks(days)
    assert current == 1  # only today
    assert longest == 4
