// Server errors, in Portuguese.
//
// The API answers with a stable machine code plus an English sentence. That
// sentence is part of the API's contract — SDK users and hundreds of tests
// read it — so it stays as it is on the wire, and the translation happens
// here, at the edge where a human reads it. The original text is not thrown
// away: it goes to the console, where debugging needs it.

/** The error envelope every OmniCraft endpoint answers failures with. */
export interface ApiErrorEnvelope {
  error?: { code?: string; message?: string } | null;
}

/**
 * Portuguese for each machine code. Keep in sync with ``ErrorCode`` in
 * ``omnicraft/errors.py`` — an unlisted code falls back to the caller's own
 * message, so a new code degrades to the previous behaviour instead of
 * showing something wrong.
 */
const MESSAGES: Record<string, string> = {
  unauthorized: "Sessão expirada ou credenciais inválidas. Entre novamente.",
  forbidden: "Você não tem permissão para esta ação.",
  not_found: "Não encontrado.",
  invalid_input: "Os dados enviados são inválidos.",
  already_exists: "Isso já existe.",
  conflict: "A operação conflita com o estado atual.",
  internal_error: "Erro interno do servidor. Tente novamente.",
  harness_protocol_violation: "O agente respondeu fora do protocolo esperado.",
  runner_unavailable: "O runner não está disponível agora.",
  runner_capability_mismatch: "O runner não oferece o recurso pedido.",
  harness_not_configured: "Este agente ainda não está configurado.",
  host_unverifiable: "Não foi possível verificar este host.",
};

/**
 * The message to show a person for a failed API call.
 *
 * :param payload: The parsed response body, when there was one.
 * :param fallback: What to show when the server said nothing usable —
 *     typically a sentence naming the action that failed.
 * :returns: A Portuguese sentence, never empty.
 */
export function apiErrorMessage(payload: ApiErrorEnvelope | null | undefined, fallback: string) {
  const error = payload?.error;
  const code = error?.code;
  if (!code || !(code in MESSAGES)) return error?.message || fallback;

  const translated = MESSAGES[code];
  const detail = error?.message?.trim();
  // The same code carries boilerplate on one route and the thing you actually
  // need on another ("'rice' needs manage permission"). Dropping the detail
  // would sometimes hide the cause, so keep it after the Portuguese sentence
  // rather than choosing between them.
  if (!detail || isBoilerplate(detail)) return translated;
  return `${stripTrailingPeriod(translated)}: ${detail}`;
}

/**
 * Whether a server message only restates its code, adding nothing.
 *
 * These are the generic sentences the framework produces; repeating them
 * after the translation would just put English back on screen for no gain.
 */
function isBoilerplate(detail: string): boolean {
  const normalized = detail.toLowerCase();
  return (
    normalized.startsWith("forbidden:") ||
    normalized.startsWith("unauthorized") ||
    normalized === "an internal error occurred." ||
    normalized === "not found"
  );
}

function stripTrailingPeriod(text: string): string {
  return text.endsWith(".") ? text.slice(0, -1) : text;
}
