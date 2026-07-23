"""Computer-control tool — let an agent drive the runner host's Mac.

A spec-opt-in builtin (``tools.builtins``) that gives an agent the screen,
pointer and keyboard of the machine the runner is on: capture the screen, click,
drag, type, press key combos, open apps and URLs. Execution is runner-local (see
``omnicraft/runner/tool_dispatch.py`` → ``omnicraft/runner/computer_control.py``),
shelling out to ``screencapture`` and ``cliclick``. This class exists for the
tool NAMESPACE and SCHEMA; its server-side ``invoke`` is a guard — the runner
intercepts the call before it is ever reached in production paths.

This is the highest-blast-radius builtin: it can click anything the signed-in
user can click, in any app. It is off unless a spec asks for it, and the shipped
policy makes every call go through per-action approval. Requires macOS with
``cliclick`` installed, plus the Screen Recording and Accessibility permissions
granted to the runner's process.
"""

from __future__ import annotations

from typing import Any

from omnicraft.tools.base import Tool, ToolContext

_RUNNER_ONLY = (
    "Erro: a ferramenta computer é executada pelo runner da sessão; "
    "este caminho de execução não a suporta."
)

_ACTIONS = [
    "screenshot",
    "click",
    "double_click",
    "right_click",
    "move",
    "drag",
    "type",
    "key",
    "open_app",
    "open_url",
]


class ComputerTool(Tool):
    """Drive the runner host's screen, pointer and keyboard."""

    @classmethod
    def name(cls) -> str:
        return "computer"

    @classmethod
    def description(cls) -> str:
        return (
            "Control the Mac this runner is on. Take a screenshot (saved to the "
            "workspace — read the file to see the screen), then click, "
            "double-click, right-click, move, drag, type text, or press a key "
            "combo, and open apps or URLs. Coordinates are pixels in the last "
            "screenshot; scaling to the display is handled for you. Every call "
            "needs the user's approval, so prefer a screenshot first and act in "
            "deliberate steps."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": _ACTIONS,
                            "description": "The action to perform on the computer.",
                        },
                        "x": {
                            "type": "integer",
                            "description": "X in screenshot pixels (pointer actions, drag start).",
                        },
                        "y": {
                            "type": "integer",
                            "description": "Y in screenshot pixels (pointer actions, drag start).",
                        },
                        "to_x": {"type": "integer", "description": "Drag end X, in pixels."},
                        "to_y": {"type": "integer", "description": "Drag end Y, in pixels."},
                        "text": {"type": "string", "description": "Text to type for 'type'."},
                        "keys": {
                            "type": "string",
                            "description": (
                                "Key combo for 'key', e.g. 'cmd+s', 'return', 'esc', "
                                "'page-down'. Use page-up/page-down or arrow-* to scroll."
                            ),
                        },
                        "app": {
                            "type": "string",
                            "description": "Application name for 'open_app', e.g. 'Safari'.",
                        },
                        "url": {"type": "string", "description": "URL for 'open_url'."},
                    },
                    "required": ["action"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        del arguments, ctx
        return _RUNNER_ONLY
