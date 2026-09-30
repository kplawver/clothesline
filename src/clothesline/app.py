"""One HTTP process for MCP and the local read-only browser interface."""

import logging
from contextlib import asynccontextmanager
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
from clothesline.models import LocalModels
from clothesline.storage import Store
from clothesline.worker import Worker

log = logging.getLogger(__name__)
TEMPLATES = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape())
TEMPLATES.filters["date"] = lambda timestamp: datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M UTC") if timestamp else "—"


def clean_blocks(blocks: list[dict]) -> str:
    """Only explicitly typed visible blocks may enter storage; never save reasoning."""
    if not isinstance(blocks, list):
        raise TypeError("Content must be a list of typed blocks")
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            raise TypeError("Blocks must be objects")
        if block.get("type") in {"reasoning", "thinking"}:
            continue
        if block.get("type") not in {"text", "tool"} or not isinstance(block.get("text"), str):
            raise ValueError("Only text and tool blocks are supported")
        parts.append(block["text"])
    content = "\n".join(parts).strip()
    if len(content) > 100_000:
        raise ValueError("Turn exceeds 100,000 characters")
    return content


def create_app(config: Config, store: Store | None = None, models: LocalModels | None = None,
               start_worker: bool = True):
    store = store or Store(config.database)
    models = models or LocalModels(config.model_dir)
    worker = Worker(store, models)
    bus = MessageBus(store)
    server = MCPServer("clothesline", version="0.5.0", instructions=(
        "Shared, local memory. Register a persistent harness installation key and an agent first. "
        "Submit visible session turns explicitly; connecting alone does not capture transcripts. "
        "Search before assuming a previous decision is current."))

    @server.tool(description="Register one installation of a coding harness. Persist installation_key locally and reuse it on every reconnect. Then call register_agent; a connection does not create identity automatically.")
    async def register_harness(installation_key: str, name: str) -> dict:
        return {"harness_id": await anyio.to_thread.run_sync(store.register_harness, installation_key, name)}

    @server.tool(description="Register an agent or sub-agent within a registered harness; reuse external_id for the same agent. Pass parent_id for a sub-agent. Use the returned agent_id on every turn and memory write.")
    async def register_agent(harness_id: str, external_id: str, parent_id: str | None = None) -> dict:
        return {"agent_id": await anyio.to_thread.run_sync(store.register_agent, harness_id, external_id, parent_id)}

    @server.tool(description="Start a session of explicitly submitted turns. Set project_key to a stable repository identifier/path; store the session_id for subsequent append_turn and end_session calls. Connecting alone does not capture history.")
    async def start_session(harness_id: str, agent_id: str, project_key: str | None = None) -> dict:
        return {"session_id": await anyio.to_thread.run_sync(store.start_session, harness_id, agent_id, project_key)}

    @server.tool(description="Submit a visible conversation turn. Use a stable source_id so retries are safe; blocks are typed text/tool/reasoning/thinking objects with text. Reasoning/thinking blocks are discarded before storage. Call for each turn; MCP cannot capture turns automatically.")
    async def append_turn(session_id: str, harness_id: str, agent_id: str, source_id: str,
                          role: str, blocks: list[dict]) -> dict:
        content = clean_blocks(blocks)
        return {"turn_id": await anyio.to_thread.run_sync(store.add_turn, session_id, harness_id, agent_id, source_id, role, content)}

    @server.tool(description="Mark a session ended; new turns may reopen it until archival. An inactive session is summarized after 72 hours. Search_sessions retrieves it later.")
    async def end_session(session_id: str, harness_id: str) -> dict:
        await anyio.to_thread.run_sync(store.end_session, session_id, harness_id)
        return {"ended": True}

    @server.tool(description="Find past turns, archived summaries, and current decisions/preferences with lexical plus semantic search. Filter by project_key or category (hot/cold/memory). Use session_id from hot/cold results with get_session, or source_id from memory results with get_memory_history. Keyword search still works when models are unavailable.")
    async def search_sessions(query: str, project_key: str | None = None, category: str | None = None,
                              limit: int = 10) -> dict:
        if not query.strip() or len(query) > 2000:
            raise ValueError("Search query must contain 1–2000 characters")
        try:
            vector = await anyio.to_thread.run_sync(models.embed, query, True)
        except Exception as error:  # noqa: BLE001 - keyword recall must survive any model failure.
            log.warning("Semantic search unavailable: %s", type(error).__name__)
            vector = None
        results = await anyio.to_thread.run_sync(store.search_rows, query, project_key, category, limit, vector)
        return {"results": results, "semantic_available": vector is not None}

    @server.tool(description="Retrieve a session by session_id. Hot sessions have turns; archived sessions contain a lossy generated summary, not the original transcript. Session metadata and dates remain available.")
    async def get_session(session_id: str) -> dict:
        return {"session": await anyio.to_thread.run_sync(store.get_session, session_id)}

    @server.tool(description="Save an explicitly user-requested decision or preference. Do not save inferred agent conclusions. Set project_key for project-specific memories; omit for global. To change one, pass supersedes_id and inspect get_memory_history. This is local attribution, not authentication.")
    async def remember(kind: str, content: str, harness_id: str, agent_id: str,
                       user_requested: bool, project_key: str | None = None, supersedes_id: str | None = None) -> dict:
        if not user_requested:
            raise ValueError("Only explicit user-requested memories can be saved")
        memory_id = await anyio.to_thread.run_sync(store.remember, kind, content, harness_id, agent_id, project_key, supersedes_id)
        return {"memory_id": memory_id}

    @server.tool(description="Read a decision/preference revision chain from oldest to newest. Superseded records are retained for history but not returned by default search; use the last entry as current guidance.")
    async def get_memory_history(memory_id: str) -> dict:
        return {"history": await anyio.to_thread.run_sync(store.get_memory, memory_id)}

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

    @server.tool(description="Create a conversation for an online agent. Optional project_key groups conversations; optional session_id connects one to a session. Use the returned conversation_id with send_message and get_conversation.")
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

    @server.tool(description="Read a conversation and its attributed messages, including messages you sent. Paginate with next_cursor as after_id. Associated session_id can be passed to get_session. Read-only; polling and acknowledgment are separate.")
    async def get_conversation(conversation_id: str, after_id: int = 0, limit: int = 30) -> dict:
        return {"conversation": await anyio.to_thread.run_sync(bus.conversation, conversation_id, after_id, limit)}

    @server.tool(description="Keyword-search durable messages across conversations; optionally filter by project_key. Returns conversation_id, sender_agent_id and timestamps. Use get_conversation for surrounding context. Messages are not automatically inferred as user preferences.")
    async def search_messages(query: str, project_key: str | None = None, limit: int = 30) -> dict:
        return {"results": await anyio.to_thread.run_sync(bus.search, query, project_key, limit)}

    def render(name, **context):
        return HTMLResponse(TEMPLATES.get_template(name).render(**context))

    @server.custom_route("/", methods=["GET"])
    async def dashboard(request: Request):
        with store.connect() as db:
            counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in ("sessions", "summaries", "memories", "turns", "conversations", "messages")}
            recent = [dict(row) for row in db.execute("SELECT s.id,s.started_at,s.archived_at,p.project_key FROM sessions s LEFT JOIN projects p ON s.project_id=p.id ORDER BY s.started_at DESC LIMIT 30")]
            failures = [dict(row) for row in db.execute("SELECT kind,source_id,attempts,error FROM jobs WHERE error IS NOT NULL ORDER BY id DESC LIMIT 10")]
        return render("dashboard.html", counts=counts, recent=recent, failures=failures,
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

    @server.custom_route("/memories", methods=["GET"])
    async def memories_page(request: Request):
        try:
            page = max(0, min(10000, int(request.query_params.get("page", "0"))))
        except ValueError:
            page = 0
        with store.connect() as db:
            memories = [dict(row) for row in db.execute(
                "SELECT m.*,p.project_key FROM memories m LEFT JOIN projects p ON m.project_id=p.id "
                "WHERE m.superseded_by IS NULL ORDER BY m.created_at DESC LIMIT 31 OFFSET ?", (page * 30,))]
        return render("memories.html", memories=memories[:30], page=page, has_next=len(memories) > 30)

    @server.custom_route("/sessions", methods=["GET"])
    async def sessions_page(request: Request):
        try:
            page = max(0, min(10000, int(request.query_params.get("page", "0"))))
        except ValueError:
            page = 0
        with store.connect() as db:
            sessions = [dict(row) for row in db.execute(
                "SELECT s.*,p.project_key FROM sessions s LEFT JOIN projects p ON s.project_id=p.id "
                "ORDER BY s.started_at DESC LIMIT 31 OFFSET ?", (page * 30,))]
        return render("sessions.html", sessions=sessions[:30], page=page, has_next=len(sessions) > 30)

    @server.custom_route("/search", methods=["GET"])
    async def search_page(request: Request):
        query = request.query_params.get("q", "")[:2000]
        project = request.query_params.get("project", "")[:500] or None
        category = request.query_params.get("category", "") or None
        results = []
        if query.strip():
            # Web search remains usable while models are offline.
            results = await anyio.to_thread.run_sync(store.search_rows, query, project, category, 30)
        return render("search.html", query=query, project=project or "", category=category or "", results=results)

    @server.custom_route("/sessions/{session_id}", methods=["GET"])
    async def session_page(request: Request):
        session = await anyio.to_thread.run_sync(store.get_session, request.path_params["session_id"])
        return render("session.html", session=session) if session else PlainTextResponse("Not found", status_code=404)

    @server.custom_route("/memories/{memory_id}", methods=["GET"])
    async def memory_page(request: Request):
        history = await anyio.to_thread.run_sync(store.get_memory, request.path_params["memory_id"])
        return render("memory.html", history=history) if history else PlainTextResponse("Not found", status_code=404)

    @server.custom_route("/config", methods=["GET"])
    async def config_page(request: Request):
        return render("config.html", config=config, embedding_ready=(models.embedding_path / "model_optimized.onnx").exists(),
                      text_ready=models.text_path.exists())

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
    sdk_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with sdk_lifespan(application):
            if start_worker:
                worker.start()
            try:
                yield
            finally:
                if start_worker:
                    await anyio.to_thread.run_sync(worker.stop)

    app.router.lifespan_context = lifespan
    app.state.store, app.state.models, app.state.worker, app.state.mcp, app.state.bus = store, models, worker, server, bus
    return app
