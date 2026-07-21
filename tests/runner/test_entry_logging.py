"""Logging setup for the runner process.

The runner's stderr IS its log file, so a write failure there is the one
failure the log cannot report by the usual means. These tests pin what it
does instead.
"""

from __future__ import annotations

import io
import logging
import sys

import pytest

from omnicraft.runner._entry import _CompactErrorStreamHandler


class _FullDisk(io.TextIOBase):
    """A stream that accepts writes up to a budget and rejects the rest.

    This is what a full volume looks like from inside ``logging``: the tiny
    ``--- Logging error ---`` header slips into the residual free space while
    the real payload does not — which is how the stdlib's multi-write
    diagnostic ends up as a header with nothing after it.
    """

    def __init__(self, budget: int) -> None:
        """:param budget: Bytes a single write may use before ENOSPC."""
        self.budget = budget
        self.written: list[str] = []

    def write(self, text: str) -> int:
        """:param text: Text to write. :returns: Characters written."""
        if len(text) > self.budget:
            raise OSError(28, "No space left on device")
        self.written.append(text)
        return len(text)


def _emit(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream: _FullDisk,
    diagnostic: _FullDisk,
    raise_exceptions: bool = True,
) -> None:
    """Emit one record through the handler.

    :param monkeypatch: Fixture used to swap ``sys.stderr``.
    :param stream: Stream the handler writes the record to.
    :param diagnostic: Stream the handler reports failures to.
    :param raise_exceptions: Value of ``logging.raiseExceptions``.
    :returns: None.
    """
    monkeypatch.setattr(sys, "stderr", diagnostic)
    monkeypatch.setattr(logging, "raiseExceptions", raise_exceptions)
    _CompactErrorStreamHandler(stream).emit(
        logging.LogRecord(
            name="omnicraft.runner",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="tunnel closed before request completed",
            args=(),
            exc_info=None,
        )
    )


def test_a_dropped_record_names_itself_and_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The diagnostic carries the lost message and the errno, in one write."""
    diagnostic = _FullDisk(budget=4096)

    _emit(monkeypatch, stream=_FullDisk(budget=0), diagnostic=diagnostic)

    assert len(diagnostic.written) == 1, "the diagnostic must be one write, not a sequence"
    assert "No space left on device" in diagnostic.written[0]
    assert "tunnel closed before request completed" in diagnostic.written[0]


def test_a_failing_diagnostic_stream_is_not_an_error_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A logger that cannot report its failure still must not raise.

    This is the full-disk case taken to its end: even the one-line
    diagnostic does not fit. Losing the report is acceptable; taking the
    runner down over a log line is not.
    """
    diagnostic = _FullDisk(budget=0)

    _emit(monkeypatch, stream=_FullDisk(budget=0), diagnostic=diagnostic)

    assert diagnostic.written == []


def test_a_healthy_stream_reports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No diagnostic is written when the record itself lands."""
    stream = _FullDisk(budget=4096)
    diagnostic = _FullDisk(budget=4096)

    _emit(monkeypatch, stream=stream, diagnostic=diagnostic)

    assert diagnostic.written == []
    assert any("tunnel closed" in chunk for chunk in stream.written)


def test_silencing_the_logger_silences_the_diagnostic_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``logging.raiseExceptions = False`` means no diagnostics at all."""
    diagnostic = _FullDisk(budget=4096)

    _emit(
        monkeypatch,
        stream=_FullDisk(budget=0),
        diagnostic=diagnostic,
        raise_exceptions=False,
    )

    assert diagnostic.written == []
