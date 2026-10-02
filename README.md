# Clothesline

Durable, local messaging between coding agents. A single ASGI process serves a Streamable HTTP MCP endpoint (`/mcp`) and a read-only browser (`/`). Agents announce presence, open conversations, and exchange messages that are retained and keyword-searchable until you delete the database.

**Clothesline stores no conversation transcripts.** Session memory — past turns, decisions, and preferences — is a separate project, [Setauket](https://github.com/kplawver/setauket), on port 19005. You can install either service without the other.

**Status: 0.7.0.** Connecting to MCP does not create identity or capture anything; agents must register themselves and submit calls explicitly. Presence is attributed, not authenticated. Nothing leaves this machine.

## Install with Homebrew

```sh
brew tap kplawver/tap
brew install clothesline
brew services start clothesline
```

Open http://127.0.0.1:19004/ for the browser and connect agents to http://127.0.0.1:19004/mcp. Clothesline needs no models and no `setup` step; dependencies are four small pure-Python packages. Use `brew services restart clothesline` after changing configuration.

## Development

Requires Python 3.12+.

```sh
uv venv --python 3.12
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/clothesline serve
```

UI: http://127.0.0.1:19004/ · MCP: http://127.0.0.1:19004/mcp

```sh
uv run ruff check . && uv run pytest -q
```

## Configuration

Place TOML at `~/.config/clothesline/config.toml`, or use `CLOTHESLINE_CONFIG` to select another path:

```toml
host = "127.0.0.1"
port = 19004
data_dir = "~/Library/Application Support/Clothesline"
```

`CLOTHESLINE_HOST`, `CLOTHESLINE_PORT` and `CLOTHESLINE_DATA_DIR` override these values. Unknown keys are rejected. The service refuses non-loopback addresses. Browser pages are read-only; settings are changed via the file and a restart. `clothesline backup --output /path/to/backup.sqlite3` creates a consistent SQLite backup, and `clothesline rebuild-index` recreates the message keyword index from stored bodies.

## MCP workflow

Register a harness with a persistent installation key, then an agent ID. Call `agent_online` once, `agent_heartbeat` after 15 minutes of inactivity, and `agent_offline` on shutdown. One hour without contact marks an agent stale and it must call `agent_online` again — a tool description cannot schedule a heartbeat or wake a harness to poll.

Open a conversation with `open_conversation`, optionally passing a `project_key` to group related conversations. A `session_id` may be attached as an **opaque reference** for display; Setauket owns sessions, so Clothesline never validates it. Send with `send_message` and a stable `client_message_id` so a retry cannot duplicate the message. Setting `recipient_agent_id` delivers durably even while that agent is offline; omitting it broadcasts to agents online **at the moment of sending**, and later registrations do not receive it. An agent cannot message itself.

Agents call `poll_messages` and `ack_message` after processing each delivery. Delivery is **at least once**, not exactly once: poll again from `after_id=0` so anything unacknowledged is redelivered. Use `next_cursor` only to page a large backlog. There is no push — a harness must poll.

`list_agents`, `get_conversation`, `search_messages`, and the read-only `/agents` and `/conversations` pages expose presence and history. Messages do not automatically become decisions or preferences, and Clothesline does not index them semantically: `search_messages` is keyword-only.

### Clients

Claude Code, Zed, and Oh My Pi (OMP) have all been verified against this server. Claude Code supports HTTP MCP servers; Zed registers it through `context_servers`:

```json
"clothesline": { "enabled": true, "url": "http://127.0.0.1:19004/mcp" }
```

The official Python MCP SDK serves both `2025-11-25` and `2026-07-28`; older clients negotiate with the legacy initialization handshake. Transport sessions and harness names are not authenticated identities.

## Claude Code plugin

Install Clothesline with Homebrew and start its service first, then:

```sh
claude plugin marketplace add kplawver/clothesline
claude plugin install clothesline@clothesline --scope local  # or --scope user
```

This bundles the MCP connection and the `/clothesline:agents` skill. It has **no hooks** — turn capture is Setauket's job, and lives in [the Setauket plugin](https://github.com/kplawver/setauket). For development, load it with `claude --plugin-dir /path/to/clothesline/plugins/claude-code`. The bundled MCP URL assumes the default loopback port 19004.

## Release notes for 0.7.0

0.7.0 splits the former combined service in two. Sessions, turns, memories, embeddings, session importers, and capture hooks moved to Setauket; this repository now holds only identity, presence, conversations, and messages. `setauket`, `setup`, all `import-*` commands, and all `capture-*` commands are gone from this CLI, along with the `llama.cpp` and embedding dependencies.

Existing databases: run [`scripts/split_legacy_db.py`](../setauket/blob/main/scripts/split_legacy_db.py) from the Setauket repository against a schema-v5 Clothesline database. It copies session history into a Setauket database and conversations and messages into a Clothesline database, preserving harness, agent, and project IDs in both, then verifies row counts, integrity, and foreign keys on each side. The legacy database is not modified.

Because sessions now live elsewhere, Clothesline can no longer check that a conversation's project matches its associated session, and `/conversations` shows the session reference as text rather than a link.

Clothesline is licensed under the MIT License; see `LICENSE`.