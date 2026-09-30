# Clothesline

Local, shared memory for coding agents. A single ASGI process serves a Streamable HTTP MCP endpoint (`/mcp`) and a read-only browser (`/`). Submitted session turns are searchable for three days after inactivity; then they are summarized and raw turns are removed. Decisions and preferences remain versioned until explicitly superseded.

**Status: early implementation, not yet a released Homebrew package.** No harness automatically streams its conversation merely by connecting to MCP. Clients must explicitly submit turns, or use a harness adapter. Never submit secrets or private reasoning as text blocks.

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

Pi/Oh My Pi does not expose an MCP client in the installed Pi version inspected while building Clothesline. It needs an MCP bridge extension (or explicit adapter) before Pi can call these tools. Merely putting the MCP URL in a Pi settings file does not work. Zed and Claude Code are the two verified MCP clients; Pi integration remains separate work.

The official Python MCP SDK serves both `2025-11-25` and `2026-07-28`; older clients negotiate with the legacy initialization handshake. Neither MCP transport sessions nor model-facing harness names act as authenticated identities.

## Retention and search

After 72 hours without new turns, a daily sweep schedules an archive job. A generated summary is indexed before raw turns and their search indexes are deleted in one database transaction. Failed summary jobs keep the originals and show errors in the dashboard. Generated summaries are lossy and should not be promoted automatically into preferences. Original turn content and associated source event IDs are no longer available after archival; project, session dates, and attribution remain.

FTS5 handles literal keyword queries; sqlite-vec adds local semantic search when embeddings are installed and processed. A live Zed-submitted turn was retrieved by a paraphrase with no matching keywords. A synthetic multi-agent session was summarized and archived locally with `llama-cli` (Qwen3-1.7B-Q8_0); model quality on real long sessions remains to be evaluated. Search results show whether semantic search was available. The web UI uses keyword search. All content remains on this machine except model downloads from Hugging Face.

## Release work remaining

Before shipping: verify `llama.cpp` support on both Apple Silicon and Intel, add schema migration support beyond the initial version, tag and publish source artifacts, then add a verified source URL/SHA-256 Homebrew formula and user service to `~/workspace/homebrew-tap`. Pi still needs an adapter before it can connect. No Homebrew formula should claim a non-existent source tarball.
