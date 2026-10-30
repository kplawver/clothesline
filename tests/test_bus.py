import json
import sqlite3
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from clothesline import identity
from clothesline.app import create_app
from clothesline.bus import MessageBus
from clothesline.config import Config
from clothesline.storage import SCHEMA_VERSION, Store


@pytest.fixture
def actors(tmp_path):
    store = Store(tmp_path / "clothesline.sqlite3")
    bus = MessageBus(store)
    first = store.register_harness("zed-install", "Zed")
    second = store.register_harness("omp-install", "Oh My Pi")
    sender = store.register_agent(first, "zed-main")
    receiver = store.register_agent(second, "omp-main")
    third = store.register_agent(second, "omp-sub", receiver)
    bus.online(first, sender)
    bus.online(second, receiver)
    return store, bus, (first, sender), (second, receiver), (second, third)


def test_schema_is_current_and_reopens_without_migration(tmp_path):
    path = tmp_path / "existing.sqlite3"
    store = Store(path)
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    reopened = Store(path)
    harness = reopened.register_harness("stable", "Zed")
    agent = reopened.register_agent(harness, "main")
    assert reopened.register_agent(harness, "main") == agent
    MessageBus(reopened).online(harness, agent)
    with store.connect() as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="Unsupported database version"):
        Store(path)


def test_version_one_database_gains_status_and_creator_columns(tmp_path):
    path = tmp_path / "v1.sqlite3"
    db = sqlite3.connect(path)
    db.executescript(identity.IDENTITY_SCHEMA + """
        CREATE TABLE agent_presence (agent_id TEXT PRIMARY KEY REFERENCES agents(id), online_at REAL NOT NULL,
          last_seen_at REAL NOT NULL, offline_at REAL);
        CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, project_id TEXT REFERENCES projects(id),
          session_id TEXT, created_at REAL NOT NULL);
        INSERT INTO harnesses VALUES ('h','key','Zed',0);
        INSERT INTO agents VALUES ('a','h','main',NULL,0);
        INSERT INTO conversations VALUES ('c','Before upgrade',NULL,NULL,0);
        PRAGMA user_version=1;""")
    db.close()
    bus = MessageBus(Store(path))
    bus.online("h", "a")
    bus.set_status("h", "a", "Upgraded in place")
    assert bus.agents()[0]["status_message"] == "Upgraded in place"
    assert bus.conversation("c")["created_by"] is None
    opened = bus.open_conversation("h", "a", "After upgrade")
    assert bus.agent_conversations(["a"])["a"][0]["id"] == opened


def test_status_message_is_published_and_cleared_on_sign_off(actors):
    _, bus, (h1, sender), _, _ = actors
    bus.set_status(h1, sender, "  Reviewing the\nmigration   plan  ")
    assert next(a for a in bus.agents() if a["id"] == sender)["status_message"] == "Reviewing the migration plan"
    for bad in ("   ", "x" * 201):
        with pytest.raises(ValueError):
            bus.set_status(h1, sender, bad)
    bus.offline(h1, sender)
    assert next(a for a in bus.agents(True) if a["id"] == sender)["status_message"] is None
    with pytest.raises(ValueError, match="offline"):
        bus.set_status(h1, sender, "Too late")


def test_owner_message_reaches_every_participant_and_only_them(actors):
    store, bus, (h1, sender), (h2, receiver), _ = actors
    bystander_harness = store.register_harness("bystander", "Codex")
    bystander = store.register_agent(bystander_harness, "codex-main")
    bus.online(bystander_harness, bystander)
    conversation = bus.open_conversation(h1, sender, "Plan")
    bus.send(conversation, h1, sender, "q", "Question for you", receiver)
    owner = bus.save_owner("Kevin", "Owns this machine")
    posted = bus.owner_send(owner, conversation, "form-1", "Both of you: ship it")
    assert bus.owner_send(owner, conversation, "form-1", "Both of you: ship it") == posted  # resubmitted form
    for harness, agent in ((h1, sender), (h2, receiver)):
        delivered = [m for m in bus.inbox(harness, agent)["messages"] if m["id"] == posted]
        assert delivered and delivered[0]["owner_name"] == "Kevin"
    assert bus.inbox(bystander_harness, bystander)["messages"] == []
    assert bus.conversation(conversation)["messages"][-1]["owner_name"] == "Kevin"
    assert {c["id"] for c in bus.agent_conversations([receiver])[receiver]} == {conversation}
    owner_agent = bus.owners()[0]["agent_id"]
    with pytest.raises(ValueError, match="browser"):  # an MCP client cannot impersonate the owner
        bus.online(store.register_harness("clothesline:owners", "Clothesline"), owner_agent)
    with pytest.raises(ValueError):
        bus.owner_send("nobody", conversation, "form-2", "Who am I?")


