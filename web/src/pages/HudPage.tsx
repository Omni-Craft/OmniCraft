/**
 * `/hud` — the floating HUD's route.
 *
 * Sits OUTSIDE the AppShell (like `/login` and `/approve`): the shell loads
 * the sidebar, conversation list and runner-health hooks, none of which a
 * 320px always-on-top strip has any use for. The page is transparent-friendly
 * and unpadded so the Electron window's own rounded card is the visible shape.
 *
 * The HUD renders in a SEPARATE Electron window, i.e. a fresh renderer with
 * its own module state — `resolveIdentity()` has to run here too before the
 * first feed fetch, or that fetch would go out without the viewer header.
 * Auth itself rides the same-origin HttpOnly cookie; nothing is passed in.
 */

import { useEffect, useState } from "react";
import { HudPanel } from "@/components/hud/HudPanel";
import { resolveIdentity } from "@/lib/identity";

export function HudPage() {
  const [identityReady, setIdentityReady] = useState(false);

  useEffect(() => {
    let cancelled = false;
    // A failed probe still unblocks the feed: header-mode servers have no
    // identity to resolve, and the feed fetch is the thing that will surface
    // a real auth problem.
    void resolveIdentity().finally(() => {
      if (!cancelled) setIdentityReady(true);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="h-screen w-screen overflow-hidden bg-transparent p-0">
      <HudPanel enabled={identityReady} />
    </div>
  );
}
