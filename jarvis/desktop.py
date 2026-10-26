"""Opt-in local desktop bridge. Never trust proxy headers for desktop access."""
from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse


def desktop_requested():
    return os.getenv("JARVIS_DESKTOP_ENABLED", "").lower() == "true"


def loopback(value):
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def local_scope(scope):
    """Validate transport peer AND authority AND optional exact browser origin."""
    peer = scope.get("client")
    headers = Headers(scope=scope)
    if not peer or not loopback(peer[0]):
        return False
    # Reject rather than reinterpret forwarded claims, even from a local tunnel.
    if any(key.lower() == "forwarded" or key.lower().startswith("x-forwarded-") for key in headers):
        return False
    hosts = headers.getlist("host")
    origins = headers.getlist("origin")
    if len(hosts) != 1 or len(origins) > 1 or headers.get("sec-fetch-site") == "cross-site":
        return False
    try:
        authority = urlsplit("http://" + hosts[0])
        if not authority.hostname or not loopback(authority.hostname) or authority.username or authority.password or authority.path or authority.query or authority.fragment:
            return False
        authority.port  # Validate port syntax, including bracketed IPv6.
        if origins:
            origin = urlsplit(origins[0])
            scheme = scope.get("scheme", "http").replace("ws", "http")
            if origin.scheme != scheme or origin.netloc.lower() != hosts[0].lower() or origin.path or origin.query or origin.fragment:
                return False
        return True
    except (ValueError, TypeError):
        return False


class LocalOnlyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from .pairing import discovery_scope
        discovery = os.getenv("JARVIS_DESKTOP_LOCAL_ONLY", "").lower() == "true" and discovery_scope(scope)
        if scope["type"] in ("http", "websocket") and not (discovery or local_scope(scope)):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": "Desktop server accepts direct loopback requests only"}, status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


import asyncio
from concurrent.futures import Future
from copy import deepcopy
import importlib
import json
import sqlite3
from pathlib import Path
import threading
import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import schedule as sched


class WakeControl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(strict=True)