def test_session_reference_is_opaque_and_unvalidated(actors):
    """Setauket owns sessions, so Clothesline stores a reference without checking it."""
    store, bus, (h1, sender), _, _ = actors
    # A session that cannot exist in this database is still accepted and echoed back.
    conversation = bus.open_conversation(h1, sender, "Cross-service", session_id="not-a-real-session")
    assert bus.conversation(conversation)["session_id"] == "not-a-real-session"
    with pytest.raises(ValueError, match="opaque"):
        bus.open_conversation(h1, sender, "Too long", session_id="x" * 200)
    with store.connect() as db:
        # No sessions table remains, so the column carries no foreign key.
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='sessions'").fetchone()


def test_targeted_delivery_survives_offline_and_ack_is_idempotent(actors):
    _, bus, (h1, sender), (h2, receiver), _ = actors
    conversation = bus.open_conversation(h1, sender, "Feature review", project_key="project")
    assert bus.conversation(conversation)["project_key"] == "project"
    bus.offline(h2, receiver)
    message = bus.send(conversation, h1, sender, "event-1", "Please review the implementation", receiver)
    assert bus.send(conversation, h1, sender, "event-1", "Please review the implementation", receiver) == message
    with pytest.raises(ValueError):
        bus.send(conversation, h1, sender, "event-1", "different text", receiver)
    bus.online(h2, receiver)
    assert [m["id"] for m in bus.inbox(h2, receiver)["messages"]] == [message]
    with pytest.raises(ValueError):
        bus.acknowledge(h1, sender, message)
    assert bus.acknowledge(h2, receiver, message) is False
    assert bus.acknowledge(h2, receiver, message) is True  # idempotent, and the first ack clearly landed
    assert bus.inbox(h2, receiver)["messages"] == []
    assert bus.conversation(conversation)["messages"][0]["body"] == "Please review the implementation"


def test_broadcast_only_currently_online_and_pagination(actors):
    store, bus, (h1, sender), (h2, receiver), (h3, third) = actors
    conversation = bus.open_conversation(h1, sender, "Code handoff", project_key="repo")
    bus.online(h3, third)
    with store.connect() as db:
        db.execute("UPDATE agent_presence SET last_seen_at=? WHERE agent_id=?", (time.time() - 3601, third))
    assert {row["id"] for row in bus.agents()} == {sender, receiver}
    assert next(row["status"] for row in bus.agents(True) if row["id"] == third) == "stale"
    with pytest.raises(ValueError):
        bus.heartbeat(h3, third)
    first = bus.send(conversation, h1, sender, "one", "First routing message")
    assert [m["id"] for m in bus.inbox(h2, receiver)["messages"]] == [first]
    bus.online(h3, third)
    assert bus.inbox(h3, third)["messages"] == []
    second = bus.send(conversation, h1, sender, "two", "Second routing message")
    assert [m["id"] for m in bus.inbox(h3, third)["messages"]] == [second]
    assert bus.inbox(h2, receiver, limit=1)["has_more"]
    assert [m["id"] for m in bus.inbox(h2, receiver, after_id=first)["messages"]] == [second]
    bus.acknowledge(h2, receiver, first)
    assert [m["id"] for m in bus.inbox(h2, receiver)["messages"]] == [second]
    page = bus.conversation(conversation, limit=1)
    assert page["has_more"] and page["next_cursor"] == first
    assert bus.conversation(conversation, after_id=first)["messages"][0]["id"] == second


def test_search_reindex_and_validation(actors):
    store, bus, (h1, sender), (h2, receiver), _ = actors
    conversation = bus.open_conversation(h1, sender, "Notes", project_key="repo")
    msg = bus.send(conversation, h1, sender, "test", "Review violet keyboard shortcuts", receiver)
    assert bus.search("violet", "repo")[0]["conversation_id"] == conversation
    assert bus.search("violet", "other") == []
    with store.connect() as db:
        db.execute("DELETE FROM messages_fts")
    assert bus.search("violet") == []
    store.rebuild_indexes()
    assert bus.search("violet")[0]["id"] == msg
    with pytest.raises(ValueError):
        bus.send(conversation, h2, sender, "invalid", "actor mismatch", receiver)
    with pytest.raises(ValueError):
        bus.send(conversation, h1, sender, "self", "No self messages", sender)
    with pytest.raises(ValueError):
        bus.inbox(h2, receiver, limit=51)


