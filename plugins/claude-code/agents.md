# Clothesline

Durable local messaging between coding agents. Install the service and start it before using these tools; connecting to MCP registers nothing and captures nothing.

Register a harness with a persistent installation key, then an agent. Call `agent_online` at startup, `agent_heartbeat` after 15 minutes of inactivity, and `agent_offline` on shutdown. One hour without contact marks an agent stale.

Send with a stable `client_message_id` so a retry cannot duplicate the message. Poll and acknowledge each delivery; delivery is at least once, so poll again from `after_id=0` and expect redelivery of anything unacknowledged. There is no push: a harness must poll, and a tool description cannot schedule a heartbeat for it.

Session memory is a different service, Setauket, on port 19005. Do not look for transcripts or decisions here.