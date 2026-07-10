"""List all built-in tools available in OmniCraft.

Returns the live registry of builtin tool names and their
descriptions, so the onboarding assistant always recommends
from the current set — not a stale hardcoded list.

Each tool class is imported individually from its own module to
avoid importing the ``omnicraft.tools.builtins`` package (which
transitively pulls in modules that conflict with the ``mcp`` pip
package in subprocess environments).
"""

from omnicraft_client import tool

# Maps every builtin tool name to (module_path, class_name).
# This is the sole source of truth — when a new builtin is added,
# add it here. Each module is imported individually to avoid the
# transitive import chain from omnicraft.tools.builtins.__init__.
_TOOL_CLASSES: dict[str, tuple[str, str]] = {
    "download_file": ("omnicraft.tools.builtins.download_file", "DownloadFileTool"),
    "export_agent": ("omnicraft.tools.builtins.export_agent", "ExportAgentTool"),
    "hindsight_recall": ("omnicraft.tools.builtins.hindsight", "HindsightRecallTool"),
    "hindsight_reflect": ("omnicraft.tools.builtins.hindsight", "HindsightReflectTool"),
    "hindsight_retain": ("omnicraft.tools.builtins.hindsight", "HindsightRetainTool"),
    "list_files": ("omnicraft.tools.builtins.list_files", "ListFilesTool"),
    "search_conversations": (
        "omnicraft.tools.builtins.search_conversations",
        "SearchConversationsTool",
    ),
    "upload_file": ("omnicraft.tools.builtins.upload_file", "UploadFileTool"),
    "web_fetch": ("omnicraft.tools.builtins.web_fetch", "WebFetchTool"),
    "web_search": ("omnicraft.tools.builtins.web_search", "WebSearchTool"),
}


@tool
def list_builtin_tools() -> str:
    """
    List all built-in tools available in OmniCraft.

    Returns tool names and descriptions. Call this before
    recommending tools for a new agent.
    """
    import importlib

    lines: list[str] = []
    for name in sorted(_TOOL_CLASSES):
        module_path, class_name = _TOOL_CLASSES[name]
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        lines.append(f"- {name}: {cls.description()}")

    return "\n".join(lines)
