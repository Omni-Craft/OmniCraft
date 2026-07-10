"""
Unit tests for ``omnicraft pane-picker``'s argv normalization.

The picker is exec'd as the new tmux pane's initial command after a
``pane-split``. It reads the parent pane's launch context, strips
flags that don't make sense for a sibling pane (resume modes,
one-shot prompts), then ``os.execvp``\\s into a fresh REPL.

These tests pin the strip helpers — the real exec path is exercised
manually in the design's § 6 phase 5 verification.
"""

from __future__ import annotations

import pytest

from omnicraft.cli import _strip_one_shot_flags, _strip_resume_flags


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # Bare ``--resume`` (picker mode): drop the single token.
        (
            ["omnicraft", "run", "a.yaml", "--profile", "prf", "--resume"],
            ["omnicraft", "run", "a.yaml", "--profile", "prf"],
        ),
        # ``--resume`` with a conversation id: drop both tokens.
        (
            ["omnicraft", "run", "a.yaml", "--resume", "conv_abc"],
            ["omnicraft", "run", "a.yaml"],
        ),
        # ``--resume=conv_id`` long-form: drop the combined token.
        (
            ["omnicraft", "run", "a.yaml", "--resume=conv_abc"],
            ["omnicraft", "run", "a.yaml"],
        ),
        # ``-r`` short form, no value: drop the single token.
        (
            ["omnicraft", "run", "a.yaml", "-r"],
            ["omnicraft", "run", "a.yaml"],
        ),
        # ``-r conv_id`` short form with value: drop both tokens.
        (
            ["omnicraft", "run", "a.yaml", "-r", "conv_abc"],
            ["omnicraft", "run", "a.yaml"],
        ),
        # Continue forms (always boolean).
        (
            ["omnicraft", "run", "a.yaml", "-c"],
            ["omnicraft", "run", "a.yaml"],
        ),
        (
            ["omnicraft", "run", "a.yaml", "--continue"],
            ["omnicraft", "run", "a.yaml"],
        ),
        # Legacy ``--session`` / ``-s`` shapes still strip cleanly so
        # a parent argv saved before the resume/session consolidation
        # sanitizes without errors.
        (
            ["omnicraft", "run", "a.yaml", "--session", "conv_abc"],
            ["omnicraft", "run", "a.yaml"],
        ),
        (
            ["omnicraft", "run", "a.yaml", "-s", "conv_abc"],
            ["omnicraft", "run", "a.yaml"],
        ),
        (
            ["omnicraft", "run", "a.yaml", "--session=conv_abc"],
            ["omnicraft", "run", "a.yaml"],
        ),
        # Multiple resume flags in one argv: all dropped.
        (
            [
                "omnicraft",
                "run",
                "a.yaml",
                "--profile",
                "prf",
                "--resume",
                "--continue",
                "--resume",
                "conv_x",
            ],
            ["omnicraft", "run", "a.yaml", "--profile", "prf"],
        ),
        # Non-resume flags survive intact even when sandwiched
        # between resume flags. Bare ``--resume`` followed by
        # another flag must NOT swallow that flag as its value.
        (
            [
                "omnicraft",
                "run",
                "a.yaml",
                "--resume",
                "--profile",
                "prf",
                "--resume",
                "x",
                "--model",
                "m",
            ],
            ["omnicraft", "run", "a.yaml", "--profile", "prf", "--model", "m"],
        ),
        # Empty argv → empty.
        ([], []),
        # Non-resume argv: identity.
        (
            ["omnicraft", "run", "a.yaml", "--model", "m", "--profile", "prf"],
            ["omnicraft", "run", "a.yaml", "--model", "m", "--profile", "prf"],
        ),
    ],
)
def test_strip_resume_flags(argv: list[str], expected: list[str]) -> None:
    """
    The strip helper must remove every shape of resume flag
    (bare ``--resume`` for the picker, ``--resume <id>`` for an
    explicit pin, the ``--resume=<id>`` long form, short ``-r``
    variants, and ``--continue`` / ``-c``) and leave every other
    flag untouched. Legacy ``--session`` / ``-s`` are still
    handled for backwards compatibility with parent argvs saved
    before the consolidation.

    Claim: each input → its expected pruned argv. Live regression
    that prompted this helper: the live pane's argv had
    ``--resume``, the click ``run`` subcommand at the time didn't
    accept that option, so exec'ing the parent's verbatim argv
    exited with a click ``Error: No such option: --resume``
    immediately, closing the new pane within seconds.
    """
    assert _strip_resume_flags(argv) == expected


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # ``-p`` short form: drop the flag and its value.
        (
            ["omnicraft", "run", "a.yaml", "-p", "hello there"],
            ["omnicraft", "run", "a.yaml"],
        ),
        # ``--prompt`` long form.
        (
            ["omnicraft", "run", "a.yaml", "--prompt", "hello"],
            ["omnicraft", "run", "a.yaml"],
        ),
        # ``--prompt=value``.
        (
            ["omnicraft", "run", "a.yaml", "--prompt=hello"],
            ["omnicraft", "run", "a.yaml"],
        ),
        # ``--system-prompt`` (note: spans both an arg-bearing flag
        # and a similarly named flag — make sure we don't strip
        # ``--system`` or ``--prompt-foo`` accidentally).
        (
            ["omnicraft", "run", "a.yaml", "--system-prompt", "be terse"],
            ["omnicraft", "run", "a.yaml"],
        ),
    ],
)
def test_strip_one_shot_flags(argv: list[str], expected: list[str]) -> None:
    """
    One-shot flags (``-p``, ``--prompt``, ``--system-prompt``) tied
    to the parent's first turn must be removed before exec'ing in
    the new pane — otherwise the new pane silently auto-sends the
    parent's prompt, surprising the user.

    Claim: every variant of one-shot flag is removed; everything
    else passes through.
    """
    assert _strip_one_shot_flags(argv) == expected
