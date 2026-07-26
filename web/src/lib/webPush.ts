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
  return pushBlocker() === null;
}

/**
 * Why Web Push can't run here, or ``null`` when it can.
 *
 * Every failure in this file is swallowed, so a phone that silently never
 * subscribes leaves nothing to look at — the reason has to be readable in the
 * UI. On iOS especially, the APIs simply don't exist until the page is
 * launched from a Home Screen icon, which is impossible to guess from the
 * outside.
 */
export function pushBlocker(): string | null {
  if (isNativeShell()) return "O app nativo usa as notificações do sistema.";
  if (typeof navigator === "undefined" || typeof window === "undefined") {
    return "Sem ambiente de navegador.";
  }
  if (!("serviceWorker" in navigator)) {
    return "Este navegador não tem service worker.";
  }
  if (!("PushManager" in window)) {
    // The iOS giveaway: Safari only exposes PushManager to an installed PWA.
    return isAppleMobile()
      ? "No iPhone, abra pelo ícone na Tela de Início (Compartilhar ▸ Adicionar à Tela de Início). Numa aba do Safari o push não existe."
      : "Este navegador não suporta Web Push.";
  }
  if (!window.isSecureContext) return "É preciso acessar por https.";
  return null;
}

function isAppleMobile(): boolean {
  return /iPhone|iPad|iPod/i.test(navigator.userAgent ?? "");
}

/** A short, human-readable state of Web Push on this device. */
export async function pushStatus(): Promise<{ active: boolean; reason: string }> {
  const blocker = pushBlocker();
  if (blocker) return { active: false, reason: blocker };
  const permission = typeof Notification !== "undefined" ? Notification.permission : "default";
  if (permission === "denied") {
    return { active: false, reason: "Notificações bloqueadas nos ajustes do sistema." };
  }
  if (permission !== "granted") {
    return {
      active: false,
      reason: "Falta permitir notificações — toque na tela para o pedido aparecer.",
    };
  }
  try {
    const registration = await navigator.serviceWorker.ready;
    const existing = await registration.pushManager.getSubscription();
    return existing
      ? { active: true, reason: "Push ativo neste aparelho." }
      : { active: false, reason: "Permissão concedida, mas a inscrição ainda não foi criada." };
  } catch (error) {
    return { active: false, reason: `Falha ao ler a inscrição: ${String(error)}` };
  }
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
