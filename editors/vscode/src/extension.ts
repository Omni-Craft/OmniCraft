/**
 * OmniCraft VS Code extension entry point (minimal iframe-only build).
 *
 * activate() wires:
 *  - Config / local-server discovery
 *  - A minimal Sessions/home tree view (so the activity-bar icon renders) whose
 *    welcome content offers an "Open OmniCraft" button
 *  - EditorPanelController: the single editor-beside iframe surface
 *  - The omnicraft.open command
 */
import * as vscode from "vscode";
import { discoverLocalServer, DEFAULT_HEALTH_TIMEOUT_MS } from "./discovery";
import { resolveServerTarget } from "./config";
import { readSettings } from "./config/vscodeSettings";
import { EditorPanelController } from "./panel/EditorPanelController";
import { registerOpenPanel } from "./commands/openPanel";

/** Id of the minimal activity-bar view (declared in package.json contributes.views). */
const HOME_VIEW_ID = "omnicraft.home";

let output: vscode.OutputChannel | undefined;
let controller: EditorPanelController | undefined;

/**
 * A no-op tree provider. A `viewsContainer` only renders its activity-bar icon
 * when it has at least one registered view; this provides that view. The actual
 * call-to-action is the `viewsWelcome` "Open OmniCraft" button in package.json.
 */
class HomeTreeProvider implements vscode.TreeDataProvider<never> {
  getTreeItem(element: never): vscode.TreeItem {
    return element;
  }
  getChildren(): never[] {
    return [];
  }
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  output = vscode.window.createOutputChannel("OmniCraft");
  context.subscriptions.push(output);
  output.appendLine("[omnicraft] activating");

  // ── Single editor-beside iframe surface ───────────────────────────────────
  controller = new EditorPanelController(context.extensionUri, output);

  // ── Minimal activity-bar view (makes the container icon render) ────────────
  context.subscriptions.push(
    vscode.window.registerTreeDataProvider(HOME_VIEW_ID, new HomeTreeProvider()),
  );

  // ── omnicraft.open command ──────────────────────────────────────────────────
  registerOpenPanel(context, controller);

  // ── Resolve the local server at activation ────────────────────────────────
  try {
    const settings = readSettings();
    const discovery = await discoverLocalServer(undefined, DEFAULT_HEALTH_TIMEOUT_MS);
    const resolution = resolveServerTarget(settings, {
      found: discovery.found,
      baseUrl: discovery.found ? discovery.baseUrl : undefined,
      health: discovery.found ? discovery.health : undefined,
    });

    if (resolution.status === "resolved") {
      const target = resolution.target;
      controller.setResolved(target);
      output.appendLine(
        `[omnicraft] target: ${target.baseUrl} (hostType=${target.hostType}, source=${target.source})`,
      );
    } else {
      output.appendLine(
        `[omnicraft] no local server (${resolution.reason}); start \`omnicraft server\` or set omnicraft.serverUrl to a localhost URL`,
      );
    }
  } catch (err) {
    output.appendLine(
      `[omnicraft] init error: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  output.appendLine("[omnicraft] ready");
}

export function deactivate(): void {
  controller?.dispose();
  controller = undefined;
  output?.appendLine("[omnicraft] deactivating");
  output = undefined;
}
