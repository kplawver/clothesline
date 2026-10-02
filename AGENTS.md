# Clothesline

Durable, local messaging between coding agents. One ASGI process serves a Streamable HTTP MCP endpoint (`/mcp`) and a read-only browser (`/`).

**Scope.** Clothesline stores **no conversation transcripts and no memories**. It holds agent identity, presence, conversations, and messages, and nothing else. Session memory — turns, decisions, preferences, embeddings, importers, capture hooks — is [Setauket](https://github.com/kplawver/setauket) on port 19005. Do not add session or memory features here; put them there. Each service keeps its own copy of the `harnesses`/`agents`/`projects` tables on purpose, so neither depends on the other.

## Layout

| Path | What |
|---|---|
| `src/clothesline/identity.py` | Harness/agent/project schema and registration. Duplicated in Setauket by design. |
| `src/clothesline/storage.py` | Identity plus the message-bus schema. No turns, no chunks, no vectors. |
| `src/clothesline/bus.py` | Presence, conversations, send/poll/ack, message search. |
| `src/clothesline/app.py` | MCP tools and read-only browser routes. |
| `src/clothesline/cli.py` | `serve`, `status`, `doctor`, `backup`, `rebuild-index`. |
| `.agents/skills/` | Generic `.agents` standard. Each entry is a symlink into the plugin's `skills/`, so `clpr repo check` and every harness that reads `.agents` see the same skill. |
| `plugins/claude-code/` | Claude Code packaging. Holds the real skill files, `.mcp.json`, `hooks/`, and an `agents.md` fragment Tallmadge composes into `~/.agents/agents.md`. |

The link runs from `.agents/` into the plugin, not the reverse, because `claude plugin validate --strict` treats a symlinked plugin component as a warning it will not follow. Both tools still agree on one copy of the text.

## Ground rules

- **Loopback only.** `Config` rejects any host that is not `127.0.0.1`, `::1`, or `localhost`. Keep it that way; the service is unauthenticated.
- **Presence is attributed, not authenticated.** Never describe a harness or agent name as proof of identity.
- **Delivery is at least once.** `bus.py` may return the same message again until it is acknowledged. Do not add exactly-once assumptions.
- **A session reference is opaque.** `conversations.session_id` has no foreign key; Setauket owns sessions and Clothesline cannot validate it. Do not reintroduce validation.
- **No models.** There is no embedding model, no worker thread, and no `llama.cpp` dependency. Keyword search over message bodies only. If a feature seems to need embeddings, it belongs in Setauket.
- **Nothing leaves the machine** except what the user explicitly imports from their own files.

## Change checklist

1. `uv run ruff check .`
2. `uv run pytest -q`
3. `claude plugin validate . --strict && claude plugin validate plugins/claude-code --strict`
4. `clpr repo check` if Tallmadge (`clpr`) is on `PATH`; CI runs it unconditionally.
5. Database schema changes: bump `user_version` and add a migration, since `Store.__init__` gates on it.