"""Shared shell-out helper for runner-local tools that drive the host machine.

The iOS-simulator and computer-control tools both run host binaries (``simctl``,
``screencapture``, ``cliclick``). They share this thin wrapper so timeout,
decoding and missing-binary handling behave identically, and so tests can stub a
single seam.
"""

from __future__ import annotations

import asyncio

#: Default ceiling for a host command. Long jobs (builds) pass their own.
DEFAULT_TIMEOUT_S = 60.0

#: Exit codes we synthesise for failures that never reached the child process.
NOT_FOUND_CODE = 127
TIMEOUT_CODE = 124


class ShellResult:
    """Outcome of one shell-out: return code plus captured streams."""

    __slots__ = ("returncode", "stderr", "stdout")

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self) -> bool:
        return self.returncode == 0


async def shell_out(argv: list[str], *, timeout: float = DEFAULT_TIMEOUT_S) -> ShellResult:
    """Run a command, capturing stdout/stderr.

    :param argv: Full argument vector, e.g. ``["screencapture", "-x", path]``.
    :param timeout: Seconds before the child is killed and reported as timed out.
    :returns: A :class:`ShellResult`; a missing binary or a timeout comes back as
        a failed result rather than an exception, so callers can turn it into an
        actionable message for the model.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return ShellResult(NOT_FOUND_CODE, "", f"command not found: {argv[0]}")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        proc.kill()
        await proc.wait()
        return ShellResult(TIMEOUT_CODE, "", f"timed out after {timeout:.0f}s: {' '.join(argv)}")
    return ShellResult(
        proc.returncode or 0,
        (out or b"").decode("utf-8", "replace"),
        (err or b"").decode("utf-8", "replace"),
    )


def tail(text: str, limit: int = 4000) -> str:
    """Trim long command output to its last ``limit`` chars for the model."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return "… (início truncado)\n" + text[-limit:]
