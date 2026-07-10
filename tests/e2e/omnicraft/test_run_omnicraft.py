"""Phase 5 integration-code tests -- ``omnicraft run`` shim (mock LLM).

Migrated to mock LLM: uses canned responses so the tests are
deterministic and need no real credentials.

**What breaks if this fails:**
- The ``run`` dispatch site regresses.
- The shim's YAML preparation pipeline breaks silently.
- The in-process omnicraft app fails to answer.
- The output extraction regresses.
- ``OMNICRAFT_RUNTIME=1`` stops being honored.
- ``omnicraft version`` diverges.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

_MODEL = "mock-run-omnicraft-model"

_HARNESS = "openai-agents"

_PROMPT = "say hi in 5 words"

_MIN_ASSISTANT_CHARS = 4

_RUN_TIMEOUT_SEC = 60


def _run_omnicraft_run_omnicraft(
    *,
    omnicraft_python: Path,
    omnicraft_repo_root: Path,
    mock_credentials_env: dict[str, str],
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute ``omnicraft run <hello_world.yaml> ... -p <prompt>``."""
    yaml_path = omnicraft_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"
    argv: list[str] = [
        str(omnicraft_python),
        "-m",
        "omnicraft",
        "run",
        str(yaml_path),
        "--model",
        _MODEL,
        "--harness",
        _HARNESS,
        "-p",
        _PROMPT,
        "--no-log",
        "--no-session",
    ]
    env = dict(mock_credentials_env)
    if extra_env is not None:
        env.update(extra_env)
    return subprocess.run(
        argv,
        env=env,
        cwd=str(omnicraft_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )


def _structural_observations(
    result: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    """Distill structural properties of an ``omnicraft run`` result."""
    text = result.stdout.strip()
    return {
        "exit_code": result.returncode,
        "assistant_text_nonempty": bool(text),
        "assistant_text_meets_min_length": len(text) >= _MIN_ASSISTANT_CHARS,
    }


def test_run_omnicraft_smoke(
    omnicraft_python: Path,
    omnicraft_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    ``omnicraft run hello_world.yaml -p <prompt>`` exits 0,
    prints non-trivial assistant text, and does not re-emit the
    pre-phase-5 hard-error on stderr.
    """
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Hello there nice to meet!"}],
        key=_MODEL,
    )

    result = _run_omnicraft_run_omnicraft(
        omnicraft_python=omnicraft_python,
        omnicraft_repo_root=omnicraft_repo_root,
        mock_credentials_env=mock_credentials_env,
    )
    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}.\n"
        f"stdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    assistant_text = result.stdout.strip()
    assert len(assistant_text) >= _MIN_ASSISTANT_CHARS, (
        f"assistant text shorter than {_MIN_ASSISTANT_CHARS} chars; got {assistant_text!r}"
    )
    assert "phase 5" not in result.stderr, (
        f"Regression: stderr contains the pre-phase-5 hard-error wording. stderr={result.stderr!r}"
    )


def test_run_omnicraft_matches_structural_fields(
    omnicraft_python: Path,
    omnicraft_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Two successive runs agree on structural fields -- proves
    structural stability.
    """
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "Hello there nice to meet!"},
            {"text": "Hello there nice to meet!"},
        ],
        key=_MODEL,
    )

    first = _run_omnicraft_run_omnicraft(
        omnicraft_python=omnicraft_python,
        omnicraft_repo_root=omnicraft_repo_root,
        mock_credentials_env=mock_credentials_env,
    )
    second = _run_omnicraft_run_omnicraft(
        omnicraft_python=omnicraft_python,
        omnicraft_repo_root=omnicraft_repo_root,
        mock_credentials_env=mock_credentials_env,
    )
    first_obs = _structural_observations(first)
    second_obs = _structural_observations(second)
    assert first_obs == second_obs, (
        "Structural observations diverge between runs:\n"
        f"first={first_obs!r}\n"
        f"second={second_obs!r}\n\n"
        f"first stdout: {first.stdout!r}\n"
        f"second stdout: {second.stdout!r}"
    )


def test_run_omnicraft_env_var_enables_integration(
    omnicraft_python: Path,
    omnicraft_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    ``OMNICRAFT_RUNTIME=1`` (with no flag on argv) must route
    through the omnicraft shim.
    """
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Hello from OMNICRAFT_RUNTIME path!"}],
        key=_MODEL,
    )

    result = _run_omnicraft_run_omnicraft(
        omnicraft_python=omnicraft_python,
        omnicraft_repo_root=omnicraft_repo_root,
        mock_credentials_env=mock_credentials_env,
        extra_env={"OMNICRAFT_RUNTIME": "1"},
    )
    assert result.returncode == 0, (
        f"OMNICRAFT_RUNTIME=1 did not yield exit 0; "
        f"got {result.returncode}.\n"
        f"stdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    assistant_text = result.stdout.strip()
    assert len(assistant_text) >= _MIN_ASSISTANT_CHARS, (
        f"OMNICRAFT_RUNTIME=1 assistant text shorter than "
        f"{_MIN_ASSISTANT_CHARS} chars; got {assistant_text!r}"
    )
    assert "phase 5" not in result.stderr, (
        f"OMNICRAFT_RUNTIME=1 fell back to the pre-phase-5 hard error. stderr={result.stderr!r}"
    )


def test_version_omnicraft_matches_version(
    omnicraft_python: Path,
    omnicraft_repo_root: Path,
) -> None:
    """
    ``omnicraft version`` must be stable and independent of
    OMNICRAFT_RUNTIME. No LLM credentials needed.
    """
    baseline = subprocess.run(
        [
            str(omnicraft_python),
            "-m",
            "omnicraft",
            "version",
        ],
        env={k: v for k, v in os.environ.items() if k != "OMNICRAFT_RUNTIME"},
        cwd=str(omnicraft_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    with_ap = subprocess.run(
        [
            str(omnicraft_python),
            "-m",
            "omnicraft",
            "version",
        ],
        env={**os.environ, "OMNICRAFT_RUNTIME": "1"},
        cwd=str(omnicraft_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert baseline.returncode == 0
    assert with_ap.returncode == 0
    assert baseline.stdout == with_ap.stdout, (
        "omnicraft version diverged between baseline and OMNICRAFT_RUNTIME=1. "
        f"baseline={baseline.stdout!r} ap={with_ap.stdout!r}"
    )
    version_text = baseline.stdout.strip()
    assert version_text, "omnicraft version printed no stdout"
    assert version_text.startswith("omnicraft "), f"unexpected version output: {baseline.stdout!r}"
    after_prefix = version_text[len("omnicraft ") :]
    assert after_prefix and after_prefix[0].isdigit(), (
        f"unexpected version output: {baseline.stdout!r}"
    )
