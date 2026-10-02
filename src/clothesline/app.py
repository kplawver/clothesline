"""One HTTP process for MCP and the local read-only browser interface."""

import logging
from datetime import UTC, datetime
from pathlib import Path

import anyio
from jinja2 import Environment, FileSystemLoader, select_autoescape
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from clothesline.bus import HEARTBEAT_AFTER, STALE_AFTER, MessageBus
from clothesline.config import Config
from clothesline.storage import Store

log = logging.getLogger(__name__)
TEMPLATES = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape())
TEMPLATES.filters["date"] = lambda timestamp: datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M UTC") if timestamp else "—"


def create_app(config: Config, store: Store | None = None):
    store = store or Store(config.database)
    bus = MessageBus(store)
    server = MCPServer("clothesline", version="0.7.0", instructions=(
        "Durable messaging between registered agents. Register a persistent harness installation key and an "
        "agent first, then call agent_online. Polling is explicit and delivery is at least once: call "
        "poll_messages and ack_message after processing. Session memory lives in the separate Setauket server."))

    @server.tool(description="Register one installation of a coding harness. Persist installation_key locally and reuse it on every reconnect. Then call register_agent; a connection does not create identity automatically.")
    async def register_harness(installation_key: str, name: str) -> dict:
        return {"harness_id": await anyio.to_thread.run_sync(store.register_harness, installation_key, name)}

    @server.tool(description="Register an agent or sub-agent within a registered harness; reuse external_id for the same agent. Pass parent_id for a sub-agent. Use the returned agent_id on every presence and message call.")
    async def register_agent(harness_id: str, external_id: str, parent_id: str | None = None) -> dict:
        return {"agent_id": await anyio.to_thread.run_sync(store.register_agent, harness_id, external_id, parent_id)}

    @server.tool(description="Mark a registered agent online; call once on start or after an hour of inactivity. Then use open_conversation/send_message/poll_messages. Presence is attributed, not authenticated. Call agent_offline on shutdown.")
    async def agent_online(harness_id: str, agent_id: str) -> dict:
        await anyio.to_thread.run_sync(bus.online, harness_id, agent_id)
        return {"online": True, "heartbeat_after_seconds": HEARTBEAT_AFTER, "stale_after_seconds": STALE_AFTER}

    @server.tool(description="Keep an online agent active after up to 15 minutes of inactivity; send/poll/ack calls also refresh presence. After one hour without contact the agent becomes stale and must call agent_online again.")
    async def agent_heartbeat(harness_id: str, agent_id: str) -> dict:
        await anyio.to_thread.run_sync(bus.heartbeat, harness_id, agent_id)
        return {"online": True}

    @server.tool(description="Sign an agent off when its harness exits. Direct messages remain queued while offline; broadcast messages only go to agents online when sent. Rejoin using agent_online.")
    async def agent_offline(harness_id: str, agent_id: str) -> dict:
        return {"signed_off": await anyio.to_thread.run_sync(bus.offline, harness_id, agent_id)}

    @server.tool(description="List agents currently online, or include stale/offline agents for history. Online means contact within the last hour; an agent cannot heartbeat autonomously without harness support.")
    async def list_agents(include_inactive: bool = False) -> dict:
        return {"agents": await anyio.to_thread.run_sync(bus.agents, include_inactive)}

    @server.tool(description="Create a conversation for an online agent. Optional project_key groups conversations. Optional session_id is an opaque Setauket session reference kept for display; this server cannot verify it. Use the returned conversation_id with send_message and get_conversation.")
    async def open_conversation(harness_id: str, agent_id: str, title: str,
                                project_key: str | None = None, session_id: str | None = None) -> dict:
        conversation_id = await anyio.to_thread.run_sync(bus.open_conversation, harness_id, agent_id, title, project_key, session_id)
        return {"conversation_id": conversation_id}

    @server.tool(description="Send a durable message from an online agent. Supply a stable client_message_id for retry safety. recipient_agent_id delivers even if that agent is offline; omit it to broadcast only to agents online now. Body must be visible text, not reasoning or secrets. Poll and acknowledge messages separately.")
    async def send_message(conversation_id: str, harness_id: str, agent_id: str, client_message_id: str,
                           body: str, recipient_agent_id: str | None = None) -> dict:
        message_id = await anyio.to_thread.run_sync(bus.send, conversation_id, harness_id, agent_id,
                                                     client_message_id, body, recipient_agent_id)
        return {"message_id": message_id}

    @server.tool(description="Poll unacknowledged messages for an online agent, oldest first. No push or automatic wakeup: the harness must call this tool. Use next_cursor only to page through a large backlog; restart at after_id=0 on the next poll so unacknowledged messages are retried. Call ack_message after processing each one.")
    async def poll_messages(harness_id: str, agent_id: str, after_id: int = 0, limit: int = 30) -> dict:
        return await anyio.to_thread.run_sync(bus.inbox, harness_id, agent_id, after_id, limit)

    @server.tool(description="Acknowledge delivery after processing a message from poll_messages. Acknowledgment is idempotent and scoped to the recipient agent; unacknowledged messages remain available for at-least-once delivery.")
    async def ack_message(harness_id: str, agent_id: str, message_id: int) -> dict:
        await anyio.to_thread.run_sync(bus.acknowledge, harness_id, agent_id, message_id)
        return {"acknowledged": True}

    @server.tool(description="Read a conversation and its attributed messages, including messages you sent. Paginate with next_cursor as after_id. Read-only; polling and acknowledgment are separate. Any session_id is an opaque Setauket reference.")
    async def get_conversation(conversation_id: str, after_id: int = 0, limit: int = 30) -> dict:
        return {"conversation": await anyio.to_thread.run_sync(bus.conversation, conversation_id, after_id, limit)}

    @server.tool(description="Keyword-search durable messages across conversations; optionally filter by project_key. Returns conversation_id, sender_agent_id and timestamps. Use get_conversation for surrounding context. Messages are not automatically inferred as user preferences or stored as memory.")
    async def search_messages(query: str, project_key: str | None = None, limit: int = 30) -> dict:
        return {"results": await anyio.to_thread.run_sync(bus.search, query, project_key, limit)}

    def render(name, **context):
        return HTMLResponse(TEMPLATES.get_template(name).render(**context))

    @server.custom_route("/", methods=["GET"])
    async def dashboard(request: Request):
        with store.connect() as db:
            counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in ("agents", "conversations", "messages")}
            recent = [dict(row) for row in db.execute("SELECT id,title,created_at FROM conversations ORDER BY created_at DESC LIMIT 30")]
        return render("dashboard.html", counts=counts, recent=recent,
                      agents=await anyio.to_thread.run_sync(bus.agents))

    @server.custom_route("/agents", methods=["GET"])
    async def agents_page(request: Request):
        agents = await anyio.to_thread.run_sync(bus.agents, True)
        return render("agents.html", agents=agents)

    @server.custom_route("/conversations", methods=["GET"])
    async def conversations_page(request: Request):
        try:
            page = max(0, min(10000, int(request.query_params.get("page", "0"))))
        except ValueError:
            page = 0
        query = request.query_params.get("q", "")[:2000]
        project = request.query_params.get("project", "")[:500]
        if query.strip():
            results = await anyio.to_thread.run_sync(bus.search, query, project or None, 30)
            return render("conversations.html", results=results, conversations=[], query=query,
                          project=project, page=0, has_next=False)
        listing = await anyio.to_thread.run_sync(bus.conversations, page)
        return render("conversations.html", results=[], conversations=listing["conversations"],
                      query="", project=project, page=page, has_next=listing["has_more"])

    @server.custom_route("/conversations/{conversation_id}", methods=["GET"])
    async def conversation_page(request: Request):
        try:
            after_id = max(0, int(request.query_params.get("after", "0")))
        except ValueError:
            after_id = 0
        conversation = await anyio.to_thread.run_sync(bus.conversation, request.path_params["conversation_id"], after_id)
        return render("conversation.html", conversation=conversation) if conversation else PlainTextResponse("Not found", status_code=404)

    @server.custom_route("/config", methods=["GET"])
    async def config_page(request: Request):
        return render("config.html", config=config)

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request):
        return JSONResponse({"status": "ok"})

    @server.custom_route("/style.css", methods=["GET"])
    async def style(request: Request):
        return PlainTextResponse((Path(__file__).parent / "templates/style.css").read_text(), media_type="text/css")

    # SDK transport checks Host and Origin for /mcp. The outer middleware covers UI routes too.
    app = server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True, host=config.host,
        transport_security=TransportSecuritySettings(
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        ),
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    app.state.store, app.state.mcp, app.state.bus = store, server, bus
    return app