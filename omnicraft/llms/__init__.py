"""
Multi-provider LLM client with OpenAI Responses API interface.

Usage::

    from omnicraft.llms import Client

    client = Client()
    resp = client.responses.create(
        input=[{"role": "user", "content": "Hello"}],
        instructions="You are a helpful assistant.",
        model="anthropic/claude-sonnet-4-20250514",
    )
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnicraft.llms.client import Client
    from omnicraft.llms.context_window import get_model_context_window

__all__ = ["Client", "get_model_context_window"]


def __getattr__(name: str) -> Any:
    """Lazy attribute access to avoid a circular import.

    Eager top-level imports here pull in ``omnicraft.llms.client``,
    which imports ``omnicraft.reasoning_effort``, which imports
    ``omnicraft.llms.errors`` -- a submodule load that re-triggers
    this ``__init__`` mid-initialisation and explodes with
    ``cannot import name 'OPENAI_EFFORTS' from partially initialized
    module``. Lazy resolution at attribute access time defers the
    expensive imports until something actually needs ``Client`` or
    ``get_model_context_window``, after both modules have finished
    initialising.
    """
    if name == "Client":
        from omnicraft.llms.client import Client

        return Client
    if name == "get_model_context_window":
        from omnicraft.llms.context_window import get_model_context_window

        return get_model_context_window
    raise AttributeError(f"module 'omnicraft.llms' has no attribute {name!r}")
