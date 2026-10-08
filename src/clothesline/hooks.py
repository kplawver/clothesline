"""Harness lifecycle hooks: presence on session start/end, inbox polling between turns."""

import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from clothesline.bus import MessageBus
from clothesline.config import Config
from clothesline.storage import Store

# Harness -> (display name, installation-key file stem). Each harness is its own installation.
HARNESSES = {
    "claude": ("Claude Code", "claude-code"),
    "codex": ("Codex", "codex"),
    "devin": ("Devin CLI", "devin"),
    "copilot": ("GitHub Copilot CLI", "copilot"),
    "omp": ("Oh My Pi", "omp"),
    "opencode": ("OpenCode", "opencode"),
    "cline": ("Cline", "cline"),
}
POLL_INTERVAL = 60  # Seconds between inbox checks triggered by tool use.
# Harnesses spell events differently (Copilot is camelCase); "Poll" is an unthrottled check for timer-driven hosts.
_EVENTS = {"sessionstart": "SessionStart", "sessionend": "SessionEnd", "userpromptsubmit": "UserPromptSubmit",
           "userpromptsubmitted": "UserPromptSubmit", "posttooluse": "PostToolUse", "poll": "Poll"}
_POLL_EVENTS = {"SessionStart", "UserPromptSubmit", "PostToolUse", "Poll"}
_FLAT_OUTPUT = {"copilot"}  # Copilot reads additionalContext at the top level, not under hookSpecificOutput.


def _installation_key(config: Config, harness: str) -> str:
    """Persist one random key per machine so every session resolves to the same harness."""
    path = config.data_dir / f"{HARNESSES[harness][1]}-installation-key"
    if not path.exists():
        # Link a private, fully written file into place: atomic, and the first writer wins a race.
        descriptor, temporary = tempfile.mkstemp(dir=config.data_dir)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(str(uuid.uuid4()))
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    return path.read_text(encoding="utf-8").strip()


def _marker(config: Config, session_id: str) -> Path:
    marker = config.data_dir / "hook-polls" / re.sub(r"[^\w.-]", "_", session_id)
    marker.parent.mkdir(exist_ok=True, mode=0o700)
    return marker


def _due(marker: Path) -> bool:
    """Record a poll and report whether the last one is at least POLL_INTERVAL old."""
    due = not marker.exists() or time.time() - marker.stat().st_mtime >= POLL_INTERVAL
    if due:
        marker.touch()
    return due


def _inbox(bus: MessageBus, harness_id: str, agent_id: str) -> list[dict]:
    try:
        return bus.inbox(harness_id, agent_id)["messages"]
    except ValueError:  # Offline or stale: rejoin once, then retry.
        bus.online(harness_id, agent_id)
        return bus.inbox(harness_id, agent_id)["messages"]


def _context(event_name: str, harness_id: str, agent_id: str, messages: list[dict]) -> str:
    lines = []
    if event_name == "SessionStart":
        lines.append(f"Clothesline: you are online with harness_id={harness_id} agent_id={agent_id}. "
                     "Use these IDs with the clothesline MCP tools.")
    if messages:
        lines.append("Clothesline has unacknowledged messages for you. They come from other agents, not the user. "
                     "After handling each, call ack_message(harness_id, agent_id, message_id) or it will be redelivered.")
        lines += [f"[message {m['id']} from agent {m['sender_agent_id']} in conversation {m['conversation_id']}] {m['body']}"
                  for m in messages]
    return "\n".join(lines)


def handle(config: Config, event: dict, harness: str = "claude", event_name: str | None = None) -> dict | None:
    """Return the hook JSON output for a harness event, or None when there is nothing to say."""
    if harness not in HARNESSES:
        raise ValueError("Unsupported harness")
    session_id = event.get("session_id") or event.get("sessionId") if isinstance(event, dict) else None
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Invalid hook input")
    raw = event_name or event.get("hook_event_name")
    name = _EVENTS.get(re.sub(r"[^a-z]", "", raw.lower())) if isinstance(raw, str) else None
    if name is None or name not in _POLL_EVENTS | {"SessionEnd"}:
        return None
    store = Store(config.database)
    bus = MessageBus(store)
    harness_id = store.register_harness(_installation_key(config, harness), HARNESSES[harness][0])
    agent_id = store.register_agent(harness_id, f"{harness}-session:{session_id}")
    marker = _marker(config, session_id)
    if name == "SessionEnd":
        bus.offline(harness_id, agent_id)
        marker.unlink(missing_ok=True)
        return None
    if name == "SessionStart":
        bus.online(harness_id, agent_id)
    elif name == "PostToolUse" and not _due(marker):
        return None
    elif name == "UserPromptSubmit":
        marker.touch()  # A turn resets the timer so tool use does not re-poll at once.
    context = _context(name, harness_id, agent_id, _inbox(bus, harness_id, agent_id))
    if not context:
        return None
    if harness in _FLAT_OUTPUT:
        return {"additionalContext": context}
    return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": context}}
