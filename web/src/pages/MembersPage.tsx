/**
 * Admin members management page (``/settings/members``; the legacy
 * ``/members`` path redirects here). Rendered as a Settings sub-category.
 *
 * Lists every account on the server and lets admins:
 *
 * - Mint a single-use invite URL to share out-of-band.
 * - Reset a member's password (server generates a fresh random
 *   one and returns it exactly once — admin DMs it to the user).
 * - Remove a member entirely (cascades grants via the existing
 *   ``ON DELETE CASCADE`` on session_permissions).
 *
 * Gated on the client by an early "not an admin → render nothing"
 * check AND on the server by the route handlers themselves —
 * client-side gating is just UX so non-admins don't see useless
 * buttons; the server is what actually enforces.
 *
 * The "reset password" and "create invite" flows display the
 * sensitive value EXACTLY ONCE in a modal with a Copy button.
 * There is intentionally no way to retrieve them later — admins
 * who lose them just reset again. This matches the field
 * convention (GitLab, n8n, Coolify all do the same) and avoids
 * accidentally caching secrets in a list endpoint.
 */

import { useCallback, useEffect, useState } from "react";
import { CopyIcon, KeyRoundIcon, RefreshCwIcon, Trash2Icon, UserPlusIcon } from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  type AccountListEntry,
  type InviteCreated,
  type PasswordReset,
  createInvite,
  deleteUser,
  listUsers,
  resetUserPassword,
} from "@/lib/accountsApi";
import { getCurrentIsAdmin, resolveIdentity } from "@/lib/identity";
import { useServerInfo } from "@/lib/CapabilitiesContext";

