"""Backup/restore drill: a copied database must reopen and serve bus history identically."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from clothesline.bus import MessageBus
from clothesline.storage import Store


def run_cli(command: list[str], data_dir: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "clothesline.cli", *command],
                          env=os.environ | {"CLOTHESLINE_DATA_DIR": str(data_dir)},
                          capture_output=True, text=True, check=check)


def counts(path: Path) -> dict:
    db = sqlite3.connect(path)
    try:
        return {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ["harnesses", "agents", "conversations", "messages", "message_deliveries"]}
    finally:
        db.close()


@pytest.fixture
def seeded(tmp_path):
    store = Store(tmp_path / "clothesline.sqlite3")
    bus = MessageBus(store)
    harness = store.register_harness("drill-install", "Zed")
    agent = store.register_agent(harness, "drill-main")
    bus.online(harness, agent)
    conversation = bus.open_conversation(harness, agent, "Restore drill", session_id="setauket-session-fixture")
    bus.send(conversation, harness, agent, "m1", "Cross-harness restore drill message")
    return store, {"harness": harness, "agent": agent, "conversation": conversation}


def test_backup_and_restore_roundtrip(seeded, tmp_path):
    store, ids = seeded
    live = store.path.parent
    backup = tmp_path / "backup.sqlite3"

    result = run_cli(["backup", "--output", str(backup)], live)
    assert backup.exists()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert "Backup written to" in result.stdout

    # The backup itself is a consistent snapshot: same rows, clean integrity.
    assert counts(backup) == counts(store.path)
    check = sqlite3.connect(backup)
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert check.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        check.close()

    # Restore into an isolated data directory and reopen it with the real store path.
    restored_dir = tmp_path / "restored-data"
    restored_dir.mkdir()
    restored_db = restored_dir / "clothesline.sqlite3"
    shutil.copyfile(backup, restored_db)
    restored_db.chmod(0o600)
    restored = Store(restored_db)
    with restored.connect() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1

    # Message history, attribution and delivery state survived the round trip.
    conversation = MessageBus(restored).conversation(ids["conversation"])
    assert [message["body"] for message in conversation["messages"]] == ["Cross-harness restore drill message"]
    assert conversation["session_id"] == "setauket-session-fixture"

    # Keyword search over messages works from the restored copy alone.
    assert MessageBus(restored).search("drill")[0]["conversation_id"] == ids["conversation"]

    # The CLI operates on the restored directory as if it were home.
    doctor = json.loads(run_cli(["doctor"], restored_dir).stdout)
    assert doctor == {"integrity": "ok"}
    status = json.loads(run_cli(["status"], restored_dir).stdout)
    assert status["database"] == str(restored_db) and status["database_exists"] is True


def test_backup_requires_existing_database(tmp_path):
    result = run_cli(["backup", "--output", str(tmp_path / "unused.sqlite3")], tmp_path / "empty", check=False)
    assert result.returncode != 0