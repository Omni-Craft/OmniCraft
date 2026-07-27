#!/usr/bin/env bash
#
# Health check against a LIVE OmniCraft install.
#
# Complements the other two checks rather than repeating them:
#   * `scripts/backend-smoke.sh` builds a throwaway install and never runs an
#     agent, so it proves the code works, not that YOUR install does.
#   * `GET /v1/doctor` inspects the environment (CLIs on PATH, host connected),
#     but never exercises a turn.
#
# This one drives the real server the way a person does — it opens a session,
# asks an agent something, and waits for the answer — so a break anywhere in
# the chain (server, host, runner, provider credentials) surfaces as a failure
# instead of as a green environment with a product that cannot answer.
#
# Usage:
#   scripts/health-check.sh                  # http://127.0.0.1:6767
#   BASE_URL=https://host.ts.net scripts/health-check.sh
#   AGENT=chat TIMEOUT=90 scripts/health-check.sh
#
# Exit code is 0 only when every check passes, so a scheduler can act on it.

set -uo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:6767}"
AGENT="${AGENT:-chat}"
# A turn goes out to a provider; slow is normal, silent forever is not.
TIMEOUT="${TIMEOUT:-90}"
PROMPT="${PROMPT:-Responda apenas: OK}"

pass=0
fail=0
created_session=""

ok() {
  printf '  ✓ %s\n' "$1"
  pass=$((pass + 1))
}

ko() {
  printf '  ✗ %s\n' "$1"
  [ -n "${2:-}" ] && printf '      %s\n' "$2"
  fail=$((fail + 1))
}

# Always drop the throwaway session, including on Ctrl-C — otherwise a daily
# run quietly fills the sidebar with test sessions.
cleanup() {
  if [ -n "$created_session" ]; then
    curl -sS -X DELETE "$BASE_URL/v1/sessions/$created_session" \
      -H "Origin: $BASE_URL" >/dev/null 2>&1
  fi
}
trap cleanup EXIT INT TERM

echo "OmniCraft — verificação de saúde"
echo "  servidor: $BASE_URL"
echo "  agente:   $AGENT"
echo

# --- 1. the server answers at all ------------------------------------------
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$BASE_URL/health" 2>/dev/null)
if [ "$code" = "200" ]; then
  ok "Servidor respondendo"
else
  ko "Servidor não respondeu" "HTTP $code — o resto não faz sentido sem isso"
  echo
  echo "RESULTADO: $pass ok, $fail com problema"
  exit 1
fi

# --- 2. the environment checks the product runs on itself ------------------
doctor=$(curl -sS --max-time 20 "$BASE_URL/v1/doctor" 2>/dev/null)
bad=$(printf '%s' "$doctor" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print('PARSE'); raise SystemExit
checks = d.get('checks', d if isinstance(d, list) else [])
print('|'.join(c.get('label', '?') for c in checks if not c.get('ok')))
" 2>/dev/null)
if [ "$bad" = "PARSE" ]; then
  ko "Diagnóstico ilegível" "resposta não é JSON"
elif [ -z "$bad" ]; then
  ok "Diagnóstico do ambiente"
else
  ko "Diagnóstico do ambiente" "falhando: ${bad//|/, }"
fi

# --- 3. the web UI is actually served --------------------------------------
for path in / /manifest.webmanifest /sw.js; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "$BASE_URL$path" 2>/dev/null)
  if [ "$code" = "200" ]; then
    ok "Interface serve $path"
  else
    ko "Interface não serve $path" "HTTP $code"
  fi
done

# --- 4. push is configured (the phone depends on it) -----------------------
key=$(curl -sS --max-time 10 "$BASE_URL/v1/push/vapid-public-key" 2>/dev/null |
  python3 -c "import json,sys; print(json.load(sys.stdin).get('key',''))" 2>/dev/null)
if [ -n "$key" ]; then
  ok "Chave de push disponível"
else
  ko "Chave de push ausente" "notificações não sairiam"
fi

# --- 5. a real turn, end to end --------------------------------------------
agent_id=$(curl -sS --max-time 15 "$BASE_URL/v1/agents" 2>/dev/null | python3 -c "
import json, sys
rows = json.load(sys.stdin)
rows = rows.get('data', rows) if isinstance(rows, dict) else rows
print(next((a['id'] for a in rows if a.get('name') == '$AGENT'), ''))
" 2>/dev/null)

if [ -z "$agent_id" ]; then
  ko "Agente '$AGENT' não está instalado" "instale-o na Galeria de agentes"
else
  ok "Agente '$AGENT' instalado"

  # A session with no host bound refuses events, so the turn needs a live one.
  # This doubles as a check: no online host means nothing can run at all.
  host_id=$(curl -sS --max-time 15 "$BASE_URL/v1/hosts" 2>/dev/null | python3 -c "
import json, sys
hosts = json.load(sys.stdin).get('hosts', [])
print(next((h['host_id'] for h in hosts if h.get('status') == 'online'), ''))
" 2>/dev/null)
  if [ -z "$host_id" ]; then
    ko "Nenhuma máquina online" "sem host, nenhum agente roda"
  else
    ok "Máquina online"
  fi

  created_session=$(curl -sS --max-time 30 -X POST "$BASE_URL/v1/sessions" \
    -H "Origin: $BASE_URL" -H "Content-Type: application/json" \
    -d "{\"agent_id\": \"$agent_id\", \"host_id\": \"$host_id\", \"workspace\": \"$PWD\"}" 2>/dev/null |
    python3 -c "import json,sys; print(json.load(sys.stdin).get('id',''))" 2>/dev/null)

  if [ -z "$created_session" ]; then
    ko "Não consegui abrir uma sessão" "o servidor recusou a criação"
  else
    ok "Sessão aberta"
    answer=$(BASE_URL="$BASE_URL" SESSION="$created_session" PROMPT="$PROMPT" \
      TIMEOUT="$TIMEOUT" python3 "$(dirname "$0")/health_check_turn.py" 2>&1)
    if [ $? -eq 0 ] && [ -n "$answer" ]; then
      ok "Agente respondeu: ${answer:0:60}"
    else
      ko "Agente não respondeu em ${TIMEOUT}s" "$answer"
    fi
  fi
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "RESULTADO: tudo funcionando ($pass verificações)"
else
  echo "RESULTADO: $fail com problema, $pass ok"
fi
exit $((fail > 0))