export function MembersPage() {
  const info = useServerInfo();
  // Password-based management (invite / reset / remove) only exists in
  // accounts mode — OIDC identities are owned by the IdP, so under OIDC
  // this page is a read-only user list (no action column, no modals).
  const manageable = info !== "loading" && info.accounts_enabled;
  // Plain header/single-user mode: no auth endpoints exist. server_version
  // distinguishes a live single-user server from a failed /v1/info probe
  // (which uses the same accounts_enabled:false / login_url:null sentinel).
  const isSingleUser =
    info !== "loading" &&
    !info.accounts_enabled &&
    info.login_url === null &&
    info.server_version !== null;
  const [meIsAdmin, setMeIsAdmin] = useState<boolean | null>(null);
  const [meId, setMeId] = useState<string | null>(null);
  const [users, setUsers] = useState<AccountListEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Modal state (only one open at a time — keeps the render simple).
  const [inviteResult, setInviteResult] = useState<InviteCreated | null>(null);
  const [showCreateInvite, setShowCreateInvite] = useState(false);
  const [inviteAsAdmin, setInviteAsAdmin] = useState(false);
  const [resetResult, setResetResult] = useState<PasswordReset | null>(null);
  const [deleteCandidate, setDeleteCandidate] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const list = await listUsers();
    if (list === null) {
      setLoadError(
        "Não foi possível carregar os membros. Você pode não ter permissão de admin, ou o servidor está inacessível.",
      );
      setUsers([]);
      return;
    }
    setLoadError(null);
    setUsers(list);
  }, []);

  // Initial load: identity probe + members list. Skipped in single-user
  // mode since no auth endpoints exist. isSingleUser is a stable boolean
  // so it is safe as a dep without risking infinite re-renders.
  useEffect(() => {
    if (isSingleUser) return;
    void (async () => {
      const userId = await resolveIdentity();
      if (userId === null) {
        // Not authenticated — resolveIdentity already redirects to the
        // provider's login URL when one exists (OIDC/accounts). Nothing
        // more to do; leave the loading state.
        return;
      }
      setMeId(userId);
      const isAdmin = getCurrentIsAdmin();
      setMeIsAdmin(isAdmin);
      if (isAdmin) await refresh();
    })();
  }, [refresh, isSingleUser]);

  if (isSingleUser) {
    return (
      <div className="mx-auto w-full max-w-2xl px-6 py-12">
        <h1 className="mb-2 text-2xl font-semibold">Membros</h1>
        <p className="text-sm text-muted-foreground">
          O gerenciamento de membros não está disponível no modo de usuário único.
        </p>
      </div>
    );
  }

  // Pre-admin-check render: blank loading state. min-h-full so the
  // AppShell's outlet container governs height — we're a child view,
  // not a full-page replacement. min-h-full so the
  // AppShell's outlet container governs height — we're a child view,
  // not a full-page replacement.
  if (meIsAdmin === null) {
    return (
      <div className="flex min-h-full items-center justify-center text-sm text-muted-foreground">
        Carregando…
      </div>
    );
  }

  // Non-admin: hard stop. Server would also 403, this is just UX.
  if (meIsAdmin === false) {
    return (
      <div className="mx-auto w-full max-w-2xl px-6 py-12">
        <h1 className="mb-2 text-2xl font-semibold">Membros</h1>
        <p className="text-sm text-muted-foreground">
          Você não tem permissão para gerenciar membros.
        </p>
      </div>
    );
  }

  async function onCreateInvite() {
    setPendingAction(true);
    setActionError(null);
    const result = await createInvite(inviteAsAdmin);
    setPendingAction(false);
    if (!result.ok) {
      setActionError(result.error);
      return;
    }
    setShowCreateInvite(false);
    setInviteResult(result);
    setInviteAsAdmin(false);
    // Refresh the list — a new user will appear once the invite is
    // redeemed, but the count stays the same now.
  }

  async function onConfirmDelete() {
    if (deleteCandidate === null) return;
    setPendingAction(true);
    setActionError(null);
    const result = await deleteUser(deleteCandidate);
    setPendingAction(false);
    if (!result.ok) {
      setActionError(result.error);
      return;
    }
    setDeleteCandidate(null);
    await refresh();
  }

  async function onResetPassword(userId: string) {
    setPendingAction(true);
    setActionError(null);
    const result = await resetUserPassword(userId);
    setPendingAction(false);
    if (!result.ok) {
      setActionError(result.error);
      return;
    }
    setResetResult(result);
  }

  return (
    <PageScroll contentClassName="px-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Membros</h1>
        {/* Invite mints a password-backed account — accounts mode only.
        Under OIDC, accounts are provisioned by the IdP on first login, so
        there's nothing to invite here. */}
        {manageable && (
          <Button onClick={() => setShowCreateInvite(true)}>
            <UserPlusIcon /> Convidar membro
          </Button>
        )}
      </div>

      {!manageable && (
        <p className="mb-4 text-sm text-muted-foreground">
          Os usuários são provisionados automaticamente no primeiro login através do seu provedor de
          identidade. Esta lista é somente leitura.
        </p>
      )}

      {loadError !== null && (
        <div
          role="alert"
          className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {loadError}
        </div>
      )}

      {users !== null && users.length > 0 && (
        <div className="overflow-hidden rounded-md border border-border">
          <table className="w-full text-sm">
            <thead className="bg-muted/40 text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Usuário</th>
                <th className="px-3 py-2 font-medium">Função</th>
                <th className="px-3 py-2 font-medium">Último login</th>
                {manageable && <th className="px-3 py-2 text-right font-medium">Ações</th>}
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className="border-t border-border">
                  <td className="px-3 py-2 align-middle">
                    <span className="font-medium">{u.id}</span>
                    {u.id === meId && (
                      <span className="ml-2 text-xs text-muted-foreground">(você)</span>
                    )}
                    {!u.has_password && (
                      <Badge variant="outline" className="ml-2">
                        Externo
                      </Badge>
                    )}
                  </td>
                  <td className="px-3 py-2 align-middle">
                    {u.is_admin ? <Badge>Admin</Badge> : <Badge variant="secondary">Membro</Badge>}
                  </td>
                  <td className="px-3 py-2 align-middle text-muted-foreground">
                    {formatEpoch(u.last_login_at)}
                  </td>
                  {manageable && (
                    <td className="px-3 py-2 text-right">
                      <div className="flex justify-end gap-1">
                        <Button
                          variant="ghost"
                          size="xs"
                          title="Redefinir senha"
                          onClick={() => void onResetPassword(u.id)}
                          disabled={pendingAction || !u.has_password}
                        >
                          <KeyRoundIcon /> Redefinir
                        </Button>
                        <Button
                          variant="ghost"
                          size="xs"
                          title="Remover usuário"
                          onClick={() => setDeleteCandidate(u.id)}
                          disabled={pendingAction || u.id === meId}
                        >
                          <Trash2Icon /> Remover
                        </Button>
                      </div>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {users !== null && users.length === 0 && (
        <p className="text-sm text-muted-foreground">Nenhum membro ainda.</p>
      )}

      <div className="mt-3 flex items-center justify-end">
        <Button variant="ghost" size="sm" onClick={() => void refresh()}>
          <RefreshCwIcon /> Atualizar
        </Button>
      </div>

      {/* ── Create invite modal ───────────────────────────────────── */}
      <Dialog
        open={showCreateInvite}
        onOpenChange={(open) => {
          if (pendingAction) return;
          setShowCreateInvite(open);
          if (!open) setActionError(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Convidar um membro</DialogTitle>
            <DialogDescription>
              Uma URL de convite de uso único será criada. Compartilhe-a com a pessoa que você
              deseja adicionar. Ela escolherá seu próprio usuário e senha ao resgatá-la.
            </DialogDescription>
          </DialogHeader>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={inviteAsAdmin}
              onChange={(e) => setInviteAsAdmin(e.target.checked)}
              disabled={pendingAction}
            />
            Conceder privilégios de admin
          </label>
          {actionError !== null && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            >
              {actionError}
            </div>
          )}
          <DialogFooter>
            <Button
              variant="ghost"
              onClick={() => setShowCreateInvite(false)}
              disabled={pendingAction}
            >
              Cancelar
            </Button>
            <Button onClick={() => void onCreateInvite()} disabled={pendingAction}>
              {pendingAction ? "Criando…" : "Criar convite"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Invite-created modal (shows the URL exactly once) ─────── */}
      <Dialog
        open={inviteResult !== null}
        onOpenChange={(open) => {
          if (!open) setInviteResult(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>URL de convite</DialogTitle>
            <DialogDescription>
              Envie esta URL para o novo membro. Ela expira em{" "}
              {formatTtl(inviteResult?.expires_at)} e é de uso único — depois de resgatada, não
              pode ser usada novamente. Esta URL é mostrada apenas uma vez.
            </DialogDescription>
          </DialogHeader>
          {inviteResult !== null && <CopyableValue value={rebaseUrl(inviteResult.register_url)} />}
          <DialogFooter>
            <Button onClick={() => setInviteResult(null)}>Concluído</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Reset password modal (shows the new password once) ───── */}
      <Dialog
        open={resetResult !== null}
        onOpenChange={(open) => {
          if (!open) setResetResult(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Nova senha para {resetResult?.id}</DialogTitle>
            <DialogDescription>
              Envie esta senha ao usuário por outro canal (por exemplo, DM no Slack). Ela é
              mostrada apenas uma vez.
            </DialogDescription>
          </DialogHeader>
          {resetResult !== null && <CopyableValue value={resetResult.new_password} />}
          <DialogFooter>
            <Button onClick={() => setResetResult(null)}>Concluído</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Delete confirmation ────────────────────────────────── */}
      <Dialog
        open={deleteCandidate !== null}
        onOpenChange={(open) => {
          if (pendingAction) return;
          if (!open) {
            setDeleteCandidate(null);
            setActionError(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Remover {deleteCandidate}?</DialogTitle>
            <DialogDescription>
              Isso exclui a conta do usuário e revoga todas as suas permissões de sessão. As
              sessões que ele possui ficam inacessíveis, a menos que outro usuário tenha direitos
              de gerenciamento sobre elas. Esta ação não pode ser desfeita.
            </DialogDescription>
          </DialogHeader>
          {actionError !== null && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            >
              {actionError}
            </div>
          )}
          <DialogFooter>
            <Button
              variant="ghost"
              onClick={() => setDeleteCandidate(null)}
              disabled={pendingAction}
            >
              Cancelar
            </Button>
            <Button
              variant="destructive"
              onClick={() => void onConfirmDelete()}
              disabled={pendingAction}
            >
              {pendingAction ? "Removendo…" : "Remover"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </PageScroll>
  );
}

/**
 * A read-only field paired with a one-click copy button.
 *
 * Used for both invite URLs and reset-issued passwords; both are
 * single-use sensitive values that need a frictionless copy path
 * since the user typically pastes them into Slack within seconds.
 */
function CopyableValue({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // No clipboard permission — the input is still selectable.
    }
  };
  return (
    <div className="flex items-center gap-2">
      <Input
        value={value}
        readOnly
        className="font-mono text-xs"
        onFocus={(e) => e.currentTarget.select()}
      />
      <Button variant="outline" size="sm" onClick={() => void onCopy()} aria-label="Copiar">
        <CopyIcon /> {copied ? "Copiado" : "Copiar"}
      </Button>
    </div>
  );
}

/**
 * Replace the origin of a server-returned URL with the current browser
 * origin so invite links work regardless of how the admin reached the
 * app (e.g. via a reverse proxy or non-loopback address).
 */
function rebaseUrl(serverUrl: string): string {
  try {
    const parsed = new URL(serverUrl);
    return `${window.location.origin}${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return serverUrl;
  }
}

function formatEpoch(epoch: number | null): string {
  if (epoch === null) return "Nunca";
  const d = new Date(epoch * 1000);
  return d.toLocaleString();
}

function formatTtl(expiresAt: number | undefined): string {
  if (expiresAt === undefined) return "soon";
  const secs = Math.max(0, expiresAt - Math.floor(Date.now() / 1000));
  const hours = Math.round(secs / 3600);
  if (hours >= 1) return `${hours}h`;
  return `${Math.max(1, Math.round(secs / 60))}m`;
}
