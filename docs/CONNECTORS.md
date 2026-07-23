# Connector directory

A browsable catalog of MCP servers with one-click install, at
**Craftwork → Conectores**. Adding a server used to mean knowing its package
name, transport and args by heart; a card carries all of that.

The manual form at **Craftwork → Servidores MCP** stays for the advanced case —
a server of your own, editing, testing an existing one.

## How an install works

1. Pick the agent (defaults to `chat`).
2. Click **Instalar**. If the connector needs a credential, the first click
   reveals the field instead of installing a server that cannot start.
3. The install `POST`s to `/v1/agents/{id}/mcp-servers` — the same endpoint the
   manual form uses — and then immediately runs the existing connection test.
   "It saved" and "it works" are different claims, and only the second is
   useful to the person who clicked.

Installed connectors get a badge plus **Testar** and **Remover**, so the page
serves discovery and upkeep both.

## Credentials

`env` (stdio) and `headers` (http) are **write-only**: the API accepts them on
create and update, writes them into the agent bundle, and never returns them.
`MCPServerSummary` has no field for either, so no read path can leak one — see
the generated `openapi.json` for both halves of the contract.

**Prefer a `${VAR}` reference to a literal secret.** The spec parser expands it
at run time, so the raw value stays out of the bundle. The install dialog
suggests that form in its placeholder.

Editing a server without supplying the credential preserves whatever the bundle
already holds, so the UI can change a command or description without knowing
the secret.

> Secrets written literally live in the agent bundle YAML. That is how MCP
> credentials have always worked here, but it means anyone who can download the
> bundle can read them. The `${VAR}` form avoids this.

## The catalog

`omnicraft/server/data/mcp_catalog.json`, served by `GET /v1/mcp-catalog`. It is
static data, not a registry crawl, so it can grow without a web rebuild.

Every entry carries what the install needs (`transport`, `command`/`args` or
`url`) plus `env_required` — the variables the UI should ask for — and an
optional `setup_note` for connectors that need a path or connection string
edited by hand.

### Adding a connector

1. Add an entry to the JSON.
2. **Verify the package actually exists** on npm or PyPI before committing. Every
   shipped entry was checked; a one-click install that gets the package name
   wrong is worse than no catalog.
3. `tests/server/test_mcp_catalog.py` validates each entry against the same
   `UpsertMCPServerRequest` the install applies, so a malformed entry fails CI
   rather than someone's machine.

## Limitations

- **No generic OAuth.** Remote connectors authenticate with static headers, or
  via the Databricks profile path. An interactive browser handshake — what
  Claude Desktop does for remote connectors — is not implemented; it is the
  largest remaining gap.
- **Emoji, not icons.** Entries carry an emoji rather than a hosted asset.
- **Install targets a template agent**, not a single session.

## Where the code lives

| Piece | File |
|-------|------|
| Catalog data | `omnicraft/server/data/mcp_catalog.json` |
| Catalog endpoint | `omnicraft/server/routes/mcp_catalog.py` |
| Directory UI | `web/src/pages/ConnectorsPage.tsx` |
| Install / test / delete API | `omnicraft/server/routes/agent_mcp_servers.py` |
| Write-only secret fields | `omnicraft/server/schemas.py` (`UpsertMCPServerRequest`) |
| Secret persistence | `omnicraft/server/routes/session_mcp_servers.py` (`_apply_transport_fields`) |