def test_mcp_tools_and_browser(actors, tmp_path):
    store, _, (h1, sender), (h2, receiver), _ = actors
    app = create_app(Config(data_dir=tmp_path), store)
    with TestClient(app, base_url="http://127.0.0.1:19004") as client:
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2026-07-28"}

        def tool(name, arguments):
            response = client.post("/mcp", headers=headers | {"Mcp-Method": "tools/call", "Mcp-Name": name},
                                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                                       "name": name, "arguments": arguments, "_meta": {
                                           "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                           "io.modelcontextprotocol/clientCapabilities": {}}}})
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            assert not result.get("isError"), result
            return json.loads(result["content"][0]["text"])

        conv = tool("open_conversation", {"harness_id": h1, "agent_id": sender, "title": "UI test"})["conversation_id"]
        sent = tool("send_message", {"conversation_id": conv, "harness_id": h1, "agent_id": sender,
                                     "client_message_id": "request-1", "body": "A harmless inbox test",
                                     "recipient_agent_id": receiver})["message_id"]
        assert tool("poll_messages", {"harness_id": h2, "agent_id": receiver})["messages"][0]["id"] == sent
        assert "A harmless inbox test" in client.get(f"/conversations/{conv}").text
        assert "UI test" in client.get("/conversations").text
        assert "omp-main" in client.get("/agents").text
        assert "A harmless inbox test" in client.get("/conversations?q=harmless").text
        tool("ack_message", {"harness_id": h2, "agent_id": receiver, "message_id": sent})
        assert tool("poll_messages", {"harness_id": h2, "agent_id": receiver})["messages"] == []

        tool("set_status", {"harness_id": h2, "agent_id": receiver, "status": "Reviewing the UI test"})
        assert "Reviewing the UI test" in client.get("/").text
        assert client.post("/profile", data={"name": "Mallory"}).status_code == 403  # no Origin
        assert client.post("/profile", data={"name": "Mallory"},
                           headers={"Origin": "http://evil.example"}).status_code == 403
        origin = {"Origin": "http://127.0.0.1:19004"}
        assert client.post("/profile", data={"name": "Kevin", "about": "Owner"}, headers=origin,
                           follow_redirects=False).status_code == 303
        assert tool("agent_online", {"harness_id": h1, "agent_id": sender})["owners"][0]["name"] == "Kevin"
        owner_id = MessageBus(store).owners()[0]["id"]
        form = {"owner_id": owner_id, "client_message_id": "page-1", "body": "Owner says hello"}
        assert client.post(f"/conversations/{conv}/messages", data=form).status_code == 403
        assert client.post(f"/conversations/{conv}/messages", data=form, headers=origin,
                           follow_redirects=False).status_code == 303
        inbox = tool("poll_messages", {"harness_id": h2, "agent_id": receiver})["messages"]
        assert inbox[0]["body"] == "Owner says hello" and inbox[0]["owner_name"] == "Kevin"
        assert "Owner says hello" in client.get(f"/conversations/{conv}").text


def test_plugin_and_marketplace_manifests():
    root = Path(__file__).resolve().parents[1]
    plugin = root / "plugins/claude-code"
    manifest = json.loads((plugin / ".claude-plugin/plugin.json").read_text())
    marketplace = json.loads((root / ".claude-plugin/marketplace.json").read_text())
    mcp = json.loads((plugin / ".mcp.json").read_text())
    assert manifest["name"] == marketplace["plugins"][0]["name"] == "clothesline"
    assert marketplace["owner"]["name"] and isinstance(marketplace["plugins"], list)
    assert marketplace["plugins"][0]["source"] == "./plugins/claude-code"
    assert set(mcp["mcpServers"]) == {"clothesline"}
    assert mcp["mcpServers"]["clothesline"]["url"] == "http://127.0.0.1:19004/mcp"
    # Capture hooks and importers belong to Setauket; Clothesline hooks may only drive presence and polling.
    hooks = json.loads((plugin / "hooks" / "hooks.json").read_text())["hooks"]
    commands = {(h["command"], tuple(h["args"])) for group in hooks.values() for entry in group for h in entry["hooks"]}
    assert commands == {("clothesline", ("hook",))}
    for name in ("sessions", "memories", "models", "worker", "capture"):
        assert not (Path(root / "src/clothesline") / f"{name}.py").exists()


def test_agents_standard_layout_serves_one_copy_of_the_skill():
    """`.agents` and the plugin must not drift: both resolve to the same file."""
    root = Path(__file__).resolve().parents[1]
    skill = root / "plugins/claude-code/skills/clothesline-agents/SKILL.md"
    assert skill.is_file() and not skill.is_symlink()  # the plugin holds the real file
    linked = root / ".agents/skills/clothesline-agents"
    assert linked.is_symlink(), ".agents/skills must bridge to the plugin, not hold a copy"
    assert linked.resolve() == skill.parent.resolve()
    assert linked.joinpath("SKILL.md").read_bytes() == skill.read_bytes()
    frontmatter = skill.read_text().split("---")[1]
    assert "name: clothesline-agents" in frontmatter
    assert "description:" in frontmatter, "most harnesses require description frontmatter"
    # The composed instruction fragment Tallmadge merges into ~/.agents/agents.md.
    fragment = root / "plugins/claude-code/agents.md"
    assert fragment.is_file() and fragment.read_text().strip()
    assert (root / "AGENTS.md").is_file(), "canonical repo instructions are required"
