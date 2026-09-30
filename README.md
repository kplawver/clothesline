# Clothesline

Local, shared memory for coding agents. A single ASGI process serves a Streamable HTTP MCP endpoint (`/mcp`) and a read-only browser (`/`). Submitted session turns are searchable for three days after inactivity; then they are summarized and raw turns are removed. Decisions and preferences remain versioned until explicitly superseded.

**Status: first macOS release; Apple Silicon tested, Intel not yet tested.** Connecting to MCP does not capture sessions. Claude Code offers optional, per-project hook capture; other clients must explicitly submit turns or opt in to an existing-session importer. Never submit secrets or private reasoning as text blocks.

## Install with Homebrew

```sh
brew tap kplawver/tap
brew install clothesline
clothesline setup  # downloads ~67 MB embeddings and ~1.83 GB GGUF, once
brew services start clothesline
```

Open http://127.0.0.1:19004/ for the browser and connect agents to http://127.0.0.1:19004/mcp. The formula uses the locked `uv` dependencies at install time, which requires access to the Python package index; models download separately on `setup`. The user-level brew service uses the default data directory, preserving memory across upgrades. Use `brew services restart clothesline` after changing configuration.

## Development

Requires Python 3.12+ and Homebrew's `llama.cpp` for local session summaries (`brew install llama.cpp`). Native `llama-cpp-python` proved too slow to build reliably on the test machine, so summarization invokes the bottled `llama-cli` as a short-lived local subprocess; the HTTP and browser interfaces remain in one process.

```sh
uv venv --python 3.12
brew install llama.cpp
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/clothesline setup  # downloads ~67 MB embedding model and ~1.83 GB GGUF
.venv/bin/clothesline serve
```

`clothesline setup` downloads both models explicitly, verifies their pinned SHA-256 hashes, and retries previously blocked jobs. The server does not download models when processing requests. Without installed models, keyword search and session submission work, but embedding jobs and archive jobs remain pending and retry until dependencies are available. `clothesline doctor` shows failed jobs.

UI: http://127.0.0.1:19004/ · MCP: http://127.0.0.1:19004/mcp

Test without downloading models:

```sh
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest -q
```

## Configuration

Place TOML at `~/.config/clothesline/config.toml`, or use `CLOTHESLINE_CONFIG` to select another path:

```toml
host = "127.0.0.1"
port = 19004
data_dir = "~/Library/Application Support/Clothesline"
```

`CLOTHESLINE_HOST`, `CLOTHESLINE_PORT` and `CLOTHESLINE_DATA_DIR` override these values. The service rejects non-loopback addresses. Browser pages are read-only; settings are changed via the file and a restart. Paths, model files and database live outside the installed package. `clothesline backup --output /path/to/backup.sqlite3` creates a consistent SQLite backup. `clothesline rebuild-index` recreates keyword indexes and requeues all embeddings from stored chunks.

## MCP workflow

Register a harness with a persistent installation key, then an agent ID. Start a session, submit each visible turn via `append_turn` (use stable source event IDs for deduplication), and optionally end the session. For a sub-agent, register a separate agent with its parent's ID. Search and inspect memories at any time. Memory writes require explicit user intent; a revision supersedes rather than deletes the previous memory. Tool descriptions contain the usage guidance.

Claude Code supports HTTP MCP servers. This checkout has a project-scoped `.mcp.json` for Clothesline; Claude Code may require approval before first use. Claude Code has successfully called `search_sessions` and recalled the Zed-submitted test turn across harnesses. Tool use is *cooperative*: no automatic transcript capture or identity persistence is provided by this server.

Zed can register Clothesline as a remote MCP server through `context_servers`:

```json
"clothesline": { "enabled": true, "url": "http://127.0.0.1:19004/mcp" }
```

The local development server must already be running. If Zed loaded this setting before the server started, restart the MCP connection in Zed's Settings → AI → MCP Servers. A Zed Agent conversation has successfully registered a harness and agent, started a session, submitted a test turn, and recalled it by keyword search. Adding the server exposes its tools but does not automatically record Zed's conversation turns.

Oh My Pi (OMP 18.4.4) supports HTTP MCP servers and discovers Clothesline from this checkout's project-scoped `.mcp.json`. OMP has successfully used `search_sessions` to retrieve Zed's turn with semantic search available. Zed, Claude Code and OMP are verified MCP clients. The separate Pi CLI (`pi`) does not include a built-in MCP client in the installed version inspected; it would need an adapter.

The official Python MCP SDK serves both `2025-11-25` and `2026-07-28`; older clients negotiate with the legacy initialization handshake. Neither MCP transport sessions nor model-facing harness names act as authenticated identities.

## Retention and search

After 72 hours without new turns, a daily sweep schedules an archive job. A generated summary is indexed before raw turns and their search indexes are deleted in one database transaction. Failed summary jobs keep the originals and show errors in the dashboard. Generated summaries are lossy and should not be promoted automatically into preferences. Original turn content and associated source event IDs are no longer available after archival; project, session dates, and attribution remain.

FTS5 handles literal keyword queries; sqlite-vec adds local semantic search when embeddings are installed and processed. A live Zed-submitted turn was retrieved by a paraphrase with no matching keywords. A synthetic multi-agent session was summarized and archived locally with `llama-cli` (Qwen3-1.7B-Q8_0); model quality on real long sessions remains to be evaluated. Search results show whether semantic search was available. The web UI uses keyword search. All content remains on this machine except model downloads from Hugging Face.

## Import existing sessions (opt-in)

