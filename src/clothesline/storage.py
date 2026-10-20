"""SQLite persistence for agent identity and the durable message bus."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from clothesline import identity

MESSAGE_BUS_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_presence (
  agent_id TEXT PRIMARY KEY REFERENCES agents(id), online_at REAL NOT NULL,
  last_seen_at REAL NOT NULL, offline_at REAL, status_message TEXT, status_at REAL
);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, project_id TEXT REFERENCES projects(id),
  session_id TEXT, created_at REAL NOT NULL, created_by TEXT REFERENCES agents(id)
);
CREATE TABLE IF NOT EXISTS owners (
  id TEXT PRIMARY KEY, agent_id TEXT UNIQUE NOT NULL REFERENCES agents(id),
  name TEXT NOT NULL, about TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_recent ON conversations(created_at DESC);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  sender_harness_id TEXT NOT NULL REFERENCES harnesses(id),
  sender_agent_id TEXT NOT NULL REFERENCES agents(id),
  recipient_agent_id TEXT REFERENCES agents(id),
  client_message_id TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL,
  UNIQUE(sender_agent_id, client_message_id)
);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id,id);
CREATE TABLE IF NOT EXISTS message_deliveries (
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  recipient_agent_id TEXT NOT NULL REFERENCES agents(id), acknowledged_at REAL,
  PRIMARY KEY(message_id, recipient_agent_id)
);
CREATE INDEX IF NOT EXISTS deliveries_inbox ON message_deliveries(recipient_agent_id, acknowledged_at, message_id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(body, message_id UNINDEXED, tokenize='porter unicode61');
"""

MAX_SESSION_REFERENCE = 128
SCHEMA_VERSION = 2
# Columns added after version 1, applied to older databases on open.
_V2_COLUMNS = (("agent_presence", "status_message", "TEXT"), ("agent_presence", "status_at", "REAL"),
               ("conversations", "created_by", "TEXT REFERENCES agents(id)"))


class Store:
    """Identity and conversations only. Session turns live in Setauket."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        if self.path.exists():
            self.path.chmod(0o600)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, SCHEMA_VERSION):
                raise ValueError("Unsupported database version; upgrade Clothesline before opening it")
            db.executescript(identity.IDENTITY_SCHEMA)
            db.executescript(MESSAGE_BUS_SCHEMA)
            for table, column, kind in _V2_COLUMNS:
                if column not in {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def register_harness(self, installation_key: str, name: str) -> str:
        with self.connect() as db:
            return identity.register_harness(db, installation_key, name)

    def register_agent(self, harness_id: str, external_id: str, parent_id: str | None = None) -> str:
        with self.connect() as db:
            return identity.register_agent(db, harness_id, external_id, parent_id)

    def rebuild_indexes(self) -> int:
        """Restore the derived message index without touching stored message bodies."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM messages_fts")
            rows = db.execute("SELECT id,body FROM messages").fetchall()
            db.executemany("INSERT INTO messages_fts(body,message_id) VALUES (?,?)",
                           ((row["body"], row["id"]) for row in rows))
            return len(rows)