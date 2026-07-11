// Web Push subscription — closed-app approval notifications.
//
// Complements the page-scoped approval notification (which only fires while the
// app is alive): a subscription lets the server deliver the "Aprovar / Negar"
// notification through the browser's push service even when the PWA is closed.
// The service worker's `push` handler renders it (see sw-src/sw.js).
//
// No-ops inside the Electron desktop shell (it uses native OS notifications and
// is effectively always running), so this is a mobile/browser-PWA concern.

import { isNativeShell } from "@/lib/nativeBridge";
import { authenticatedFetch } from "@/lib/identity";

function urlBase64ToUint8Array(base64: string): Uint8Array {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const normalized = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(normalized);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) output[i] = raw.charCodeAt(i);
  return output;
}

function pushSupported(): boolean {
  return (
    !isNativeShell() &&
    typeof navigator !== "undefined" &&
    "serviceWorker" in navigator &&
    typeof window !== "undefined" &&
    "PushManager" in window
  );
}

/**
 * Ensure this browser is subscribed to Web Push for the current user. Safe to
 * call repeatedly — it reuses an existing subscription. Requires notification
 * permission to already be granted. Returns whether a subscription is active.
 */
export async function subscribeWebPush(): Promise<boolean> {
  if (!pushSupported()) return false;
  try {
    const registration = await navigator.serviceWorker.ready;
    const existing = await registration.pushManager.getSubscription();
    if (existing) {
      // Re-register with the server (idempotent) in case the store was reset.
      await authenticatedFetch("/v1/push/subscriptions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(existing.toJSON()),
      }).catch(() => {});
      return true;
    }
    const keyRes = await authenticatedFetch("/v1/push/vapid-public-key");
    if (!keyRes.ok) return false;
    const { key } = (await keyRes.json()) as { key: string };
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key) as BufferSource,
    });
    const res = await authenticatedFetch("/v1/push/subscriptions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(subscription.toJSON()),
    });
    return res.ok;
  } catch {
    return false;
  }
}

/** Remove this browser's Web Push subscription (server + local). */
export async function unsubscribeWebPush(): Promise<void> {
  if (!pushSupported()) return;
  try {
    const registration = await navigator.serviceWorker.ready;
    const subscription = await registration.pushManager.getSubscription();
    if (!subscription) return;
    await authenticatedFetch("/v1/push/subscriptions", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint: subscription.endpoint }),
    }).catch(() => {});
    await subscription.unsubscribe();
  } catch {
    // Best-effort.
  }
}
