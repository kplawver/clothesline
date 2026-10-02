# Clothesline — current plan

_Last updated: 2026-10-02, after the split that left this repository holding only the message bus._

## Goal and boundaries

Build a local, durable channel between coding agents on one machine. Keep one source of truth in Clothesline; integrations should call its tools, not implement their own transport.

- Python/Starlette serves the browser and Streamable HTTP MCP from one process. SQLite provides durable storage and an FTS5 index over message bodies.
- Bind only to loopback (default `127.0.0.1:19004`); no authentication or remote service. Install and run as a Homebrew user service.
- **Out of scope since 0.7.0:** session turns, memories, embeddings, session archiving, session importers, and capture hooks. Those live in [Setauket](https://github.com/kplawver/setauket) on port 19005.
- Connecting an MCP client **does not** register anything or deliver anything. Agents register themselves, announce presence, and poll.

## Shipped and verified

**v0.7.0** is the first release of Clothesline as a messaging-only service. The Python suite has 18 passing tests; Ruff is clean. Four runtime dependencies (`mcp`, `starlette`, `uvicorn`, `jinja2`) — no models, no `llama-cli`, no `setup` command, and the Homebrew formula no longer needs `llama.cpp`.

- **Core bus:** attributed harnesses and agents (including sub-agents), presence with heartbeats and staleness, durable directed/broadcast conversations, polling and acknowledgments with at-least-once delivery, keyword search over messages, and read-only browser pages. OMP sent a live message that another agent polled, saw redelivered before acknowledgment, and no longer saw after acknowledgment.
- **MCP clients:** Claude Code, Zed, and **Oh My Pi (OMP)** have called Clothesline tools. OMP has native HTTP MCP; it is not the separate Pi CLI.
- **Claude Code plugin:** MCP connection plus the `/clothesline:agents` skill. No hooks — capture moved to Setauket's plugin.
- **Data migration:** the old schema-v5 database was split with Setauket's `scripts/split_legacy_db.py`. Conversations, messages, deliveries, presence, and the identity and project tables moved here with IDs unchanged; sessions and turns moved to Setauket. The split was drilled against a backup of the live database: row counts reconciled on every table, both sides passed `integrity_check` and `foreign_key_check`, the split Clothesline served every browser page, exposed all 12 MCP tools, and answered `search_messages` and `get_conversation` with the original message and attribution intact.

## Known integration caveats

- **Session references are opaque.** `conversations.session_id` used to be a foreign key with a project-match check. It is now a plain `TEXT` column: Clothesline cannot verify it and `/conversations` renders it as text rather than a link. This is the one capability the split genuinely cost.
- Presence is attributed, not authenticated. A tool description cannot schedule a heartbeat or wake a harness to poll; a dormant agent stays dormant until its harness polls again.
- Delivery is at least once. Clients that ack and then crash, or ack the wrong message ID, will see redelivery.
- Broadcast reaches only the agents online when the message is sent; later registrations do not receive it.
- Zed thread and OpenCode session caveats belong to Setauket's importers, not here.

## Possible follow-on work

- Improve harness-side bus ergonomics using lifecycle hooks where available, but decide delivery and acknowledgment semantics before automatically injecting messages into an agent's context.
- Consider a `.agents`-standard plugin so most harnesses can install the bus the same way they install skills.
- Message bodies are keyword-searched only. Semantic message search would need an embedding model here, which the split deliberately removed; weigh that against reinstalling ~67 MB of model for a feature Setauket already provides for turns.

See `README.md` for current commands and observed behavior. The original Phase One/Two requirements remain in `plan.md`.