Connecting via MCP does **not** capture a session. Select **one** saved session at a time for manual import. First run `--dry-run` to preview counts without opening or changing the Clothesline database:

```sh
clothesline import-omp --source ~/.omp/agent/sessions/PROJECT/SESSION.jsonl --dry-run
clothesline import-pi --source ~/.pi/agent/sessions/PROJECT/SESSION.jsonl --dry-run
clothesline import-claude --source ~/.claude/projects/PROJECT/SESSION.jsonl --dry-run
clothesline import-codex --source ~/.codex/sessions/YEAR/MONTH/DAY/SESSION.jsonl --dry-run

clothesline import-zed --source ~/Library/'Application Support'/Zed/threads/threads.db --list
clothesline import-zed --source ~/Library/'Application Support'/Zed/threads/threads.db --session-id THREAD_ID --dry-run
clothesline import-opencode --source ~/.local/share/opencode/opencode.db --list
clothesline import-opencode --source ~/.local/share/opencode/opencode.db --session-id SESSION_ID --dry-run
```

Omit `--dry-run` to import. `--list` prints only IDs, not titles or message content. Pi and OMP import the active tree branch; Claude Code imports the main branch, or a **separately selected** `subagents/agent-*.jsonl` file with distinct agent attribution. Codex imports visible conversation events, not duplicate response items. Zed and OpenCode databases are opened **read-only** and require a specific thread/session ID. Zed does not provide per-message dates in its thread data, so imported turns use the thread snapshot's update time.

Only visible user and assistant text is copied. Structured thinking/reasoning, system prompts, tool arguments and results, attachments/images, and alternative branches are excluded. **This is not a secret scanner:** visible text can still contain secrets; review sessions before importing. Each batch of turns and its checkpoint is atomic, and re-imports add only new turns. If previously imported text changes or a branch is replaced, Clothesline refuses to silently mix histories. Once a session is archived, new turns start a separate segment. Session files are limited to 50 MiB and individual visible turns to 250,000 characters.

Original harness files/databases are never modified by Clothesline's three-day retention. Historical imports may be archived at the next daily sweep based on their original timestamps (Zed uses the thread's update time). There is no background scanning of saved session files.

## Claude Code plugin: opt-in live capture

Install Clothesline with Homebrew and start its service first. Then install the Claude Code plugin, which bundles the MCP connection, a memory skill (`/clothesline:memory`), and hooks for `UserPromptSubmit` and `Stop`:

```sh
claude plugin marketplace add kplawver/clothesline
claude plugin install clothesline@clothesline --scope local  # or --scope user
clothesline capture-claude --enable --project /absolute/path/to/project
clothesline capture-claude --project /absolute/path/to/project  # show status
```

Restart Claude Code after installation. If the project also has a Clothesline `.mcp.json`, the plugin may expose a second connection; keep only one MCP configuration. Installing the plugin alone **does not enable capture**: the allowlist is stored at `<data_dir>/claude-capture.json` (mode 0600), with **exact project directory matches**. The hooks submit the visible user prompt and final assistant text to Clothesline's existing turn storage. They do not read the transcript, thinking, tool calls, tool results, or images. Claude Code writes its transcript asynchronously; the hooks use the event's current text fields rather than relying on a possibly stale file. The hook fails open if capture fails and never blocks a user prompt. A harmless live hook test captured both roles in an isolated database.

```sh
clothesline capture-claude --disable --project /absolute/path/to/project
```

Disabling stops **new** capture; it does not delete turns already stored, archived summaries, or backups. Visible prompts and final replies can contain secrets, including pasted text. The plugin does not scan or redact them; only enable projects you're comfortable storing locally. Existing manually imported Claude sessions must not be captured again (and vice versa); Clothesline refuses this combination for the same session. This plugin does not automate message-bus presence, heartbeats, or polling. For development, load `plugins/claude-code` with `claude --plugin-dir /path/to/clothesline/plugins/claude-code`. The bundled MCP URL assumes the default loopback port 19004; configure the server separately if you change ports.

## Agent conversations (Phase Two)

The message bus uses the same persistent harness and agent IDs as sessions. After `register_agent`, call `agent_online`. Call `agent_heartbeat` after 15 minutes without other bus activity, and `agent_offline` when the agent exits. One hour without contact makes presence stale; call `agent_online` again. A tool description cannot itself schedule a heartbeat or wake a harness to poll.

An online agent can `open_conversation`, optionally associating a project or session, then `send_message` with a stable `client_message_id` for safe retries. A specified recipient gets a durable delivery even while offline; a message without a recipient broadcasts to agents online **when it is sent**. Future registrations do not receive earlier broadcasts. The sender is not a recipient of its own broadcasts.

Agents call `poll_messages` and `ack_message` after processing each delivery. Polling without acknowledgment can return the same message again: delivery is **at least once**, not exactly once. Use `next_cursor` only to paginate a large backlog and start the next poll at `after_id=0` so unacknowledged messages are retried. Message bodies are retained and indexed for keyword search independently of the three-day session-turn retention. `get_conversation`, `search_messages`, and the read-only `/conversations` and `/agents` pages expose history and presence. Messages do not automatically become decisions or preferences.

## Release work remaining

The macOS Apple Silicon Homebrew install, service start, and cross-client semantic recall have been tested. SQLite migrations through v5 preserve existing memories while adding conversations, delivery state, verified session-import checkpoints, and hook capture. Still to do: test Intel, evaluate summaries on longer real sessions, and optionally add an adapter for the separate Pi CLI. Clothesline is licensed under the MIT License; see `LICENSE`.
