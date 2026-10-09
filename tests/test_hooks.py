import os
import time

import pytest

from clothesline.bus import MessageBus
from clothesline.config import Config
from clothesline.hooks import POLL_INTERVAL, handle
from clothesline.storage import Store


def event(name, **extra):
    return {"hook_event_name": name, "session_id": "s1", **extra}


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path / "data")


def agent_status(config):
    return MessageBus(Store(config.database)).agents(include_inactive=True)[0]["status"]


def send_to_session(config, body, message_id="m1"):
    """Deliver a message to the hook-registered agent from a second agent."""
    store = Store(config.database)
    bus = MessageBus(store)
    with store.connect() as db:
        recipient = db.execute("SELECT id FROM agents").fetchone()[0]
    harness = store.register_harness("other", "Zed")
    sender = store.register_agent(harness, "zed")
    bus.online(harness, sender)
    bus.send(bus.open_conversation(harness, sender, "Hi"), harness, sender, message_id, body, recipient)


def test_session_lifecycle_toggles_presence_and_reuses_identity(config):
    start = handle(config, event("SessionStart"))["hookSpecificOutput"]
    assert start["hookEventName"] == "SessionStart"
    assert "agent_id=" in start["additionalContext"]
    assert agent_status(config) == "online"
    handle(config, event("SessionStart"))  # resume: same agent, not a second one
    assert len(MessageBus(Store(config.database)).agents(include_inactive=True)) == 1
    assert handle(config, event("SessionEnd")) is None
    assert agent_status(config) == "offline"


def test_prompt_surfaces_unacknowledged_messages_until_acked(config):
    handle(config, event("SessionStart"))
    assert handle(config, event("UserPromptSubmit")) is None
    send_to_session(config, "please review")
    for _ in range(2):  # at-least-once: redelivered until acknowledged
        context = handle(config, event("UserPromptSubmit"))["hookSpecificOutput"]["additionalContext"]
        assert "please review" in context and "ack_message" in context
    store = Store(config.database)
    with store.connect() as db:
        agent = db.execute("SELECT id,harness_id FROM agents WHERE external_id='claude-session:s1'").fetchone()
    MessageBus(store).acknowledge(agent["harness_id"], agent["id"], 1)
    assert handle(config, event("UserPromptSubmit")) is None


def test_tool_use_polls_at_most_once_per_interval(config):
    handle(config, event("SessionStart"))
    send_to_session(config, "first")
    assert "first" in handle(config, event("PostToolUse"))["hookSpecificOutput"]["additionalContext"]
    assert handle(config, event("PostToolUse")) is None  # throttled despite the unacked message
    marker = next((config.data_dir / "hook-polls").iterdir())
    old = time.time() - POLL_INTERVAL - 1
    os.utime(marker, (old, old))
    assert "first" in handle(config, event("PostToolUse"))["hookSpecificOutput"]["additionalContext"]


def test_prompt_rejoins_after_session_end_without_new_start(config):
    handle(config, event("SessionStart"))
    handle(config, event("SessionEnd"))
    handle(config, event("UserPromptSubmit"))
    assert agent_status(config) == "online"


def test_unrelated_events_and_bad_input_are_rejected_safely(config):
    assert handle(config, event("Stop")) is None
    assert not config.database.exists()
    with pytest.raises(ValueError):
        handle(config, {"hook_event_name": "SessionStart"})


def test_installation_key_is_private_and_stable_and_session_end_clears_marker(config):
    handle(config, event("SessionStart"))
    handle(config, event("UserPromptSubmit"))
    key = config.data_dir / "claude-code-installation-key"
    assert key.stat().st_mode & 0o777 == 0o600
    first = key.read_text()
    handle(config, event("SessionEnd"))
    handle(config, event("SessionStart", session_id="s2"))
    assert key.read_text() == first
    assert not list((config.data_dir / "hook-polls").glob("s1"))


def test_copilot_uses_camelcase_payload_event_argument_and_flat_output(config):
    payload = {"sessionId": "c1", "cwd": "/tmp"}  # Copilot's camelCase payloads name neither event nor snake_case id.
    start = handle(config, payload, "copilot", "sessionStart")
    assert set(start) == {"additionalContext"}
    assert agent_status(config) == "online"
    send_to_session(config, "from copilot")
    assert "from copilot" in handle(config, payload, "copilot", "postToolUse")["additionalContext"]
    handle(config, payload, "copilot", "sessionEnd")
    assert agent_status(config) == "offline"


@pytest.mark.parametrize("harness,name", [("codex", "Codex"), ("devin", "Devin CLI"), ("omp", "Oh My Pi"),
                                          ("opencode", "OpenCode"), ("cline", "Cline")])
def test_each_harness_is_its_own_installation(config, harness, name):
    handle(config, event("SessionStart"), "claude")
    handle(config, event("SessionStart"), harness)
    with Store(config.database).connect() as db:
        names = {row[0] for row in db.execute("SELECT name FROM harnesses")}
        assert {"Claude Code", name} == names
        assert db.execute("SELECT count(*) FROM agents WHERE external_id=?", (f"{harness}-session:s1",)).fetchone()[0] == 1


def test_poll_event_ignores_the_tool_use_throttle(config):
    handle(config, event("SessionStart"), "omp")
    send_to_session(config, "timer")
    assert "timer" in handle(config, event("Poll"), "omp")["hookSpecificOutput"]["additionalContext"]
    assert "timer" in handle(config, event("Poll"), "omp")["hookSpecificOutput"]["additionalContext"]
