"""Opt-in, per-project Claude Code hook capture."""

import json
import os
import tempfile
from pathlib import Path

from clothesline.config import Config
from clothesline.storage import Store


def _path(config: Config) -> Path:
    return config.data_dir / "claude-capture.json"


def _projects(config: Config) -> set[str]:
    path = _path(config)
    if not path.exists():
        return set()
    if path.stat().st_mode & 0o077:
        raise ValueError("Claude capture allowlist must have 0600 permissions")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("projects"), list) or not all(
        isinstance(project, str) and Path(project).is_absolute() for project in value["projects"]
    ):
        raise ValueError("Invalid Claude capture allowlist")
    return set(value["projects"])


def enabled(config: Config, project: Path) -> bool:
    return str(project.resolve(strict=True)) in _projects(config)


def set_enabled(config: Config, project: Path, enable: bool) -> bool:
    """Persist an exact-path allowlist atomically with restrictive permissions."""
    key = str(project.expanduser().resolve(strict=True))
    if not Path(key).is_dir():
        raise ValueError("Capture project must be a directory")
    projects = _projects(config)
    if enable:
        projects.add(key)
    else:
        projects.discard(key)
    path = _path(config)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix="claude-capture-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"projects": sorted(projects)}, stream, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return key in projects


def capture_hook(config: Config, event: dict) -> bool:
    """Use only Claude's visible event fields; never inspect transcript/tool data."""
    if not isinstance(event, dict):
        raise TypeError("Invalid Claude hook input")
    project = event.get("cwd")
    if not isinstance(project, str) or not Path(project).is_absolute() or not enabled(config, Path(project)):
        return False
    kind = event.get("hook_event_name")
    if kind not in {"UserPromptSubmit", "Stop"}:
        return False
    session_id = event.get("session_id")
    prompt_id = event.get("prompt_id")
    content = event.get("prompt" if kind == "UserPromptSubmit" else "last_assistant_message")
    if not isinstance(session_id, str) or not session_id or not isinstance(content, str) or not content.strip():
        return False
    if prompt_id is not None and not isinstance(prompt_id, str):
        raise ValueError("Invalid Claude prompt ID")
    role = "user" if kind == "UserPromptSubmit" else "assistant"
    transcript = event.get("transcript_path")
    transcript_path = str(Path(transcript).expanduser().resolve()) if isinstance(transcript, str) else None
    return Store(config.database).capture_claude(str(Path(project).resolve()), session_id, prompt_id,
                                                 role, content, transcript_path)