class DesktopBridge:
    """One app, one durable queue, one desktop-affine worker thread."""
    SCHEDULE_TOOLS = frozenset({"desktop_schedule", "desktop_list_schedules", "desktop_cancel_schedule"})
    tool_names = frozenset({"desktop_capabilities", "desktop_action", "desktop_job_status", "desktop_cancel_job"}) | SCHEDULE_TOOLS
    MAX_WAIT = 2.0
    MAX_SCHEDULES = 50  # Active per owner.
    SCHEDULE_GRACE = 3600.0  # A missed fire older than this is skipped, never replayed.
    SCHEDULE_POLL = 1.0
    DONE = frozenset({"succeeded", "completed", "done", "failed", "cancelled", "unknown", "uncertain", "interrupted", "blocked"})

    def __init__(self):
        self.requested = desktop_requested()
        self.available = False
        self.ready = False
        self.engine = self.queue = None
        self.thread = None
        self._stop = threading.Event()
        self._signal = threading.Event()
        self._capabilities = {}
        self.schemas = []
        self.actions = {}
        self.wake = None
        self._wake_lock = threading.RLock()
        self._wake_owner = None
        self._wake_heartbeat = 0.0
        self._wake_watchdog = None
        self._schedule_task = None
        self._sched_lock = threading.RLock()
        self.now = time.time  # Injectable clock for schedules.
        self.detail = "Desktop control is disabled"
        self.workspace = Path(os.getenv("JARVIS_WORKSPACE_DIR", "workspace"))
        self.db_path = Path(os.getenv("JARVIS_DATA_DIR", "data")) / "desktop-jobs.sqlite3"
        if not self.requested or os.getenv("JARVIS_DESKTOP_LOCAL_ONLY", "").lower() != "true":
            return
        try:
            actions = importlib.import_module("companion.actions")
            queue = importlib.import_module("companion.queue")
            self.engine_factory = actions.ActionEngine
            self.queue_factory = queue.JobQueue
            self.risk_function = actions.action_risk
            self.actions = {s["function"]["name"]: deepcopy(s["function"]["parameters"]) for s in actions.ACTIONS}
            if not self.actions:
                raise ValueError("No supported actions")
            self.schemas = [
                {"type": "function", "function": {"name": "desktop_capabilities", "description": "Read capabilities of this installed machine; not the remote browser device.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
                {"type": "function", "function": {"name": "desktop_action", "description": "Run an allowlisted action on this installed machine. Workbook mutations require exact UI approval. Action argument schemas: " + json.dumps(self.actions, sort_keys=True), "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": sorted(self.actions)}, "arguments": {"type": "object"}}, "required": ["action", "arguments"], "additionalProperties": False}}},
            ]
            job_id = {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"], "additionalProperties": False}
            self.schemas += [
                {"type": "function", "function": {"name": "desktop_job_status", "description": "Read status and last result of one of your own long-running desktop jobs. Never resubmit a pending job.", "parameters": deepcopy(job_id)}},
                {"type": "function", "function": {"name": "desktop_cancel_job", "description": "Request cancellation of one of your own desktop jobs. Effects already completed are not undone.", "parameters": deepcopy(job_id)}},
                {"type": "function", "function": {"name": "desktop_schedule", "description": (
                    "Schedule one allowlisted desktop action to run later or repeatedly on this installed machine, unattended. "
                    "Always requires exact UI approval: approval authorizes every future run of exactly this action and arguments. "
                    "schedule is exactly one of {\"at\": ISO-8601 local datetime WITH offset, e.g. 2026-10-26T09:00:00+05:30} | "
                    "{\"cron\": 5-field 'minute hour day-of-month month day-of-week', day-of-week 0-6 with Sunday=0} | "
                    f"{{\"every_minutes\": {sched.MIN_EVERY}-{sched.MAX_EVERY}}}. timezone is an IANA name, default {sched.DEFAULT_TZ}. "
                    "Use desktop_action argument schemas for arguments."),
                    "parameters": {"type": "object", "properties": {
                        "action": {"type": "string", "enum": sorted(self.actions)}, "arguments": {"type": "object"},
                        "schedule": {"type": "object", "properties": {"at": {"type": "string"}, "cron": {"type": "string"},
                                     "every_minutes": {"type": "integer", "minimum": sched.MIN_EVERY, "maximum": sched.MAX_EVERY}},
                                     "minProperties": 1, "maxProperties": 1, "additionalProperties": False},
                        "timezone": {"type": "string"}},
                        "required": ["action", "arguments", "schedule"], "additionalProperties": False}}},
                {"type": "function", "function": {"name": "desktop_list_schedules", "description": "List your own scheduled desktop tasks with their next run time.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
                {"type": "function", "function": {"name": "desktop_cancel_schedule", "description": "Cancel one of your own scheduled desktop tasks so it never fires again. Jobs already started are not undone.", "parameters": {"type": "object", "properties": {"schedule_id": {"type": "string"}}, "required": ["schedule_id"], "additionalProperties": False}}},
            ]
            self.available = True
            self.detail = "Desktop worker not started"
        except Exception:
            self.detail = "Desktop components are unavailable"

    async def start(self):
        if not self.available or self.thread:
            return
        initialized = Future()
        self.thread = threading.Thread(target=self._worker, args=(initialized,), name="jarvis-desktop", daemon=True)
        self.thread.start()
        await asyncio.wrap_future(initialized)
        if self.ready:
            self._wake_watchdog = asyncio.create_task(self._watch_wake())
            self._schedule_task = asyncio.create_task(self._schedule_loop())

    async def _schedule_loop(self):
        while not self._stop.is_set():
            if self.ready:
                try:
                    await asyncio.to_thread(self.tick_schedules)
                except Exception:
                    pass  # State is durable; next tick retries. Never resubmits a fired slot.
            await asyncio.sleep(self.SCHEDULE_POLL)

    async def _watch_wake(self):
        while not self._stop.is_set():
            await asyncio.sleep(.25)
            with self._wake_lock:
                if self._wake_owner and time.monotonic() - self._wake_heartbeat > 15:
                    self.wake.stop()
                    self._wake_owner = None

    def _worker(self, initialized):
        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
            self.engine = self.engine_factory(workspace=self.workspace)
            self.queue = self.queue_factory(db_path=self.db_path, engine=self.engine)
            self._capabilities = self.engine.capabilities()
            self.ready = True
            self.detail = "Desktop worker ready"
        except Exception:
            self.detail = "Desktop worker initialization failed"
        finally:
            initialized.set_result(self.ready)
        try:
            while self.ready and not self._stop.is_set():
                # Queue owns durable state and recovery. Never resubmit on errors.
                try:
                    job = self.queue.run_next()
                except Exception:
                    self.ready = False
                    self.detail = "Desktop worker stopped after an uncertain queue error"
                    break
                if not job:
                    self._signal.wait(0.1)
                    self._signal.clear()
        finally:
            self.ready = False
            if self.engine and callable(getattr(self.engine, "close", None)):
                self.engine.close()

    async def close(self):
        with self._wake_lock:
            if self.wake:
                self.wake.stop()
            self._wake_owner = None
        self._stop.set()
        self._signal.set()
        stop = getattr(self.queue, "request_stop", None)
        if callable(stop):
            try:
                stop()  # Cooperative only; never kills processes.
            except Exception:
                pass
        for task in (self._wake_watchdog, self._schedule_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self.thread:
            await asyncio.to_thread(self.thread.join, 5)

    def status(self):
        with self._wake_lock:
            wake = self.wake_status()
            return {"enabled": self.ready, "wake_available": wake.get("available") is True,
                    "wake_enabled": wake.get("listening") is True, "wake": wake,
                    "capabilities": deepcopy(self._capabilities) if self.ready else {}, "detail": self.detail}

    def wake_status(self):
        if not self.ready:
            return {"available": False, "listening": False, "reason": "desktop_unavailable"}
        try:
            if self.wake is None:
                factory = importlib.import_module("companion.launcher").WakeController
                self.wake = factory(model_path=os.getenv("JARVIS_WAKE_MODEL_PATH"))
            return self.wake.status()
        except Exception:
            return {"available": False, "listening": False, "reason": "wake_unavailable"}

    def wake_control(self, owner, enabled):
        with self._wake_lock:
            if self._wake_owner not in (None, owner):
                raise HTTPException(409, "Wake listener belongs to another session")
            state = self.wake_status()
            if enabled:
                if not state.get("available"):
                    raise HTTPException(503, "Local wake is unavailable")
                try:
                    started = self.wake.start()
                except Exception:
                    started = False
                if not started:
                    self.wake.stop()
                    raise HTTPException(503, "Local wake could not start")
                self._wake_owner = owner
                self._wake_heartbeat = time.monotonic()
            elif self.wake:
                self.wake.stop()
                self._wake_owner = None
            return self.status()

    def pop_wake(self, owner):
        with self._wake_lock:
            if self._wake_owner != owner or self.wake is None:
                return {"event": None}
            self._wake_heartbeat = time.monotonic()
            return {"event": self.wake.pop_event()}

    def known(self, args):
        return isinstance(args, dict) and set(args) == {"action", "arguments"} and isinstance(args.get("action"), str) and args["action"] in self.actions and isinstance(args.get("arguments"), dict)

    def stable_owner(self, owner):
        """Login sessions rotate; the user does not. Map a session id to its access-token user.

        Sessions authenticate with one access token, so its hash identifies the person across
        logins. Falls back to the session id itself when no session record exists (tests, legacy)."""
        lookup = getattr(self, "user_lookup", None)
        try:
            user = lookup(owner) if callable(lookup) else None
        except Exception:
            user = None
        return ("user:" + user) if isinstance(user, str) and user else owner

    def bind_approval(self, owner, sid, approval_id, call_id):
        """Keep provider call identity durable without altering approved arguments."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS desktop_approvals (id TEXT PRIMARY KEY, owner TEXT NOT NULL, sid TEXT NOT NULL, call_id TEXT NOT NULL)")
            db.execute("INSERT INTO desktop_approvals VALUES (?,?,?,?)", (approval_id, owner, sid, call_id))

    def approval_call(self, owner, sid, approval_id):
        if not self.ready:
            return None
        try:
            with sqlite3.connect(self.db_path) as db:
                row = db.execute("SELECT call_id FROM desktop_approvals WHERE id=? AND owner=? AND sid=?", (approval_id, owner, sid)).fetchone()
                if row:
                    # Only /api/confirm with approve=True asks for this; record it so a schedule
                    # can prove a human approved this exact call (proposal alone never suffices).
                    db.execute("CREATE TABLE IF NOT EXISTS desktop_approved_calls (id TEXT PRIMARY KEY, owner TEXT NOT NULL, sid TEXT NOT NULL, call_id TEXT NOT NULL, approved REAL NOT NULL)")
                    db.execute("INSERT OR IGNORE INTO desktop_approved_calls VALUES (?,?,?,?,?)", (approval_id, owner, sid, row[0], time.time()))
                return row[0] if row else None
        except sqlite3.Error:
            return None  # Missing legacy identity must never execute a mutation.

    def needs_approval(self, name, args):
        if name == "desktop_capabilities":
            return False
        if name in {"desktop_list_schedules", "desktop_cancel_schedule"}:
            return False  # Own-owner read / disarm only; enforced at execute.
        if name == "desktop_schedule":
            # Always approval, even for low-risk actions: it authorizes unattended future runs.
            # Invalid requests go to execute for denial, never proposal.
            if not self.ready:
                return False
            try:
                self._validate_schedule_request(args)
                return True
            except sched.ScheduleError:
                return False
        if name in {"desktop_job_status", "desktop_cancel_job"}:
            return False  # Read-only status / cancelling own job; owner enforced at execute.
        if name != "desktop_action" or not self.ready or not self.known(args):
            return False  # Invalid calls go to execute for denial, never proposal.
        try:
            return self.risk_function(args["action"]) is not False
        except Exception:
            return True

    def get(self, ident, owner):
        if not self.queue:
            return None
        # Scheduled runs are owned by the stable user, interactive runs by the session.
        for candidate in dict.fromkeys((owner, self.stable_owner(owner))):
            try:
                job = self.queue.get(ident, candidate)
            except KeyError:
                continue
            if job:
                return {**job, "status": job.get("status", job.get("state"))}
        return None

    @staticmethod
    def _public_job(ident, job):
        state = job.get("status")
        result = job.get("result") if isinstance(job.get("result"), dict) else None
        if state not in DesktopBridge.DONE:
            return {"ok": True, "job_id": ident, "status": state, "pending": True, "verified": False,
                    "cancel_requested": bool(job.get("cancel_requested")), "result": result,
                    "message": "Job outcome is pending. Poll the job; do not resubmit."}
        ok = state in {"succeeded", "completed", "done"} and bool(result and result.get("ok") is True)
        out = {**(result or {}), "job_id": ident, "status": state, "ok": ok, "verified": ok}
        if not result:
            out["error"] = "Desktop job has no verified result; do not retry"
        elif not ok:
            out["result"] = result
        return out

    async def _control(self, owner, name, args):
        if not isinstance(args, dict) or set(args) != {"job_id"} or not isinstance(args["job_id"], str) or not args["job_id"].strip():
            return {"ok": False, "error": "Unknown desktop action or invalid arguments"}
        ident = args["job_id"]
        try:
            if name == "desktop_job_status":
                job = await asyncio.to_thread(self.get, ident, owner)
            else:
                cancel = getattr(self.queue, "cancel", None)
                before = await asyncio.to_thread(self.get, ident, owner)
                if before and before.get("status") not in self.DONE and callable(cancel):
                    await asyncio.to_thread(cancel, ident, before.get("owner", owner))
                    self._signal.set()
                    job = await asyncio.to_thread(self.get, ident, owner)
                    return {"ok": True, "job_id": ident, "status": job.get("status"), "cancel_requested": True,
                            "pending": job.get("status") not in self.DONE,
                            "message": "Cancellation requested; completed effects are not undone."}
                job = before
                if job:
                    return {"ok": False, "job_id": ident, "status": job.get("status"), "error": "Job already finished; nothing to cancel"}
        except Exception:
            return {"ok": False, "job_id": ident, "error": "Desktop job state unavailable"}
        if not job:
            return {"ok": False, "error": "Desktop job not found"}
        return self._public_job(ident, job)

    def cancel(self, ident, owner):
        if not self.queue or not callable(getattr(self.queue, "cancel", None)):
            return None
        job = self.get(ident, owner)
        if not job:
            return None
        try:
            self.queue.cancel(ident, job.get("owner", owner))
        except KeyError:
            return None
        self._signal.set()
        return self.get(ident, owner)

    # ---------------- schedules ----------------

    _TYPES = {"string": str, "object": dict, "array": list, "boolean": bool, "null": type(None)}

    @classmethod
    def _value_ok(cls, value, schema):
        if not isinstance(schema, dict):
            return False
        kinds = schema.get("type")
        if kinds is not None:
            kinds = kinds if isinstance(kinds, list) else [kinds]
            def match(kind):
                if kind == "integer":
                    return type(value) is int
                if kind == "number":
                    return type(value) in (int, float)
                return kind in cls._TYPES and type(value) is cls._TYPES[kind]
            if not any(match(k) for k in kinds):
                return False
        if "enum" in schema and value not in schema["enum"]:
            return False
        if isinstance(value, str) and not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 10000):
            return False
        if type(value) in (int, float) and not (schema.get("minimum", value) <= value <= schema.get("maximum", value)):
            return False
        if isinstance(value, list):
            if len(value) > schema.get("maxItems", 1000):
                return False
            if "items" in schema and not all(cls._value_ok(v, schema["items"]) for v in value):
                return False
        if isinstance(value, dict):
            return cls._object_ok(value, schema)
        return True

    @classmethod
    def _object_ok(cls, value, schema):
        props = schema.get("properties", {})
        if not set(schema.get("required", [])) <= value.keys():
            return False
        if schema.get("additionalProperties") is False and value.keys() - props.keys():
            return False
        return all(cls._value_ok(v, props[k]) for k, v in value.items() if k in props)

    def _validate_schedule_request(self, args):
        """Return (action, arguments, spec, tz, first_ts) or raise ScheduleError."""
        if not isinstance(args, dict) or not {"action", "arguments", "schedule"} <= args.keys() or args.keys() - {"action", "arguments", "schedule", "timezone"}:
            raise sched.ScheduleError("Expected action, arguments, schedule and optional timezone")
        if not self.known({"action": args["action"], "arguments": args["arguments"]}):
            raise sched.ScheduleError("Unknown desktop action or invalid arguments")
        schema = self.actions[args["action"]]
        if not self._object_ok(args["arguments"], schema):
            raise sched.ScheduleError("Arguments do not match the action schema")
        try:
            encoded = json.dumps(args["arguments"], sort_keys=True, allow_nan=False)
        except (TypeError, ValueError):
            raise sched.ScheduleError("Arguments are not plain JSON") from None
        if len(encoded.encode()) > 16000:
            raise sched.ScheduleError("Arguments are too large")
        spec, tz, first = sched.validate_schedule(args["schedule"], args.get("timezone"), self.now())
        return args["action"], args["arguments"], spec, tz, first

    def _sdb(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS schedules (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, idem TEXT NOT NULL, action TEXT NOT NULL, args TEXT NOT NULL,
            spec TEXT NOT NULL, tz TEXT NOT NULL, anchor REAL NOT NULL, next_run REAL, active INTEGER NOT NULL,
            created REAL NOT NULL, last_job_id TEXT, last_fired REAL, runs INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0, last_skipped REAL, UNIQUE(owner, idem))""")
        db.execute("CREATE INDEX IF NOT EXISTS schedules_due ON schedules(active, next_run)")
        return db

    @staticmethod
    def _public_schedule(row):
        tz, nxt = row["tz"], row["next_run"] if row["active"] else None
        return {"id": row["id"], "action": row["action"], "arguments": json.loads(row["args"]),
                "schedule": json.loads(row["spec"]), "timezone": tz, "active": bool(row["active"]),
                "next_run": sched.local_iso(nxt, tz), "next_run_ts": nxt, "next_run_text": sched.spoken(nxt, tz),
                "created": row["created"], "last_job_id": row["last_job_id"],
                "last_fired": sched.local_iso(row["last_fired"], tz), "runs": row["runs"], "skipped": row["skipped"]}

    def _create_schedule_row(self, owner, action, arguments, spec, tz, first, idem):
        """Insert (or return existing for the same approval) schedule id. Raises ScheduleError when over quota."""
        import uuid
        now = self.now()
        anchor = first - spec["every_minutes"] * 60 if "every_minutes" in spec else now
        with self._sched_lock:
            db = self._sdb()
            try:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    old = db.execute("SELECT id FROM schedules WHERE owner=? AND idem=?", (owner, idem)).fetchone()
                    if old:
                        return old["id"]
                    if db.execute("SELECT COUNT(*) FROM schedules WHERE owner=? AND active=1", (owner,)).fetchone()[0] >= self.MAX_SCHEDULES:
                        raise sched.ScheduleError(f"You already have {self.MAX_SCHEDULES} active schedules; cancel one first")
                    ident = str(uuid.uuid4())
                    db.execute("INSERT INTO schedules (id, owner, idem, action, args, spec, tz, anchor, next_run, active, created) VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                               (ident, owner, idem, action, json.dumps(arguments), json.dumps(spec), tz, anchor, first, now))
                    return ident
            finally:
                db.close()

    def _schedule_row(self, ident, owner):
        db = self._sdb()
        try:
            return db.execute("SELECT * FROM schedules WHERE id=? AND owner=?", (ident, owner)).fetchone()
        finally:
            db.close()

    def list_schedules(self, owner):
        db = self._sdb()
        try:
            rows = db.execute("SELECT * FROM schedules WHERE owner=? ORDER BY created, rowid", (owner,)).fetchall()
        finally:
            db.close()
        return [self._public_schedule(r) for r in rows]

    def cancel_schedule(self, ident, owner):
        """Deactivate own schedule; None when absent or owned by someone else."""
        if not isinstance(ident, str):
            return None
        with self._sched_lock:
            db = self._sdb()
            try:
                with db:
                    db.execute("UPDATE schedules SET active=0 WHERE id=? AND owner=?", (ident, owner))
                    row = db.execute("SELECT * FROM schedules WHERE id=? AND owner=?", (ident, owner)).fetchone()
            finally:
                db.close()
        return self._public_schedule(row) if row else None

    def tick_schedules(self):
        """Submit due schedules exactly once per slot. Returns fired job ids. Safe to call concurrently."""
        if not self.ready or self.queue is None:
            return []
        fired = []
        with self._sched_lock:
            now = self.now()
            db = self._sdb()
            try:
                due = db.execute("SELECT * FROM schedules WHERE active=1 AND next_run IS NOT NULL AND next_run<=? ORDER BY next_run, rowid LIMIT 50", (now,)).fetchall()
                for row in due:
                    if not self.ready:
                        break  # Never fire while the bridge is not ready.
                    slot, spec = row["next_run"], json.loads(row["spec"])
                    try:
                        following = sched.next_run(spec, row["tz"], max(now, slot), anchor=row["anchor"])
                    except Exception:
                        following = None
                    if now - slot > self.SCHEDULE_GRACE:
                        with db:
                            db.execute("UPDATE schedules SET next_run=?, active=?, skipped=skipped+1, last_skipped=? WHERE id=? AND active=1 AND next_run=?",
                                       (following, int(following is not None), slot, row["id"], slot))
                        continue
                    if row["action"] not in self.actions:
                        with db:
                            db.execute("UPDATE schedules SET active=0 WHERE id=?", (row["id"],))
                        continue
                    # Deterministic key: a crash between submit and update re-submits the same slot,
                    # which the durable queue de-duplicates. Restarts never double-fire.
                    key = f"schedule:{row['id']}:{int(slot)}"
                    try:
                        job = self.queue.submit(row["action"], json.loads(row["args"]), owner=row["owner"], idempotency_key=key, depends_on=None)
                    except Exception:
                        continue  # Retried next tick until grace expires; slot is never advanced unfired.
                    with db:
                        db.execute("UPDATE schedules SET next_run=?, active=?, last_job_id=?, last_fired=?, runs=runs+1 WHERE id=? AND next_run=?",
                                   (following, int(following is not None) if row["active"] else 0, job["id"], slot, row["id"], slot))
                    fired.append(job["id"])
            finally:
                db.close()
        if fired:
            self._signal.set()
        return fired

    def _approved_call(self, owner, sid, call_id):
        try:
            with sqlite3.connect(self.db_path) as db:
                return db.execute("SELECT 1 FROM desktop_approved_calls WHERE owner=? AND sid=? AND call_id=?", (owner, sid, call_id)).fetchone() is not None
        except sqlite3.Error:
            return False

    async def _schedule_tool(self, owner, name, args, sid, call_id):
        # Approval stays bound to the login session; schedules belong to the stable user so a
        # later login can still list and cancel them (they keep firing across logins).
        session_owner, owner = owner, self.stable_owner(owner)
        if name == "desktop_list_schedules":
            if args != {}:
                return {"ok": False, "error": "desktop_list_schedules takes no arguments"}
            items = await asyncio.to_thread(self.list_schedules, owner)
            return {"ok": True, "schedules": items, "active": sum(i["active"] for i in items)}
        if name == "desktop_cancel_schedule":
            if not isinstance(args, dict) or set(args) != {"schedule_id"} or not isinstance(args["schedule_id"], str) or not args["schedule_id"].strip():
                return {"ok": False, "error": "Invalid schedule id"}
            before = await asyncio.to_thread(self._schedule_row, args["schedule_id"], owner)
            if not before:
                return {"ok": False, "error": "Schedule not found"}
            if not before["active"]:
                return {"ok": False, "schedule_id": before["id"], "error": "Schedule is already inactive"}
            row = await asyncio.to_thread(self.cancel_schedule, args["schedule_id"], owner)
            return {"ok": True, "schedule_id": row["id"], "active": False,
                    "message": "Schedule cancelled; it will not run again. Jobs already started are not undone."}
        # desktop_schedule: execution only after exact UI approval bound to this owner/session/call.
        try:
            action, arguments, spec, tz, first = self._validate_schedule_request(args)
        except sched.ScheduleError as error:
            return {"ok": False, "error": str(error)}
        if not sid or not call_id or not await asyncio.to_thread(self._approved_call, session_owner, sid, call_id):
            return {"ok": False, "error": "Scheduling requires exact UI approval"}
        idem = json.dumps([sid, call_id], separators=(",", ":"))
        try:
            ident = await asyncio.to_thread(self._create_schedule_row, owner, action, arguments, spec, tz, first, idem)
        except sched.ScheduleError as error:
            return {"ok": False, "error": str(error)}
        except Exception:
            return {"ok": False, "error": "Schedule was not saved; do not automatically retry"}
        row = self._public_schedule(await asyncio.to_thread(self._schedule_row, ident, owner))
        return {"ok": True, "schedule_id": ident, "action": action, "schedule": row["schedule"], "timezone": tz,
                "active": row["active"], "next_run": row["next_run"], "next_run_text": row["next_run_text"],
                "message": f"Scheduled. Next run {row['next_run_text']}."}

    async def execute(self, owner, name, args, sid, call_id, *, timeout=90):
        if not self.ready:
            return {"ok": False, "error": "Desktop control is unavailable"}
        if name in {"desktop_job_status", "desktop_cancel_job"}:
            return await self._control(owner, name, args)
        if name in self.SCHEDULE_TOOLS:
            return await self._schedule_tool(owner, name, args, sid, call_id)
        timeout = max(0.0, min(float(timeout), self.MAX_WAIT))
        if name == "desktop_capabilities" and args == {}:
            return {"ok": True, **deepcopy(self._capabilities)}
        if name != "desktop_action" or not self.known(args) or not sid or not call_id:
            return {"ok": False, "error": "Unknown desktop action or invalid arguments"}
        # Deterministic across retries; scope and arguments are checked by queue.
        key = json.dumps([sid, call_id], separators=(",", ":"))
        try:
            job = self.queue.submit(args["action"], args["arguments"], owner=owner, idempotency_key=key, depends_on=None)
        except Exception:
            return {"ok": False, "error": "Desktop job was not accepted; do not automatically retry"}
        ident = job["id"]
        self._signal.set()
        deadline = time.monotonic() + timeout
        while True:
            job = self.get(ident, owner)
            if not job:
                return {"ok": False, "job_id": ident, "error": "Desktop job state unavailable; do not retry"}
            state = job.get("status")
            if state in self.DONE:
                return self._public_job(ident, job)
            if time.monotonic() >= deadline or not self.ready:
                return {"ok": True, "job_id": ident, "status": state, "pending": True, "verified": False, "message": "Job outcome is pending. Poll the job; do not resubmit."}
            await asyncio.sleep(0.03)


def register_desktop_routes(app, bridge, authenticated):
    if not bridge.requested:
        return
    app.router.add_event_handler("startup", bridge.start)
    app.router.add_event_handler("shutdown", bridge.close)

    @app.get("/api/desktop/status")
    def desktop_status(session=Depends(authenticated)):
        return bridge.status()

    @app.get("/api/desktop/wake")
    def desktop_wake(session=Depends(authenticated)):
        return bridge.pop_wake(session["id"])

    @app.post("/api/desktop/wake-control")
    def desktop_wake_control(body: WakeControl, session=Depends(authenticated)):
        return bridge.wake_control(session["id"], body.enabled)

    @app.get("/api/desktop/jobs/{ident}")
    def desktop_job(ident: str, session=Depends(authenticated)):
        job = bridge.get(ident, session["id"])
        if not job:
            raise HTTPException(404, "Desktop job not found")
        return job


    @app.get("/api/desktop/schedules")
    def desktop_schedules(session=Depends(authenticated)):
        if not bridge.available:
            raise HTTPException(503, "Desktop control is unavailable")
        return {"schedules": bridge.list_schedules(bridge.stable_owner(session["id"]))}

    @app.post("/api/desktop/schedules/{ident}/cancel")
    def desktop_schedule_cancel(ident: str, session=Depends(authenticated)):
        row = bridge.cancel_schedule(ident, bridge.stable_owner(session["id"])) if bridge.available else None
        if not row:
            raise HTTPException(404, "Schedule not found")
        return row

    @app.post("/api/desktop/jobs/{ident}/cancel")
    def desktop_job_cancel(ident: str, session=Depends(authenticated)):
        job = bridge.cancel(ident, session["id"])
        if not job:
            raise HTTPException(404, "Desktop job not found")
        return job
