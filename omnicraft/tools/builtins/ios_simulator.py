"""iOS Simulator tool — drive an Xcode simulator from the agent.

A single spec-opt-in builtin (``tools.builtins``) that lets an agent control an
iOS simulator on the runner host: list devices, boot one, build/install/launch
an app, capture the screen, and inject touches. Execution is runner-local (see
``omnicraft/runner/tool_dispatch.py`` → ``omnicraft/runner/ios_simulator.py``):
the runner shells out to ``xcrun simctl`` / ``xcodebuild`` directly, since that
is where Xcode lives. This class exists for the tool NAMESPACE and SCHEMA
(advertised via ``ToolManager``); its server-side ``invoke`` is a guard — the
runner intercepts the call before it is ever reached in production paths.

Requires a Mac with Xcode and at least one iOS runtime installed. Touch input
(tap/type/swipe) additionally needs ``idb`` (fb-idb); without it those actions
return an actionable install hint.
"""

from __future__ import annotations

from typing import Any

from omnicraft.tools.base import Tool, ToolContext

_RUNNER_ONLY = (
    "Erro: a ferramenta ios_simulator é executada pelo runner da sessão; "
    "este caminho de execução não a suporta."
)

_ACTIONS = [
    "list",
    "boot",
    "shutdown",
    "install",
    "launch",
    "terminate",
    "screenshot",
    "openurl",
    "appearance",
    "tap",
    "swipe",
    "type",
    "build",
]


class IosSimulatorTool(Tool):
    """Control an iOS simulator on the runner host (simctl/xcodebuild/idb)."""

    @classmethod
    def name(cls) -> str:
        return "ios_simulator"

    @classmethod
    def description(cls) -> str:
        return (
            "Control an iOS Simulator on the runner's Mac. Pick an action: "
            "list (devices/runtimes), boot/shutdown a device, build an Xcode "
            "scheme for the simulator, install a .app, launch/terminate by "
            "bundle id, screenshot the screen (saved to the workspace), open a "
            "URL/deep link, toggle light/dark appearance, and tap/swipe/type "
            "(these need idb installed). Runs on the machine with Xcode."
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
                            "description": "The simulator action to perform.",
                        },
                        "device": {
                            "type": "string",
                            "description": (
                                "Device name (e.g. 'iPhone 17 Pro') or UDID. "
                                "Required for boot; other actions default to the "
                                "booted device."
                            ),
                        },
                        "scheme": {"type": "string", "description": "Xcode scheme for 'build'."},
                        "project": {
                            "type": "string",
                            "description": ".xcodeproj path for 'build'.",
                        },
                        "workspace": {
                            "type": "string",
                            "description": ".xcworkspace path for build (instead of project).",
                        },
                        "configuration": {
                            "type": "string",
                            "description": "Build configuration for 'build' (default Debug).",
                        },
                        "app_path": {
                            "type": "string",
                            "description": ".app bundle path for 'install'.",
                        },
                        "bundle_id": {
                            "type": "string",
                            "description": "Bundle id for launch/terminate, e.g. com.acme.field.",
                        },
                        "url": {
                            "type": "string",
                            "description": "URL or deep link for 'openurl'.",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["light", "dark"],
                            "description": "Appearance for 'appearance'.",
                        },
                        "text": {"type": "string", "description": "Text to type for 'type'."},
                        "x": {"type": "integer", "description": "X for 'tap'."},
                        "y": {"type": "integer", "description": "Y for 'tap'."},
                        "x1": {"type": "integer", "description": "Start X for 'swipe'."},
                        "y1": {"type": "integer", "description": "Start Y for 'swipe'."},
                        "x2": {"type": "integer", "description": "End X for 'swipe'."},
                        "y2": {"type": "integer", "description": "End Y for 'swipe'."},
                    },
                    "required": ["action"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        del arguments, ctx
        return _RUNNER_ONLY
