"""SQLite persistence; parameterized queries and short-lived connections."""
import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager
from fastapi import HTTPException


class Store:
    def __init__(self, directory):
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.path = str(Path(directory) / "jarvis.sqlite3")
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, expires REAL NOT NULL, token_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, owner TEXT NOT NULL, messages TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS confirmations (id TEXT PRIMARY KEY, owner TEXT NOT NULL, conversation TEXT NOT NULL, tool TEXT NOT NULL, arguments TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS timers (id TEXT PRIMARY KEY, owner TEXT NOT NULL, label TEXT NOT NULL, due REAL NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
            """)
            columns = {r["name"] for r in db.execute("PRAGMA table_info(conversations)")}
            if "private_tainted" not in columns:
                # Legacy history may already have trimmed away private results.
                db.execute("ALTER TABLE conversations ADD COLUMN private_tainted INTEGER NOT NULL DEFAULT 1")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def session(self, cookie, token):
        if not cookie or not token:
            return None
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=? AND expires>? AND token_hash=?", (hashlib.sha256(cookie.encode()).hexdigest(), time.time(), hashlib.sha256(token.encode()).hexdigest())).fetchone()
            return dict(row) if row else None

    def session_user(self, session_id):
        """Stable user id behind a login session: the access-token hash it authenticated with."""
        with self.connect() as db:
            row = db.execute("SELECT token_hash FROM sessions WHERE id=?", (session_id,)).fetchone()
            return row["token_hash"] if row else None

    def login(self, token):
        cookie = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("DELETE FROM conversations WHERE owner IN (SELECT id FROM sessions WHERE expires<?)", (time.time(),))
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            if db.execute("SELECT count(*) FROM sessions").fetchone()[0] >= 1000:
                raise HTTPException(429, "Session limit reached")
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(cookie.encode()).hexdigest(), time.time() + 30 * 86400, hashlib.sha256(token.encode()).hexdigest()))
        return cookie

    def conversation(self, owner, sid=None):
        with self.connect() as db:
            if sid:
                row = db.execute("SELECT messages FROM conversations WHERE id=? AND owner=?", (sid, owner)).fetchone()
                if row is None:
                    raise HTTPException(404, "Conversation not found")
                return sid, json.loads(row["messages"])
            if db.execute("SELECT count(*) FROM conversations WHERE owner=?", (owner,)).fetchone()[0] >= 100:
                raise HTTPException(429, "Conversation limit reached")
            sid = secrets.token_urlsafe(24)
            db.execute("INSERT INTO conversations (id,owner,messages,private_tainted) VALUES (?,?,?,0)", (sid, owner, "[]"))
            return sid, []

    def taint(self, sid):
        with self.connect() as db:
            db.execute("UPDATE conversations SET private_tainted=1 WHERE id=?", (sid,))

    def private_tainted(self, sid):
        with self.connect() as db:
            row = db.execute("SELECT private_tainted FROM conversations WHERE id=?", (sid,)).fetchone()
        return not row or bool(row["private_tainted"])

    def save(self, sid, messages):
        # Trim only at user boundaries to avoid orphan tool-call messages.
        while len(messages) > 40 or len(json.dumps(messages)) > 100000:
            next_user = next((i for i, m in enumerate(messages[1:], 1) if m["role"] == "user"), None)
            if next_user is None:
                break
            messages = messages[next_user:]
        with self.connect() as db:
            db.execute("UPDATE conversations SET messages=? WHERE id=?", (json.dumps(messages), sid))

    def pending(self, owner, sid):
        with self.connect() as db:
            row = db.execute("SELECT * FROM confirmations WHERE owner=? AND conversation=? AND used=0 AND expires>? ORDER BY expires DESC LIMIT 1", (owner, sid, time.time())).fetchone()
        return self.public_confirmation(row) if row else None

    @staticmethod
    def public_confirmation(row):
        arguments = json.loads(row["arguments"])
        digits = int(hashlib.sha256(row["id"].encode()).hexdigest(), 16) % 10000
        phrase = f"approve {digits:04d}"
        return {"id": row["id"], "tool": row["tool"], "arguments": arguments, "voice_phrase": phrase,
                "spoken_summary": f"Approval required. Action: {row['tool']}. Exact arguments: {json.dumps(arguments, ensure_ascii=False, sort_keys=True)}. Nothing has been executed. After this summary finishes, say {phrase}, or use Approve or Deny."}

    def propose(self, owner, sid, tool, arguments):
        ident = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("INSERT INTO confirmations VALUES (?,?,?,?,?,?,0)", (ident, owner, sid, tool, json.dumps(arguments), time.time() + 300))
        return self.public_confirmation({"id": ident, "tool": tool, "arguments": json.dumps(arguments)})

    def consume(self, owner, sid, ident, voice_phrase=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM confirmations WHERE id=? AND owner=? AND conversation=?", (ident, owner, sid)).fetchone()
            if not row:
                raise HTTPException(404, "Confirmation not found")
            if row["used"] or row["expires"] <= time.time():
                raise HTTPException(409, "Confirmation expired or already used")
            if voice_phrase is not None and voice_phrase != self.public_confirmation(row)["voice_phrase"]:
                raise HTTPException(422, "Voice approval phrase does not match this action")
            db.execute("UPDATE confirmations SET used=1 WHERE id=?", (ident,))
        return self.public_confirmation(row)

    def timers(self, owner):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM timers WHERE owner=? ORDER BY due DESC LIMIT 200", (owner,)).fetchall()
        return [{"id": r["id"], "label": r["label"], "due_at": datetime.fromtimestamp(r["due"], timezone.utc).isoformat(), "delivered": bool(r["delivered"]), "due": r["due"] <= time.time()} for r in rows]

    def set_timer(self, owner, args):
        seconds, label = args.get("seconds"), args.get("label", "Timer")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= 604800 or not isinstance(label, str) or not 1 <= len(label) <= 200:
            return {"error": "Timer requires 1–604800 seconds and a label of at most 200 characters"}
        ident = secrets.token_urlsafe(16)
        with self.connect() as db:
            if db.execute("SELECT count(*) FROM timers WHERE owner=?", (owner,)).fetchone()[0] >= 200:
                return {"error": "Timer limit reached"}
            db.execute("INSERT INTO timers VALUES (?,?,?,?,0)", (ident, owner, label, time.time() + seconds))
        return next(t for t in self.timers(owner) if t["id"] == ident)
