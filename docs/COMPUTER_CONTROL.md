# Computer control

The `computer` builtin gives an agent the screen, pointer and keyboard of the
machine its **runner** is on. Unlike the `browser_*` tools — which only reach
inside the desktop app's embedded web view — this drives the real Mac.

It is the highest-blast-radius tool in the tree: it can click anything the
signed-in user can click, in any application. Two things keep that in check:

- **Opt-in per agent spec.** Absent from `tools.builtins`, the tool does not
  exist for that agent. It is enabled nowhere by default.
- **Per-action approval, always.** Every single call parks for the user's
  approval via a guard the policy builder injects unconditionally — no spec can
  enable the tool and skip the gate. See
  [POLICIES.md → Always-on guards](POLICIES.md#always-on-guards).

## Requirements

| Requirement | Why | How |
|-------------|-----|-----|
| macOS | Uses `screencapture` and `cliclick` | — |
| `screencapture` | Screen capture | Ships with macOS |
| `cliclick` | Pointer/keyboard injection — macOS has no built-in CLI for it | `brew install cliclick` |
| **Screen Recording** permission | Otherwise captures show the desktop with no windows | System Settings → Privacy & Security → Screen Recording |
| **Accessibility** permission | Otherwise clicks and keystrokes are ignored | System Settings → Privacy & Security → Accessibility |

Without `cliclick`, `screenshot` still works and every input action returns an
actionable install hint instead of failing silently.

### Which process needs the permissions

The one that costs an hour if you get it wrong: macOS grants TCC permissions to
the process that **runs the command**, which is the **runner** — not the
OmniCraft server, and not the desktop app. If the runner is launched from a
terminal, the prompt (and the entry in System Settings) belongs to that terminal
or to its Python binary.

`GET /v1/doctor` has a `computer_control` check that reports `cliclick` presence
and restates this, so the Diagnóstico page can surface it.

## Actions

| Action | Arguments | Notes |
|--------|-----------|-------|
| `screenshot` | — | Saves a PNG under `<workspace>/.omnicraft/computer/` and returns the path. The image is never inlined — a raw screenshot in a tool result costs a fortune in tokens. |
| `click`, `double_click`, `right_click`, `move` | `x`, `y` | Screenshot pixels (see below). |
| `drag` | `x`, `y`, `to_x`, `to_y` | Emits press → move → release. |
| `type` | `text` | Types the literal text. |
| `key` | `keys` | A combo like `cmd+s`, or a named key like `return`, `esc`, `page-down`, `arrow-up`. |
| `open_app` | `app` | e.g. `Safari`. |
| `open_url` | `url` | Opens in the default browser. |

## Coordinates

**The model reasons in screenshot pixels; `cliclick` works in screen points.**
On a 2x Retina display those differ by a factor of two — a capture is 3420px
wide where the screen is 1710pt. Passing pixel coordinates straight through
would click at double the intended position.

The tool measures both once per process (`osascript` for the desktop bounds in
points, the capture's own dimensions for pixels) and scales every coordinate. So
you pass what you read off the screenshot and it lands where you meant.

If either measurement is unavailable the scale falls back to 1.0, which assumes
the caller already speaks points.

## What approval looks like

The guard turns each call into a sentence describing the action, not the call
shape:

> O agente quer clicar em (820, 410) no seu computador. Aprovar?

Long typed text is truncated so the approval card stays readable. The request
travels the existing elicitation path, so it appears in the notch island with
Approve / Reject, and in the web approval prompt.

## Limitations

- **macOS only.** There is no Linux or Windows path.
- **No native scroll.** `cliclick` does not expose a scroll verb; use `key` with
  `page-down` / `page-up` / `arrow-*` on the focused element.
- **Main display only.** Capture and the point/pixel measurement both assume the
  primary display; a multi-monitor setup is not handled.
- **Not verified end to end.** The screenshot path and the Retina scaling were
  checked on a real machine; the click/type path has not been exercised live,
  because it needs `cliclick` installed plus both TCC permissions granted.

## Where the code lives

| Piece | File |
|-------|------|
| Execution (shell-outs, scaling) | `omnicraft/runner/computer_control.py` |
| Tool namespace + schema | `omnicraft/tools/builtins/computer.py` |
| Runner-local dispatch | `omnicraft/runner/tool_dispatch.py` (`_COMPUTER_TOOLS`) |
| Approval guard | `omnicraft/policies/builtins/safety.py` (`ask_on_computer_use`) |
| Unconditional injection | `omnicraft/runtime/policies/builder.py` |
| Shared shell helper | `omnicraft/runner/host_shell.py` |
