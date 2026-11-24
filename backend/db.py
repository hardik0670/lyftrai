import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.row_factory = sqlite3.Row
            yield connection

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    system_prompt TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    metadata TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_session_order ON messages(session_id, id);
            """)

    def check(self):
        with self.connect() as connection:
            connection.execute("SELECT 1 FROM messages LIMIT 1").fetchone()

    def load_session(self, session_id: str):
        with self.connect() as connection:
            session = connection.execute(
                "SELECT system_prompt FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None:
                return None, []

            rows = connection.execute(
                "SELECT role, content, metadata FROM messages WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()

        messages = []
        for row in rows:
            message = {"role": row["role"], "content": row["content"]}
            if row["metadata"] is not None:
                message["metadata"] = json.loads(row["metadata"])
            messages.append(message)
        return session["system_prompt"], messages

    def save_turn(self, session_id: str, prompt: str, message: str, reply: str, metadata: dict):
        created_at = datetime.now(timezone.utc).isoformat()
        with self.connect() as connection, connection:
            connection.execute(
                "INSERT INTO sessions (id, system_prompt) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET system_prompt = excluded.system_prompt",
                (session_id, prompt),
            )
            connection.executemany(
                "INSERT INTO messages (session_id, role, content, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (session_id, "user", message, None, created_at),
                    (session_id, "assistant", reply, json.dumps(metadata), created_at),
                ],
            )

    def delete_session(self, session_id: str) -> bool:
        with self.connect() as connection, connection:
            connection.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            deleted = connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return deleted.rowcount == 1
