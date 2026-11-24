"""SQLite store: the single source of truth for draft status (the graph checkpoint
only holds control flow). Thread-safe because parallel graph branches write here."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS processed(
  message_id TEXT PRIMARY KEY, outcome TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drafts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id TEXT UNIQUE NOT NULL,
  thread_id TEXT NOT NULL,
  to_addr TEXT NOT NULL,
  subject TEXT NOT NULL,
  rfc_message_id TEXT,
  references_hdr TEXT,
  orig_from TEXT NOT NULL,
  orig_subject TEXT NOT NULL,
  orig_excerpt TEXT NOT NULL,
  priority TEXT NOT NULL DEFAULT 'normal',
  body TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | manual
  feedback TEXT NOT NULL DEFAULT '[]',
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  sent_at TEXT);
CREATE INDEX IF NOT EXISTS ix_drafts_status ON drafts(status);
CREATE TABLE IF NOT EXISTS usage(
  day TEXT, model TEXT, calls INTEGER, prompt INTEGER, cached INTEGER, completion INTEGER,
  PRIMARY KEY(day, model));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

_PRIORITY_ORDER = "CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, args)

    # ---- processed messages ----
    def processed_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        q = f"SELECT message_id FROM processed WHERE message_id IN ({','.join('?' * len(ids))})"
        return {r[0] for r in self._exec(q, tuple(ids))}

    def mark_processed(self, message_id: str, outcome: str) -> None:
        self._exec("INSERT OR REPLACE INTO processed VALUES(?,?,?)", (message_id, outcome, _now()))

    # ---- drafts ----
    def insert_draft(self, email: dict, body: str, priority: str = "normal") -> int:
        subj = email.get("subject") or ""
        reply_subject = subj if subj.lower().startswith(("re:", "השב:")) else f"Re: {subj}".strip()
        now = _now()
        with self.lock:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO drafts(message_id, thread_id, to_addr, subject, rfc_message_id,
                   references_hdr, orig_from, orig_subject, orig_excerpt, priority, body,
                   created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (email["id"], email["thread_id"], email.get("reply_to") or email["from"], reply_subject,
                 email.get("rfc_message_id"), email.get("references"), email["from"], subj,
                 email["body"], priority, body, now, now),
            )
            self.conn.execute("INSERT OR REPLACE INTO processed VALUES(?,?,?)", (email["id"], "drafted", now))
            if cur.lastrowid and cur.rowcount:
                return int(cur.lastrowid)
            row = self.conn.execute("SELECT id FROM drafts WHERE message_id=?", (email["id"],)).fetchone()
            return int(row[0])

    def get_draft(self, draft_id: int) -> dict | None:
        row = self._exec("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        return dict(row) if row else None

    def pending_drafts(self) -> list[dict]:
        rows = self._exec(f"SELECT * FROM drafts WHERE status='pending' ORDER BY {_PRIORITY_ORDER}, created_at")
        return [dict(r) for r in rows]

    def update_body(self, draft_id: int, body: str, new_attempt: bool) -> None:
        self._exec(
            "UPDATE drafts SET body=?, attempts=attempts+?, updated_at=? WHERE id=?",
            (body, 1 if new_attempt else 0, _now(), draft_id),
        )

    def add_feedback(self, draft_id: int, comment: str, edited: bool) -> None:
        with self.lock:
            row = self.conn.execute("SELECT feedback, attempts FROM drafts WHERE id=?", (draft_id,)).fetchone()
            fb = json.loads(row["feedback"])
            fb.append({"attempt": row["attempts"], "comment": comment, "edited": edited, "at": _now()})
            self.conn.execute("UPDATE drafts SET feedback=?, updated_at=? WHERE id=?",
                              (json.dumps(fb, ensure_ascii=False), _now(), draft_id))

    def set_status(self, draft_id: int, status: str, error: str | None = None) -> None:
        sent_at = _now() if status == "sent" else None
        self._exec("UPDATE drafts SET status=?, error=?, sent_at=COALESCE(?, sent_at), updated_at=? WHERE id=?",
                   (status, error, sent_at, _now(), draft_id))

    def count_by_status(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self._exec("SELECT status, COUNT(*) FROM drafts GROUP BY status")}

    # ---- usage / meta ----
    def add_usage(self, model: str, prompt: int, cached: int, completion: int) -> None:
        day = datetime.now().date().isoformat()
        self._exec(
            """INSERT INTO usage VALUES(?,?,1,?,?,?) ON CONFLICT(day, model) DO UPDATE SET
               calls=calls+1, prompt=prompt+excluded.prompt, cached=cached+excluded.cached,
               completion=completion+excluded.completion""",
            (day, model, prompt, cached, completion),
        )

    def usage_report(self, days: int = 7) -> list[dict]:
        rows = self._exec("SELECT * FROM usage ORDER BY day DESC, model LIMIT ?", (days * 4,))
        return [dict(r) for r in rows]

    def get_meta(self, key: str) -> str | None:
        row = self._exec("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._exec("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value))
