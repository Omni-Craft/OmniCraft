"""Embedded-browser tools — drive the desktop app's Navegador pane.

Spec-opt-in builtins (``tools.builtins``) that let an agent navigate,
snapshot, click, type into and screenshot the conversation's embedded
Electron browser. Execution is runner-local (see
``omnicraft/runner/tool_dispatch.py``): the runner POSTs the action to
``/v1/sessions/{id}/browser/actions`` and the web UI's relay drives the
actual WebContentsView. These classes exist for the tool NAMESPACE and
SCHEMAS (advertised via ``ToolManager``); their server-side ``invoke`` is
a guard — the runner intercepts the calls before it is ever reached in
production paths.

Requires the desktop app open on the conversation; with no renderer the
action times out with an actionable error (no headless fallback — use the
Playwright MCP for headless work).
"""

from __future__ import annotations

from typing import Any

from omnicraft.tools.base import Tool, ToolContext

_RUNNER_ONLY = (
    "Erro: as ferramentas browser_* são executadas pelo runner da sessão; "
    "este caminho de execução não as suporta."
)

# Shared param fragments — click/type accept EITHER a snapshot ref OR a CSS
# selector, plus the snapshot_id for precise staleness errors.
_TARGET_PROPS: dict[str, Any] = {
    "ref": {
        "type": "integer",
        "description": "Element ref from the latest browser_snapshot, e.g. 12.",
    },
    "selector": {
        "type": "string",
        "description": 'CSS selector, used when no ref is given, e.g. "#submit".',
    },
    "snapshot_id": {
        "type": "string",
        "description": "snapshot_id from browser_snapshot — detects stale refs precisely.",
    },
}


def _schema(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


class BrowserNavigateTool(Tool):
    """Navigate the conversation's embedded browser pane to a URL."""

    @classmethod
    def name(cls) -> str:
        return "browser_navigate"

    @classmethod
    def description(cls) -> str:
        return (
            "Open a URL in the conversation's embedded browser pane (the "
            "Navegador tab the user can watch). Requires the desktop app "
            "open on this conversation. After navigating, call "
            "browser_snapshot to see the page."
        )

    def get_schema(self) -> dict[str, Any]:
        return _schema(
            self.name(),
            self.description(),
            {"url": {"type": "string", "description": "Absolute URL, e.g. http://localhost:3000"}},
            ["url"],
        )

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        del arguments, ctx
        return _RUNNER_ONLY


class BrowserSnapshotTool(Tool):
    """Accessibility-style snapshot of the embedded browser page."""

    @classmethod
    def name(cls) -> str:
        return "browser_snapshot"

    @classmethod
    def description(cls) -> str:
        return (
            "Capture an accessibility-style outline of the page in the "
            "embedded browser pane, with stable [ref=N] ids you can pass "
            "to browser_click / browser_type. Call again after the page "
            "changes — refs go stale."
        )

    def get_schema(self) -> dict[str, Any]:
        return _schema(self.name(), self.description(), {}, [])

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        del arguments, ctx
        return _RUNNER_ONLY


class BrowserScreenshotTool(Tool):
    """Screenshot of the embedded browser pane, saved into the workspace."""

    @classmethod
    def name(cls) -> str:
        return "browser_screenshot"

    @classmethod
    def description(cls) -> str:
        return (
            "Take a screenshot of the embedded browser pane. The image is "
            "saved as a PNG file in the session workspace and the path is "
            "returned — read the file to view it."
        )

    def get_schema(self) -> dict[str, Any]:
        return _schema(self.name(), self.description(), {}, [])

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        del arguments, ctx
        return _RUNNER_ONLY


class BrowserClickTool(Tool):
    """Click an element in the embedded browser pane."""

    @classmethod
    def name(cls) -> str:
        return "browser_click"

    @classmethod
    def description(cls) -> str:
        return (
            "Click an element in the embedded browser pane, addressed by a "
            "browser_snapshot ref (preferred) or a CSS selector."
        )

    def get_schema(self) -> dict[str, Any]:
        return _schema(self.name(), self.description(), dict(_TARGET_PROPS), [])

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        del arguments, ctx
        return _RUNNER_ONLY


class BrowserTypeTool(Tool):
    """Type text into an element in the embedded browser pane."""

    @classmethod
    def name(cls) -> str:
        return "browser_type"

    @classmethod
    def description(cls) -> str:
        return (
            "Set the value of an input/textarea in the embedded browser "
            "pane (fires input/change events), addressed by a "
            "browser_snapshot ref (preferred) or a CSS selector."
        )

    def get_schema(self) -> dict[str, Any]:
        props = dict(_TARGET_PROPS)
        props["text"] = {"type": "string", "description": "The text to set."}
        return _schema(self.name(), self.description(), props, ["text"])

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        del arguments, ctx
        return _RUNNER_ONLY
