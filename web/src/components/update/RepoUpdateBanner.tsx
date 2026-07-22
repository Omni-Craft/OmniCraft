import { useState } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { useRepoUpdate } from "./useRepoUpdate";

/**
 * Repository-update banner.
 *
 * Distinct from the PWA banner, which reloads the page into fresh frontend
 * assets. This one fires when the git checkout the server runs from has moved
 * ahead of the running process — new code was pulled or committed under an
 * editable install. Applying it needs a server RESTART, which a web page
 * cannot do, so the banner states the action rather than offering a button
 * that would only reload stale code. It clears itself: once the server
 * restarts on the new commit, the next poll reports no update and it vanishes.
 */
export function RepoUpdateBanner() {
  const { update_available } = useRepoUpdate();
  const [dismissed, setDismissed] = useState(false);

  if (!update_available || dismissed) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "fixed inset-x-0 top-0 z-[100] flex items-center justify-center gap-3 px-4 py-3",
        "border-b border-border bg-background/95 backdrop-blur",
        "supports-[backdrop-filter]:bg-background/80",
      )}
    >
      <span className="text-sm text-foreground">
        Atualização disponível — reinicie o OmniCraft para aplicar.
      </span>
      <Button size="sm" variant="ghost" onClick={() => setDismissed(true)}>
        Dispensar
      </Button>
    </div>
  );
}
