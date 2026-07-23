# iOS Simulator

Two pieces, one feature:

- The **`ios_simulator` builtin** lets an agent drive a simulator on the runner's
  Mac — list devices, boot one, build an Xcode scheme, install and launch an
  app, capture the screen, inject taps.
- The **Simulador pane** in the workspace rail shows that simulator live, so the
  user watches what the agent is doing.

Both run where Xcode is: the tool shells out on the **runner host**, and the
pane's screenshot route runs on the server (which, in the local desktop
deployment, is the same machine).

## Requirements

| Requirement | Why | How |
|-------------|-----|-----|
| Xcode | `simctl` and `xcodebuild` | App Store / developer.apple.com |
| An iOS runtime | Nothing can boot without one (~8 GB) | `xcodebuild -downloadPlatform iOS`, or Xcode → Settings → Components |
| `idb` | Tap/swipe/type — `simctl` has no touch injection | `brew install idb-companion && pipx install fb-idb` |

Without a runtime, `list` says so explicitly and the pane rests on its empty
state. Without `idb`, everything except `tap` / `swipe` / `type` still works.

## Actions

| Action | Arguments |
|--------|-----------|
| `list` | — |
| `boot` | `device` (name or UDID; required) |
| `shutdown`, `screenshot` | `device` (defaults to the booted one) |
| `build` | `scheme`, plus `project` or `workspace`, `configuration`, `device` |
| `install` | `app_path` |
| `launch`, `terminate` | `bundle_id` |
| `openurl` | `url` |
| `appearance` | `mode` (`light` / `dark`) |
| `tap`, `swipe` | `x`,`y` (and `x2`,`y2`) — needs `idb` |
| `type` | `text` — needs `idb` |

Screenshots are saved under `<workspace>/.omnicraft/ios/` and only the path is
returned; the model reads the file.

## Coordinates

Same shape as the computer tool: **screenshots are pixels, `idb ui tap` works in
points.** An iPhone 17 Pro captures at 1206×2622 but taps at 402×874 — a 3x
difference. The tool asks `idb describe` for the device's own dimensions and
scales, so coordinates read off the screenshot land where intended.

## The pane

The Simulador tab lives in the right workspace rail, next to Navegador, and is
**desktop-shell only** (a plain web build cannot reach a runner Mac's
simulator). It polls `GET /v1/sessions/{id}/ios/screenshot` roughly every 800 ms
and swaps the frame — the closest thing to a stream without an encoder pipeline.

The screen is drawn inside a phone chassis (rounded corners, Dynamic Island,
side buttons) so it reads as a device rather than a floating rectangle. Clicking
the image forwards a tap. Controls: pause/resume, capture, reload, close.

Routes: `/v1/sessions/{id}/ios/devices`, `.../screenshot`, `.../tap`.

## Gotchas

Things that will otherwise cost you an afternoon:

- **A screenshot right after `boot` fails** with `Error creating the image` —
  SpringBoard has not finished rendering. Wait for `simctl bootstatus`, or just
  retry a few seconds later.
- **A booted simulator eats disk fast.** On a machine that was already tight,
  booting one took free space from ~9 GB down to ~500 MB. `xcrun simctl shutdown
  all` alone may not give it back if simulator processes still hold deleted
  files — kill them, and `xcrun simctl erase all` reclaims the device data
  (1.5 GB → 192 MB in one measured case).
- **The runtime itself is ~8 GB** and lives in `/Library/Developer/CoreSimulator`.
  Budget disk before downloading; the download failing at 99% for lack of space
  is a real outcome.
- **`boot` may report a device needing erase** after an interrupted run. `xcrun
  simctl erase <udid>` then boot again.

## Limitations

- **macOS + Xcode only.**
- **The live view is a screenshot poll**, not video — roughly 3-5 fps. Real
  `recordVideo` → H.264 streaming is not implemented.
- **Tap is not verified end to end** — the scaling is unit-tested, but it has not
  been exercised against a live device because `idb` was not installed.

## Where the code lives

| Piece | File |
|-------|------|
| Execution (simctl/xcodebuild/idb) | `omnicraft/runner/ios_simulator.py` |
| Tool namespace + schema | `omnicraft/tools/builtins/ios_simulator.py` |
| Runner-local dispatch | `omnicraft/runner/tool_dispatch.py` (`_IOS_SIMULATOR_TOOLS`) |
| Pane routes | `omnicraft/server/routes/ios_simulator.py` |
| Pane | `web/src/components/SimulatorPane/SimulatorPane.tsx` |
| Shared shell helper | `omnicraft/runner/host_shell.py` |
