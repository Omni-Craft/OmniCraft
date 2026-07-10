"""End-to-end test for ``examples/agents/secure_research_agent_os_env``.

``secure_research_agent`` + an ``os_env:`` sandbox. Same fake
research tools as the non-sandbox variant.

The YAML now ships with ``sandbox: type: none`` so the example
(and this test) run on macOS. Swap in ``linux_bwrap`` on a
Linux host to actually exercise the bwrap write-path
restriction the example is designed to demonstrate.

**What breaks if this fails:**
- Spec parser regresses on a YAML combining ``tools:``,
  ``policies:``, AND ``os_env:`` in the same agent.
- The ``sys_os_*`` builtins stop auto-registering on agents
  that already declare non-trivial ``tools:``.
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e.omnicraft._example_helpers import (
    assert_completed_one_shot,
    run_one_shot,
)
from tests.e2e.omnicraft.conftest import configure_mock_llm


def test_secure_research_agent_os_env_one_shot(
    omnicraft_python: Path,
    omnicraft_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Run the os_env variant one-shot cross-platform.

    Uses the mock LLM server for deterministic responses.

    :param omnicraft_python: Interpreter with omnicraft +
        openai-agents installed.
    :param omnicraft_repo_root: Repo root for subprocess cwd.
    :param mock_credentials_env: Mock-LLM env vars.
    :param mock_llm_server_url: Mock server URL for configuring
        response queues.
    """
    configure_mock_llm(mock_llm_server_url, [{"text": "OK"}])
    result = run_one_shot(
        omnicraft_python=omnicraft_python,
        omnicraft_repo_root=omnicraft_repo_root,
        omnicraft_credentials_env=mock_credentials_env,
        example_name="secure_research_agent_os_env",
        model="mock-model",
    )
    assert_completed_one_shot(result, "secure_research_agent_os_env")
