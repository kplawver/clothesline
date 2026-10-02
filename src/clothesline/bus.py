"""Durable, explicitly polled conversations between registered agents."""

import re
import time

from clothesline import identity
from clothesline.identity import uid
from clothesline.storage import MAX_SESSION_REFERENCE, Store

STALE_AFTER = 60 * 60
HEARTBEAT_AFTER = 15 * 60


class MessageBus:
    def __init__(self, store: Store):
        self.store = store

    def online(self, harness_id: str, agent_id: str) -> None:
        with self.store.connect() as db:
            identity.actor(db, harness_id, agent_id)
            now = time.time()
            db.execute("INSERT INTO agent_presence VALUES (?,?,?,NULL) "
                       "ON CONFLICT(agent_id) DO UPDATE SET online_at=excluded.online_at, "
                       "last_seen_at=excluded.last_seen_at, offline_at=NULL", (agent_id, now, now))

    def _touch(self, db, harness_id: str, agent_id: str) -> None:
        identity.actor(db, harness_id, agent_id)
        now = time.time()
        updated = db.execute("UPDATE agent_presence SET last_seen_at=? WHERE agent_id=? "
                             "AND offline_at IS NULL AND last_seen_at>?",
                             (now, agent_id, now - STALE_AFTER))
        if updated.rowcount != 1:
            raise ValueError("Agent is offline or stale; call agent_online first")

    def heartbeat(self, harness_id: str, agent_id: str) -> None:
        with self.store.connect() as db:
            self._touch(db, harness_id, agent_id)

    def offline(self, harness_id: str, agent_id: str) -> bool:
        with self.store.connect() as db:
            identity.actor(db, harness_id, agent_id)
            updated = db.execute("UPDATE agent_presence SET offline_at=? WHERE agent_id=? AND offline_at IS NULL",
                                 (time.time(), agent_id))
            return updated.rowcount == 1

    def agents(self, include_inactive: bool = False) -> list[dict]:
        with self.store.connect() as db:
            cutoff = time.time() - STALE_AFTER
            where = "" if include_inactive else "WHERE p.offline_at IS NULL AND p.last_seen_at>?"
            params = () if include_inactive else (cutoff,)
            rows = db.execute("SELECT a.id,a.external_id,a.parent_id,a.harness_id,h.name AS harness_name,"
                              "p.online_at,p.last_seen_at,p.offline_at FROM agent_presence p "
                              "JOIN agents a ON a.id=p.agent_id JOIN harnesses h ON h.id=a.harness_id "
                              f"{where} ORDER BY p.last_seen_at DESC LIMIT 200", params).fetchall()
            return [{**dict(row), "status": ("offline" if row["offline_at"] is not None else
                    "stale" if row["last_seen_at"] <= cutoff else "online")} for row in rows]

    def open_conversation(self, harness_id: str, agent_id: str, title: str,
                          project_key: str | None = None, session_id: str | None = None) -> str:
        if not title.strip() or len(title) > 200:
            raise ValueError("Conversation title must contain 1–200 characters")
        if session_id and (len(session_id) > MAX_SESSION_REFERENCE or not session_id.strip()):
            raise ValueError("Session reference must be a short opaque identifier")
        with self.store.connect() as db:
            self._touch(db, harness_id, agent_id)
            # Sessions live in Setauket, so a session reference is stored but never validated here.
            conversation_id = uid()
            db.execute("INSERT INTO conversations VALUES (?,?,?,?,?)",
                       (conversation_id, title.strip(), identity.project(db, project_key), session_id, time.time()))
            return conversation_id

    def send(self, conversation_id: str, harness_id: str, agent_id: str, client_message_id: str,
             body: str, recipient_agent_id: str | None = None) -> int:
        if not client_message_id or len(client_message_id) > 256 or not body.strip() or len(body) > 100_000:
            raise ValueError("A stable message ID and 1–100,000 characters of content are required")
        if recipient_agent_id == agent_id:
            raise ValueError("An agent cannot send a message to itself")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._touch(db, harness_id, agent_id)
            if not db.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone():
                raise ValueError("Unknown conversation")
            existing = db.execute("SELECT id,conversation_id,body,recipient_agent_id FROM messages "
                                  "WHERE sender_agent_id=? AND client_message_id=?", (agent_id, client_message_id)).fetchone()
            if existing:
                if (existing["conversation_id"], existing["body"], existing["recipient_agent_id"]) != (conversation_id, body, recipient_agent_id):
                    raise ValueError("Message ID already used for different content")
                return existing["id"]
            if recipient_agent_id:
                if not db.execute("SELECT 1 FROM agents WHERE id=?", (recipient_agent_id,)).fetchone():
                    raise ValueError("Unknown recipient")
                recipients = [recipient_agent_id]
            else:
                recipients = [row[0] for row in db.execute(
                    "SELECT agent_id FROM agent_presence WHERE agent_id<>? AND offline_at IS NULL "
                    "AND last_seen_at>?", (agent_id, time.time() - STALE_AFTER))]
            cursor = db.execute("INSERT INTO messages(conversation_id,sender_harness_id,sender_agent_id,"
                                "recipient_agent_id,client_message_id,body,created_at) VALUES (?,?,?,?,?,?,?)",
                                (conversation_id, harness_id, agent_id, recipient_agent_id,
                                 client_message_id, body, time.time()))
            message_id = cursor.lastrowid
            db.execute("INSERT INTO messages_fts(body,message_id) VALUES (?,?)", (body, message_id))
            db.executemany("INSERT INTO message_deliveries(message_id,recipient_agent_id) VALUES (?,?)",
                           ((message_id, recipient) for recipient in recipients))
            return message_id

    def inbox(self, harness_id: str, agent_id: str, after_id: int = 0, limit: int = 30) -> dict:
        if after_id < 0 or not 1 <= limit <= 50:
            raise ValueError("after_id must be nonnegative and limit must be between 1 and 50")
        with self.store.connect() as db:
            self._touch(db, harness_id, agent_id)
            rows = db.execute("SELECT m.* FROM message_deliveries d JOIN messages m ON m.id=d.message_id "
                              "WHERE d.recipient_agent_id=? AND d.acknowledged_at IS NULL AND m.id>? "
                              "ORDER BY m.id LIMIT ?", (agent_id, after_id, limit + 1)).fetchall()
            messages = [dict(row) for row in rows[:limit]]
            return {"messages": messages, "has_more": len(rows) > limit,
                    "next_cursor": messages[-1]["id"] if messages else after_id}

    def acknowledge(self, harness_id: str, agent_id: str, message_id: int) -> None:
        with self.store.connect() as db:
            self._touch(db, harness_id, agent_id)
            if not db.execute("SELECT 1 FROM message_deliveries WHERE recipient_agent_id=? AND message_id=?",
                              (agent_id, message_id)).fetchone():
                raise ValueError("Message was not delivered to this agent")
            db.execute("UPDATE message_deliveries SET acknowledged_at=COALESCE(acknowledged_at,?) "
                       "WHERE recipient_agent_id=? AND message_id=?", (time.time(), agent_id, message_id))

    def conversation(self, conversation_id: str, after_id: int = 0, limit: int = 30) -> dict | None:
        if after_id < 0 or not 1 <= limit <= 50:
            raise ValueError("after_id must be nonnegative and limit must be between 1 and 50")
        with self.store.connect() as db:
            row = db.execute("SELECT c.*,p.project_key FROM conversations c "
                             "LEFT JOIN projects p ON p.id=c.project_id WHERE c.id=?", (conversation_id,)).fetchone()
            if not row:
                return None
            rows = db.execute("SELECT m.*,h.name AS harness_name,a.external_id AS agent_name "
                              "FROM messages m JOIN harnesses h ON h.id=m.sender_harness_id "
                              "JOIN agents a ON a.id=m.sender_agent_id WHERE m.conversation_id=? "
                              "AND m.id>? ORDER BY m.id LIMIT ?", (conversation_id, after_id, limit + 1)).fetchall()
            messages = [dict(item) for item in rows[:limit]]
            return {**dict(row), "messages": messages, "has_more": len(rows) > limit,
                    "next_cursor": messages[-1]["id"] if messages else after_id}

    def conversations(self, page: int = 0, limit: int = 30) -> dict:
        if not 0 <= page <= 10000 or not 1 <= limit <= 50:
            raise ValueError("Invalid page or limit")
        with self.store.connect() as db:
            rows = db.execute("SELECT c.*,p.project_key, "
                              "(SELECT MAX(id) FROM messages WHERE conversation_id=c.id) AS last_message_id "
                              "FROM conversations c LEFT JOIN projects p ON p.id=c.project_id "
                              "ORDER BY COALESCE(last_message_id,0) DESC,c.created_at DESC LIMIT ? OFFSET ?",
                              (limit + 1, page * limit)).fetchall()
            return {"conversations": [dict(row) for row in rows[:limit]], "has_more": len(rows) > limit}

    def search(self, query: str, project_key: str | None = None, limit: int = 30) -> list[dict]:
        if not query.strip() or len(query) > 2000 or not 1 <= limit <= 50:
            raise ValueError("Search requires a query of up to 2000 characters and limit 1–50")
        terms = [re.sub(r"[^\w]", "", word) for word in query.split()][:12]
        terms = [term for term in terms if term]
        if not terms:
            return []
        expression = " OR ".join('"' + term + '"' for term in terms)
        with self.store.connect() as db:
            rows = db.execute("SELECT m.id,m.body,m.created_at,m.conversation_id,m.sender_agent_id,"
                              "p.project_key,c.title FROM messages_fts f "
                              "JOIN messages m ON m.id=CAST(f.message_id AS INTEGER) "
                              "JOIN conversations c ON c.id=m.conversation_id "
                              "LEFT JOIN projects p ON p.id=c.project_id "
                              "WHERE messages_fts MATCH ? AND (? IS NULL OR p.project_key=?) "
                              "ORDER BY bm25(messages_fts) LIMIT ?",
                              (expression, project_key, project_key, limit)).fetchall()
            return [dict(row) for row in rows]
