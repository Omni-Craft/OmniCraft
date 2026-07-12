/**
 * Admin default-policies management page (``/settings/policies``; the legacy
 * ``/policies`` path redirects here). Rendered as a Settings sub-category.
 *
 * Lists every global default policy and lets admins add, toggle,
 * and remove them. The add-policy dialog reuses the same registry-
 * driven picker as the per-session policy UI in AgentInfo.
 *
 * Gated on the client by an early admin check (non-admins see a
 * "no permission" message) AND on the server by the route handlers
 * themselves — client-side gating is just UX.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  FlaskConicalIcon,
  PlusIcon,
  RefreshCwIcon,
  ShieldCheckIcon,
  TrashIcon,
  XIcon,
} from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { ModelValueCombobox } from "@/components/ModelValueCombobox";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import {
  useDefaultPolicies,
  useAddDefaultPolicy,
  useUpdateDefaultPolicy,
  useDeleteDefaultPolicy,
  type DefaultPolicy,
} from "@/hooks/useDefaultPolicies";
import { usePolicyRegistry, type PolicyRegistryEntry } from "@/hooks/usePolicies";
import { CLAUDE_NATIVE_MODELS } from "@/lib/claudeNativeModels";
import { authenticatedFetch, getCurrentIsAdmin, resolveIdentity } from "@/lib/identity";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { coercePolicyParams } from "@/lib/policyParams";

// ---------------------------------------------------------------------------
// Add-policy dialog (registry-driven, same UX as session policies)
// ---------------------------------------------------------------------------

function AddDefaultPolicyDialog({
  registry,
  appliedHandlers,
  open,
  onOpenChange,
}: {
  registry: PolicyRegistryEntry[];
  appliedHandlers: Set<string>;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [selected, setSelected] = useState<string>("");
  const [filter, setFilter] = useState("");
  const [policyName, setPolicyName] = useState<string>("");
  const [factoryParams, setFactoryParams] = useState<Record<string, string>>({});
  const [paramError, setParamError] = useState<string | null>(null);
  const addPolicy = useAddDefaultPolicy();

  const entry = registry.find((r) => r.handler === selected);
  const rawSchema = entry?.params_schema as
    | {
        properties?: Record<
          string,
          {
            type?: string;
            description?: string;
            default?: unknown;
            enum?: string[];
            items?: { type?: string; enum?: string[]; "x-enum-source"?: string };
            uniqueItems?: boolean;
          }
        >;
        required?: string[];
      }
    | null
    | undefined;
  const modelIds = useMemo(() => CLAUDE_NATIVE_MODELS.map((m) => m.id), []);
  const properties = useMemo(() => {
    const props = rawSchema?.properties ?? {};
    if (!modelIds.length) return props;
    const enriched: typeof props = {};
    for (const [key, prop] of Object.entries(props)) {
      if (prop.items?.["x-enum-source"] === "models" && !prop.items.enum) {
        enriched[key] = { ...prop, items: { ...prop.items, enum: modelIds } };
      } else {
        enriched[key] = prop;
      }
    }
    return enriched;
  }, [rawSchema?.properties, modelIds]);
  const paramKeys = Object.keys(properties);

  function handleSelect(handler: string) {
    const e = registry.find((r) => r.handler === handler);
    setSelected(handler);
    setFilter("");
    setPolicyName(e ? e.name.toLowerCase().replace(/\s+/g, "_") : "");
    setFactoryParams({});
    setParamError(null);
  }

  function handleAdd() {
    if (!entry) return;
    let parsedParams: Record<string, unknown> | undefined;
    if (entry.kind === "factory" && paramKeys.length > 0) {
      const result = coercePolicyParams(paramKeys, properties, factoryParams);
      if (!result.ok) {
        setParamError(result.error);
        return;
      }
      parsedParams = result.params;
    }
    setParamError(null);
    const includeFactoryParams =
      entry.kind === "factory" ? { factory_params: parsedParams ?? {} } : {};
    addPolicy.mutate(
      {
        name: policyName || entry.name.toLowerCase().replace(/\s+/g, "_"),
        type: "python",
        handler: entry.handler,
        ...includeFactoryParams,
      },
      {
        onSuccess: () => {
          setSelected("");
          setPolicyName("");
          setFactoryParams({});
          onOpenChange(false);
        },
      },
    );
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Reset to the policy list on close so reopening never lands mid-config.
        if (!next) {
          setSelected("");
          setPolicyName("");
          setFactoryParams({});
          setParamError(null);
        }
        onOpenChange(next);
      }}
    >
      <DialogContent className="max-h-[80vh] overflow-y-auto sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Adicionar política global</DialogTitle>
          <DialogDescription>
            Escolha uma política para aplicar globalmente a todas as sessões.
          </DialogDescription>
        </DialogHeader>
        <div className="min-w-0 space-y-3 pt-1">
          {!selected &&
            (() => {
              const available = registry.filter((r) => !appliedHandlers.has(r.handler));
              const lowerFilter = filter.toLowerCase();
              const filtered = lowerFilter
                ? available.filter(
                    (r) =>
                      r.name.toLowerCase().includes(lowerFilter) ||
                      r.description?.toLowerCase().includes(lowerFilter),
                  )
                : available;
              return (
                <>
                  <input
                    type="text"
                    value={filter}
                    onChange={(e) => setFilter(e.target.value)}
                    placeholder="Filtrar políticas..."
                    className="w-full rounded border border-border bg-background px-2 py-1.5 text-sm placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-ring"
                    // eslint-disable-next-line jsx-a11y/no-autofocus
                    autoFocus
                  />
                  <div className="flex max-h-52 flex-col divide-y divide-border overflow-y-auto rounded border border-border">
                    {filtered.map((r) => (
                      <button
                        key={r.handler}
                        type="button"
                        onClick={() => handleSelect(r.handler)}
                        className="flex flex-col gap-0.5 px-2.5 py-2 text-left hover:bg-muted"
                      >
                        <span className="text-sm">{r.name}</span>
                        {r.description && (
                          <span className="line-clamp-2 text-[11px] text-muted-foreground">
                            {r.description}
                          </span>
                        )}
                      </button>
                    ))}
                    {filtered.length === 0 && (
                      <p className="py-2 text-center text-xs text-muted-foreground">
                        {available.length === 0
                          ? "Todas as políticas disponíveis já foram aplicadas."
                          : "Nenhuma política corresponde ao seu filtro."}
                      </p>
                    )}
                  </div>
                </>
              );
            })()}
          {entry && (
            <div className="flex flex-col gap-1 rounded border border-border bg-muted/50 px-2.5 py-2">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">{entry.name}</span>
                <button
                  type="button"
                  onClick={() => {
                    setSelected("");
                    setPolicyName("");
                    setFactoryParams({});
                    setParamError(null);
                  }}
                  className="text-[11px] text-muted-foreground hover:text-foreground"
                >
                  Alterar
                </button>
              </div>
              {entry.description && (
                <p className="text-xs text-muted-foreground">{entry.description}</p>
              )}
            </div>
          )}
          {entry && (
            <div>
              <label className="flex items-center gap-1 text-xs text-muted-foreground">
                <span className="font-medium text-foreground">nome</span>
              </label>
              <input
                type="text"
                value={policyName}
                onChange={(e) => setPolicyName(e.target.value)}
                className="mt-0.5 w-full rounded border border-border bg-background px-2 py-1.5 text-sm"
              />
            </div>
          )}
          {entry?.kind === "factory" && paramKeys.length > 0 && (
            <div className="space-y-2">
              {paramKeys.map((key) => {
                const prop = properties[key];
                return (
                  <div key={key}>
                    <label className="flex items-center gap-1 text-xs text-muted-foreground">
                      <span className="font-medium text-foreground">{key}</span>
                      {prop?.type && (
                        <span>
                          (
                          {prop.type === "array" && prop.items?.enum
                            ? "seleção múltipla"
                            : prop.type === "array"
                              ? "separado por vírgulas"
                              : prop.type}
                          )
                        </span>
                      )}
                    </label>
                    {prop?.description && (
                      <p className="break-all text-[11px] text-muted-foreground">
                        {prop.description}
                      </p>
                    )}
                    {prop?.type === "boolean" ? (
                      <select
                        value={
                          factoryParams[key] ??
                          (prop?.default !== undefined ? String(prop.default) : "")
                        }
                        onChange={(e) =>
                          setFactoryParams((prev) => ({
                            ...prev,
                            [key]: e.target.value,
                          }))
                        }
                        className="mt-0.5 w-full rounded border border-border bg-background px-2 py-1.5 text-sm"
                      >
                        <option value="true">true</option>
                        <option value="false">false</option>
                      </select>
                    ) : prop?.type === "string" && prop.enum ? (
                      <select
                        value={
                          factoryParams[key] ??
                          (prop?.default !== undefined
                            ? String(prop.default)
                            : (prop.enum[0] ?? ""))
                        }
                        onChange={(e) =>
                          setFactoryParams((prev) => ({
                            ...prev,
                            [key]: e.target.value,
                          }))
                        }
                        className="mt-0.5 w-full rounded border border-border bg-background px-2 py-1.5 text-sm"
                      >
                        {prop.enum.map((v) => (
                          <option key={v} value={v}>
                            {v}
                          </option>
                        ))}
                      </select>
                    ) : prop?.type === "array" && prop.items?.enum ? (
                      (() => {
                        const current = factoryParams[key]
                          ? factoryParams[key].split(",").filter(Boolean)
                          : Array.isArray(prop?.default)
                            ? (prop.default as string[])
                            : [];
                        return (
                          <div className="mt-0.5 space-y-1.5">
                            {current.length > 0 && (
                              <div className="flex flex-wrap gap-1">
                                {current.map((v) => (
                                  <span
                                    key={v}
                                    className="inline-flex items-center gap-0.5 rounded-md bg-muted px-1.5 py-0.5 text-xs"
                                  >
                                    {v}
                                    <button
                                      type="button"
                                      onClick={() => {
                                        const next = current.filter((x) => x !== v);
                                        setFactoryParams((prev) => ({
                                          ...prev,
                                          [key]: next.join(","),
                                        }));
                                      }}
                                      className="ml-0.5 text-muted-foreground hover:text-foreground"
                                    >
                                      <XIcon className="size-3" />
                                    </button>
                                  </span>
                                ))}
                              </div>
                            )}
                            <ModelValueCombobox
                              options={prop.items.enum}
                              selected={current}
                              onToggle={(v) => {
                                const next = current.includes(v)
                                  ? current.filter((x) => x !== v)
                                  : [...current, v];
                                setFactoryParams((prev) => ({
                                  ...prev,
                                  [key]: next.join(","),
                                }));
                              }}
                            />
                          </div>
                        );
                      })()
                    ) : (
                      <input
                        type={
                          prop?.type === "integer" || prop?.type === "number" ? "number" : "text"
                        }
                        placeholder={
                          prop?.type === "array"
                            ? prop?.default !== undefined
                              ? (prop.default as string[]).join(", ")
                              : "valores separados por vírgula"
                            : prop?.default !== undefined
                              ? String(prop.default)
                              : ""
                        }
                        value={factoryParams[key] ?? ""}
                        onChange={(e) =>
                          setFactoryParams((prev) => ({
                            ...prev,
                            [key]: e.target.value,
                          }))
                        }
                        className="mt-0.5 w-full rounded border border-border bg-background px-2 py-1.5 text-sm"
                      />
                    )}
                  </div>
                );
              })}
            </div>
          )}
          {(paramError || addPolicy.isError) && (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            >
              {paramError ?? addPolicy.error?.message}
            </div>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={() => {
                // With a policy selected, Cancel steps back to the list so the
                // user can pick another; only close the dialog from the list.
                if (selected) {
                  setSelected("");
                  setFactoryParams({});
                  setParamError(null);
                } else {
                  onOpenChange(false);
                }
              }}
              className="rounded px-3 py-1.5 text-xs hover:bg-muted"
            >
              Cancelar
            </button>
            <button
              type="button"
              onClick={handleAdd}
              disabled={!selected || addPolicy.isPending}
              className="rounded bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-50"
            >
              {addPolicy.isPending ? "Adicionando..." : "Adicionar"}
            </button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

// ── Simulation: dry-run a policy against a past session ──────────────

interface SimResult {
  tool: string;
  result: "ALLOW" | "ASK" | "DENY";
  reason: string | null;
}
interface SimData {
  results: SimResult[];
  summary: { ALLOW: number; ASK: number; DENY: number };
  tool_call_count: number;
}
const RESULT_TONE: Record<string, string> = {
  ALLOW: "#30a46c",
  ASK: "#e3a008",
  DENY: "#e5484d",
};

function SimulatePolicyDialog({ policy, onClose }: { policy: DefaultPolicy; onClose: () => void }) {
  const [sessions, setSessions] = useState<{ id: string; title: string }[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [data, setData] = useState<SimData | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // kind=any so sub-agent sessions (where most tool calls happen) are pickable.
    void authenticatedFetch("/v1/sessions?limit=80&kind=any")
      .then((r) => (r.ok ? r.json() : { data: [] }))
      .then((j: { data?: Array<{ id: string; title?: string; agent_name?: string }> }) =>
        setSessions(
          (j.data ?? []).map((s) => ({ id: s.id, title: s.title || s.agent_name || s.id })),
        ),
      )
      .catch(() => {});
  }, []);

  const run = async () => {
    if (!sessionId) return;
    setBusy(true);
    setError(null);
    setData(null);
    try {
      const res = await authenticatedFetch("/v1/policies/simulate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          handler: policy.handler,
          factory_params: policy.factory_params ?? undefined,
          session_id: sessionId,
        }),
      });
      const j = (await res.json().catch(() => ({}))) as SimData & { error?: { message?: string } };
      if (!res.ok) {
        setError(j?.error?.message ?? "Falha na simulação.");
        return;
      }
      setData(j);
    } catch {
      setError("Falha de rede na simulação.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Simular: {policy.name}</DialogTitle>
          <DialogDescription>
            Dry-run desta política contra os tool calls de uma sessão passada — sem afetar nada.
          </DialogDescription>
        </DialogHeader>
        <div className="flex items-center gap-2">
          <select
            value={sessionId}
            onChange={(e) => setSessionId(e.target.value)}
            className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1.5 text-sm"
          >
            <option value="">Escolha uma sessão…</option>
            {sessions.map((s) => (
              <option key={s.id} value={s.id}>
                {s.title}
              </option>
            ))}
          </select>
          <Button size="sm" disabled={!sessionId || busy} onClick={() => void run()}>
            {busy ? "Simulando…" : "Simular"}
          </Button>
        </div>
        {error && <p className="text-sm text-destructive">{error}</p>}
        {data && (
          <div className="flex flex-col gap-3">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              {(["ALLOW", "ASK", "DENY"] as const).map((k) => (
                <span
                  key={k}
                  className="rounded-md px-2 py-1 tabular-nums"
                  style={{ backgroundColor: `${RESULT_TONE[k]}22`, color: RESULT_TONE[k] }}
                >
                  {k}: {data.summary[k]}
                </span>
              ))}
              <span className="ml-auto text-xs text-muted-foreground">
                {data.tool_call_count} tool calls
              </span>
            </div>
            <div className="max-h-72 overflow-auto rounded-md border border-border">
              {data.results.length === 0 ? (
                <p className="p-3 text-sm text-muted-foreground">
                  Essa sessão não teve tool calls.
                </p>
              ) : (
                <ul className="divide-y divide-border/60">
                  {data.results.map((r, i) => (
                    <li key={i} className="flex items-center gap-2 px-3 py-1.5 text-sm">
                      <span
                        className="w-12 shrink-0 text-xs font-semibold"
                        style={{ color: RESULT_TONE[r.result] }}
                      >
                        {r.result}
                      </span>
                      <span className="truncate font-mono text-xs">{r.tool}</span>
                      {r.reason && (
                        <span
                          className="ml-auto max-w-[45%] truncate text-xs text-muted-foreground"
                          title={r.reason}
                        >
                          {r.reason}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

export function PoliciesPage() {
  const info = useServerInfo();
  // Plain header/single-user mode: no auth endpoints exist. server_version
  // distinguishes a live single-user server from a failed /v1/info probe
  // (which uses the same accounts_enabled:false / login_url:null sentinel).
  const isSingleUser =
    info !== "loading" &&
    !info.accounts_enabled &&
    info.login_url === null &&
    info.server_version !== null;
  const [meIsAdmin, setMeIsAdmin] = useState<boolean | null>(null);
  const { data: policies = [], refetch } = useDefaultPolicies();
  const { data: registry = [] } = usePolicyRegistry();
  const updatePolicy = useUpdateDefaultPolicy();
  const deletePolicy = useDeleteDefaultPolicy();
  const [addOpen, setAddOpen] = useState(false);
  const [deleteCandidate, setDeleteCandidate] = useState<DefaultPolicy | null>(null);
  const [simulateCandidate, setSimulateCandidate] = useState<DefaultPolicy | null>(null);
  const [pendingAction, setPendingAction] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const registryByHandler = new Map(registry.map((r) => [r.handler, r]));
  const appliedHandlers = new Set(policies.map((p) => p.handler));

  const refresh = useCallback(() => {
    void refetch();
  }, [refetch]);

  // Admin probe via the mode-agnostic `/v1/me` identity (works under OIDC
  // too, unlike the accounts-only `/auth/me`). Skipped in single-user mode
  // because no auth endpoints exist and the backend skips admin enforcement.
  useEffect(() => {
    if (isSingleUser) return;
    void (async () => {
      const userId = await resolveIdentity();
      if (userId === null) return;
      setMeIsAdmin(getCurrentIsAdmin());
    })();
  }, [isSingleUser]);

  if (!isSingleUser && meIsAdmin === null) {
    return (
      <div className="flex min-h-full items-center justify-center text-sm text-muted-foreground">
        Carregando...
      </div>
    );
  }

  if (!isSingleUser && meIsAdmin === false) {
    return (
      <div className="mx-auto w-full max-w-2xl px-6 py-12">
        <h1 className="mb-2 text-2xl font-semibold">Políticas globais</h1>
        <p className="text-sm text-muted-foreground">
          Você não tem permissão para gerenciar políticas globais.
        </p>
      </div>
    );
  }

  async function onConfirmDelete() {
    if (deleteCandidate === null) return;
    setPendingAction(true);
    setActionError(null);
    deletePolicy.mutate(deleteCandidate.id, {
      onSuccess: () => {
        setPendingAction(false);
        setDeleteCandidate(null);
      },
      onError: (err) => {
        setPendingAction(false);
        setActionError(err.message);
      },
    });
  }

  return (
    <PageScroll contentClassName="px-6">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Políticas globais</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Políticas globais aplicadas a todas as sessões.
          </p>
        </div>
        <Button onClick={() => setAddOpen(true)}>
          <PlusIcon /> Adicionar política
        </Button>
      </div>

      {policies.length > 0 && (
        <div className="flex flex-col gap-3">
          {policies.map((p) => {
            const registryEntry = registryByHandler.get(p.handler);
            const params = p.factory_params;
            const hasParams = params != null && Object.keys(params).length > 0;
            return (
              <div key={p.id} className="rounded-lg border border-border bg-background p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-start gap-2.5 min-w-0">
                    <ShieldCheckIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium">{p.name}</span>
                        {!p.enabled && (
                          <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                            Desativada
                          </span>
                        )}
                      </div>
                      {registryEntry?.description && (
                        <p className="mt-0.5 text-xs text-muted-foreground">
                          {registryEntry.description}
                        </p>
                      )}
                      <code className="mt-1 block text-[11px] text-muted-foreground/70">
                        {p.handler}
                      </code>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <Switch
                      checked={p.enabled}
                      onCheckedChange={(checked) =>
                        updatePolicy.mutate({
                          policyId: p.id,
                          enabled: checked,
                        })
                      }
                      aria-label={`Alternar ${p.name}`}
                    />
                    <Button
                      variant="ghost"
                      size="icon"
                      className="size-8 text-muted-foreground hover:text-foreground"
                      title="Simular política contra uma sessão"
                      onClick={() => setSimulateCandidate(p)}
                    >
                      <FlaskConicalIcon className="size-3.5" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="size-8 text-muted-foreground hover:text-destructive"
                      title="Remover política"
                      onClick={() => setDeleteCandidate(p)}
                      disabled={pendingAction}
                    >
                      <TrashIcon className="size-3.5" />
                    </Button>
                  </div>
                </div>
                {hasParams && (
                  <div className="ml-6.5 mt-2 rounded-md border border-border/60 bg-muted/40 px-3 py-2">
                    <span className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground/70">
                      Parâmetros
                    </span>
                    <div className="mt-1 flex flex-col gap-0.5">
                      {Object.entries(params).map(([key, value]) => (
                        <div key={key} className="flex items-baseline gap-1.5 text-xs">
                          <span className="font-medium text-foreground/80">{key}:</span>
                          <span className="text-muted-foreground">
                            {Array.isArray(value) ? value.join(", ") : String(value)}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {policies.length === 0 && (
        <p className="text-sm text-muted-foreground">
          Nenhuma política global configurada. Adicione uma para aplicá-la a todas as sessões.
        </p>
      )}

      <div className="mt-3 flex items-center justify-end">
        <Button variant="ghost" size="sm" onClick={refresh}>
          <RefreshCwIcon /> Atualizar
        </Button>
      </div>

      <AddDefaultPolicyDialog
        registry={registry}
        appliedHandlers={appliedHandlers}
        open={addOpen}
        onOpenChange={setAddOpen}
      />

      {simulateCandidate && (
        <SimulatePolicyDialog
          policy={simulateCandidate}
          onClose={() => setSimulateCandidate(null)}
        />
      )}

      {/* Delete confirmation */}
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
            <DialogTitle>Remover {deleteCandidate?.name}?</DialogTitle>
            <DialogDescription>
              Isso remove a política global de todas as sessões. Políticas no nível da sessão já
              existentes com o mesmo handler não são afetadas.
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
              {pendingAction ? "Removendo..." : "Remover"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </PageScroll>
  );
}
