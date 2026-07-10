"""End-to-end test for ``examples/rate_limited_search_agent.yaml``.

The example wires a :class:`FunctionPolicy` (loaded from
``tests.resources.examples._shared.search_rate_limit_policy``) that caps tool-call counts
per turn. The policy runs in the tool-call phase of the agent
loop — every turn exercises the policy's pre/post-tool hooks
regardless of whether the cap is actually hit.

**What breaks if this fails:**
- Spec parser regresses on ``policies:`` entries with
  ``type: function`` + agent-local ``callable:`` paths.
- The FunctionPolicy tool-call phase hook stops firing on agents
  declared via YAML.
- The per-agent duplicated helper (``search_rate_limit_policy.py``
  copied into the example dir during unification) stops resolving
  via ``tests.resources.examples._shared.search_rate_limit_policy`` dotted lookup.
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e.omnicraft._example_helpers import (
    assert_completed_one_shot,
    run_one_shot,
)
from tests.e2e.omnicraft.conftest import configure_mock_llm

# Low-token summary request so the policy's rate limit stays
# comfortably unrehced — the goal is to exercise the hook, not
# to trigger the cap.
_PROMPT = "Summarize in one sentence: the sky is blue."


def test_rate_limited_search_agent_one_shot(
    omnicraft_python: Path,
    omnicraft_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    """
    Run the rate-limited search agent one-shot. The FunctionPolicy
    registers + runs its pre-turn hook before the LLM returns.

    Uses the mock LLM server for deterministic responses.

    :param omnicraft_python: Interpreter with omnicraft +
        openai-agents installed.
    :param omnicraft_repo_root: Repo root for subprocess cwd.
    :param mock_credentials_env: Mock-LLM env vars.
    :param mock_llm_server_url: Mock server URL for configuring
        response queues.
    """
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "The sky is blue due to Rayleigh scattering of sunlight."}],
    )
    result = run_one_shot(
        omnicraft_python=omnicraft_python,
        omnicraft_repo_root=omnicraft_repo_root,
        omnicraft_credentials_env=mock_credentials_env,
        example_name="rate_limited_search_agent",
        prompt=_PROMPT,
        model="mock-model",
    )
    assert_completed_one_shot(result, "rate_limited_search_agent")
