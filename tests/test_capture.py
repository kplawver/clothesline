import io
import json
import sys
from pathlib import Path

import pytest

from clothesline.capture import capture_hook, set_enabled
from clothesline.cli import main
from clothesline.config import Config
from clothesline.storage import Store


def event(name, project, **extra):
    return {"hook_event_name": name, "cwd": str(project), "session_id": "claude-session-1",
            "prompt_id": "prompt-1", "transcript_path": "/nonexistent/private-transcript.jsonl", **extra}


def test_explicit_consent_visible_fields_and_duplicate_events(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = Config(data_dir=tmp_path / "data")
    user = event("UserPromptSubmit", project, prompt="Visible question",
                 thinking="private reasoning", tool_input={"secret": "private tool arguments"})
    assistant = event("Stop", project, last_assistant_message="Visible final answer",
                      tool_result="private tool output", last_assistant_thinking="private reasoning")
    assert not capture_hook(config, user)
    assert not config.database.exists()
    assert set_enabled(config, project, True)
    assert (config.data_dir / "claude-capture.json").stat().st_mode & 0o777 == 0o600
    assert config.data_dir.stat().st_mode & 0o777 == 0o700
    assert capture_hook(config, user)
    assert not capture_hook(config, user)
    assert capture_hook(config, assistant)
    assert not capture_hook(config, assistant)
    store = Store(config.database)
    with store.connect() as db:
        session = db.execute("SELECT * FROM sessions").fetchone()
        assert session["project_id"]
        assert db.execute("SELECT installation_key FROM harnesses").fetchone()[0] == "clothesline:claude:hook"
        assert db.execute("SELECT external_id FROM agents").fetchone()[0] == "claude-session:claude-session-1"
        assert db.execute("SELECT count(*) FROM capture_events").fetchone()[0] == 2
    turns = store.get_session(session["id"])["turns"]
    assert [turn["role"] for turn in turns] == ["user", "assistant"]
    assert [turn["content"] for turn in turns] == ["Visible question", "Visible final answer"]
    assert not store.search_rows("private")
    assert not capture_hook(config, event("UserPromptSubmit", tmp_path, prompt="Not allowed"))
    assert not set_enabled(config, project, False)
    assert not capture_hook(config, event("UserPromptSubmit", project, prompt="After disable", prompt_id="p2"))
    assert len(store.get_session(session["id"])["turns"]) == 2


def test_missing_prompt_id_repeated_prompt_and_archived_segment(tmp_path):
    config = Config(data_dir=tmp_path / "data")
    set_enabled(config, tmp_path, True)
    prompt = event("UserPromptSubmit", tmp_path, prompt="Same text", prompt_id=None)
    reply = event("Stop", tmp_path, last_assistant_message="Same reply", prompt_id=None)
    assert capture_hook(config, prompt)
    assert not capture_hook(config, prompt)
    assert capture_hook(config, reply)
    assert capture_hook(config, prompt)
    assert capture_hook(config, reply)
    store = Store(config.database)
    with store.connect() as db:
        session_id = db.execute("SELECT id FROM sessions").fetchone()[0]
    assert len(store.get_session(session_id)["turns"]) == 4
    session = store.get_session(session_id)
    assert store.archive(session_id, session["last_turn_at"], "Earlier answers", "fake")
    assert not capture_hook(config, reply)  # No duplicate after the raw turns expire.
    assert capture_hook(config, event("UserPromptSubmit", tmp_path, prompt="New prompt", prompt_id="new-id"))
    with store.connect() as db:
        assert db.execute("SELECT segment FROM capture_sources").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2
    assert store.get_session(session_id)["summary"]["content"] == "Earlier answers"


def test_prompt_id_reuse_with_different_content_rejected(tmp_path):
    config = Config(data_dir=tmp_path / "data")
    set_enabled(config, tmp_path, True)
    capture_hook(config, event("UserPromptSubmit", tmp_path, prompt="First"))
    with pytest.raises(ValueError, match="reused with different text"):
        capture_hook(config, event("UserPromptSubmit", tmp_path, prompt="Changed"))
    with Store(config.database).connect() as db:
        assert db.execute("SELECT count(*) FROM turns").fetchone()[0] == 1


def test_cli_hook_fail_open_and_per_project_status(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("CLOTHESLINE_DATA_DIR", str(data_dir))
    monkeypatch.setattr(sys, "argv", ["clothesline", "capture-claude", "--project", str(tmp_path), "--enable"])
    main()
    assert json.loads(capsys.readouterr().out)["capture_enabled"] is True
    monkeypatch.setattr(sys, "argv", ["clothesline", "capture-claude", "--hook"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event("UserPromptSubmit", tmp_path, prompt="CLI hook"))))
    main()
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(sys, "stdin", io.StringIO("not JSON (private)"))
    main()
    output = capsys.readouterr()
    assert output.out == ""
    assert "private" not in output.err
    monkeypatch.setattr(sys, "argv", ["clothesline", "capture-claude", "--project", str(tmp_path), "--disable"])
    main()
    assert json.loads(capsys.readouterr().out)["capture_enabled"] is False


def test_manual_import_and_hook_cannot_duplicate_same_claude_session(tmp_path, monkeypatch, capsys):
    source = tmp_path / "conversation.jsonl"
    entries = [
        {"type": "user", "uuid": "user-event", "parentUuid": None, "sessionId": "claude-session-1",
         "timestamp": "2026-09-30T10:00:00Z", "cwd": str(tmp_path),
         "message": {"role": "user", "content": "Already discussed"}},
        {"type": "assistant", "uuid": "assistant-event", "parentUuid": "user-event",
         "sessionId": "claude-session-1", "timestamp": "2026-09-30T10:00:01Z", "cwd": str(tmp_path),
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Already answered"}]}}
    ]
    source.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
    config = Config(data_dir=tmp_path / "data")
    monkeypatch.setenv("CLOTHESLINE_DATA_DIR", str(config.data_dir))
    set_enabled(config, tmp_path, True)
    hook_event = event("UserPromptSubmit", tmp_path, prompt="Already discussed", transcript_path=str(source))
    assert capture_hook(config, hook_event)
    monkeypatch.setattr(sys, "argv", ["clothesline", "import-claude", "--source", str(source)])
    with pytest.raises(ValueError, match="already captured by hook"):
        main()
    assert capsys.readouterr().out == ""
    with Store(config.database).connect() as db:
        assert db.execute("SELECT count(*) FROM turns").fetchone()[0] == 1

    second = tmp_path / "import-first.jsonl"
    second.write_text(source.read_text().replace("claude-session-1", "another-session"))
    monkeypatch.setattr(sys, "argv", ["clothesline", "import-claude", "--source", str(second)])
    main()
    capsys.readouterr()
    with pytest.raises(ValueError, match="already imported manually"):
        capture_hook(config, event("UserPromptSubmit", tmp_path, session_id="another-session",
                                   prompt="Already discussed", transcript_path=str(second)))


def test_v4_upgrade_preserves_history(tmp_path):
    store = Store(tmp_path / "memory.db")
    h = store.register_harness("other", "Other")
    a = store.register_agent(h, "agent")
    session = store.start_session(h, a)
    store.add_turn(session, h, a, "1", "user", "Remember this")
    with store.connect() as db:
        db.execute("DROP TABLE capture_events")
        db.execute("DROP TABLE capture_sources")
        db.execute("PRAGMA user_version=4")
    upgraded = Store(store.path)
    assert upgraded.get_session(session)["turns"][0]["content"] == "Remember this"
    with upgraded.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert db.execute("SELECT count(*) FROM capture_sources").fetchone()[0] == 0


def test_plugin_files_are_valid_json():
    root = Path(__file__).resolve().parents[1]
    plugin = root / "plugins/claude-code"
    manifest = json.loads((plugin / ".claude-plugin/plugin.json").read_text())
    marketplace = json.loads((root / ".claude-plugin/marketplace.json").read_text())
    mcp = json.loads((plugin / ".mcp.json").read_text())
    hooks = json.loads((plugin / "hooks/hooks.json").read_text())
    assert manifest["name"] == marketplace["plugins"][0]["name"] == "clothesline"
    assert mcp["mcpServers"]["clothesline"]["url"] == "http://127.0.0.1:19004/mcp"
    assert set(hooks["hooks"]) == {"UserPromptSubmit", "Stop"}
    assert (plugin / "skills/memory/SKILL.md").exists()
