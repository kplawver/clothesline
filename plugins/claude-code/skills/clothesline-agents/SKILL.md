---
name: clothesline-agents
description: Exchange durable messages with other agents on this machine using the local Clothesline MCP tools when asked to coordinate, delegate, hand off work, or check what another agent has been asked to do.
---

Clothesline is a message bus between registered agents. It stores nothing about your conversation and does not record your turns.

Call `agent_online` once at startup and `agent_offline` on shutdown. After 15 minutes without bus activity, call `agent_heartbeat`; one hour without contact marks you stale and you must call `agent_online` again. A tool description cannot schedule a heartbeat for you.

`send_message` needs a stable `client_message_id` so a retry does not duplicate the message. Set `recipient_agent_id` to deliver to one agent even while it is offline; omit it to broadcast, which reaches only the agents online at that moment. You cannot message yourself.

Delivery is at least once, not exactly once. Call `poll_messages`, process each message, then `ack_message`. Poll again with `after_id=0` so anything you did not acknowledge is redelivered. Use `next_cursor` only to page through a large backlog. There is no push: a harness must poll for you.

Messages are retained and keyword-searchable via `search_messages`, independently of Setauket's three-day turn retention. A message never becomes a decision or preference automatically.

Session memory — past turns, decisions, and preferences — lives in the separate Setauket MCP server. This plugin does not provide it.
