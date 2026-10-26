"""Owner-scoped long-task status/cancel and cooperative cancellation. Never touches a real desktop/UFO."""
import asyncio
import sys
import threading
import time
import types

import pytest
from fastapi.testclient import TestClient

TOKEN = "desktop-test-token-not-a-real-secret"
ARGS = {"action": "ufo_task", "arguments": {"value": "x"}}


def make_app(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("JARVIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JARVIS_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("JARVIS_DESKTOP_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DESKTOP_LOCAL_ONLY", "true")
    monkeypatch.setenv("JARVIS_LLM_BASE_URL", "https://llm.invalid/v1")
    monkeypatch.setenv("JARVIS_LLM_MODEL", "test")
    from jarvis.main import create_app
    return create_app()


class CancellableEngine:
    """Blocks in execute_cancellable until cancel_check() is true or release is set."""
    release = None
    started = None

    def __init__(self, workspace):
        self.calls, self.plain_calls, self.closed = [], [], False

    def capabilities(self):
        return {"scope": "test_machine"}

    def execute(self, name, args):
        self.plain_calls.append((name, args))
        return {"ok": True, "plain": True}

    def execute_cancellable(self, name, args, cancel_check):
        self.calls.append((name, args))
        type(self).started.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not type(self).release.is_set():
            if cancel_check():
                return {"ok": False, "error": "cancelled", "token": "secret-value"}
            time.sleep(.01)
        return {"ok": True, "finished": True, "password": "hunter2"}

    def close(self):
        self.closed = True


@pytest.fixture
def backend(monkeypatch):
    CancellableEngine.release = threading.Event()
    CancellableEngine.started = threading.Event()
    schemas = [{"type": "function", "function": {"name": "ufo_task", "description": "t",
        "parameters": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}}}]
    monkeypatch.setitem(sys.modules, "companion.actions", types.SimpleNamespace(
        ACTIONS=schemas, ActionEngine=CancellableEngine, action_risk=lambda name: False))
    sys.modules.pop("companion.queue", None)
    yield CancellableEngine
    CancellableEngine.release.set()


def run(coro):
    return asyncio.run(coro)


# ---- queue ----

def test_queue_passes_live_cancel_check_to_cancellable_engine(tmp_path, backend):
    from companion.queue import JobQueue
    engine = CancellableEngine(None)
    queue = JobQueue(tmp_path / "q.sqlite3", engine)
    job = queue.submit("ufo_task", {"value": "x"}, "alice", "k1")
    done = []
    thread = threading.Thread(target=lambda: done.append(queue.run_next()))
    thread.start()
    assert backend.started.wait(2)
    assert queue.cancel(job["id"], "alice")["cancel_requested"] is True
    thread.join(3)
    final = queue.get(job["id"], "alice")
    assert final["state"] == "cancelled"
    assert final["result"]["ok"] is False
    assert final["result"]["token"] == "[REDACTED]"
    assert engine.plain_calls == [] and engine.calls == [("ufo_task", {"value": "x"})]


def test_cancel_after_success_keeps_success_honest(tmp_path, backend):
    from companion.queue import JobQueue
    engine = CancellableEngine(None)
    backend.release.set()
    queue = JobQueue(tmp_path / "q.sqlite3", engine)
    job = queue.submit("ufo_task", {"value": "x"}, "alice", "k1")
    queue.run_next()
    assert queue.cancel(job["id"], "alice")["state"] == "succeeded"


def test_queue_falls_back_to_plain_execute(tmp_path):
    from companion.queue import JobQueue

    class Plain:
        def execute(self, name, args):
            return {"ok": True}
    queue = JobQueue(tmp_path / "q.sqlite3", Plain())
    job = queue.submit("a", {}, "alice", "k")
    assert queue.run_next()["state"] == "succeeded"
    assert queue.get(job["id"], "alice")["state"] == "succeeded"


def test_queue_stop_requests_running_cancel(tmp_path, backend):
    from companion.queue import JobQueue
    queue = JobQueue(tmp_path / "q.sqlite3", CancellableEngine(None))
    job = queue.submit("ufo_task", {"value": "x"}, "alice", "k1")
    thread = threading.Thread(target=queue.run_next)
    thread.start()
    assert backend.started.wait(2)
    queue.request_stop()
    thread.join(3)
    assert not thread.is_alive()
    assert queue.get(job["id"], "alice")["state"] == "cancelled"


# ---- bridge ----

def test_schemas_and_names(monkeypatch, tmp_path, backend):
    bridge = make_app(monkeypatch, tmp_path).state.desktop
    assert {"desktop_job_status", "desktop_cancel_job"} <= bridge.tool_names
    schemas = {s["function"]["name"]: s["function"]["parameters"] for s in bridge.schemas}
    for name in ("desktop_job_status", "desktop_cancel_job"):
        assert schemas[name]["required"] == ["job_id"]
        assert schemas[name]["additionalProperties"] is False


def test_pending_within_two_seconds_status_cancel_and_cross_owner(monkeypatch, tmp_path, backend):
    bridge = make_app(monkeypatch, tmp_path).state.desktop

    async def check():
        await bridge.start()
        try:
            started = time.monotonic()
            first = await bridge.execute("alice", "desktop_action", ARGS, "sid", "call", timeout=90)
            assert time.monotonic() - started < 3
            assert first["pending"] is True
            jid = first["job_id"]
            assert backend.started.wait(2)
            again = await bridge.execute("alice", "desktop_action", ARGS, "sid", "call", timeout=0)
            assert again["job_id"] == jid
            assert len(bridge.engine.calls) == 1  # duplicate never re-executes

            status = await bridge.execute("alice", "desktop_job_status", {"job_id": jid}, "sid", "c2")
            assert status["pending"] is True and status["status"] == "running" and "owner" not in status
            for tool in ("desktop_job_status", "desktop_cancel_job"):
                denied = await bridge.execute("bob", tool, {"job_id": jid}, "sid2", "c3")
                assert denied["ok"] is False and "pending" not in denied
            assert bridge.get(jid, "alice")["cancel_requested"] is False

            # Control is not blocked by the busy serial worker.
            t0 = time.monotonic()
            cancel = await bridge.execute("alice", "desktop_cancel_job", {"job_id": jid}, "sid", "c4")
            assert time.monotonic() - t0 < 1
            assert cancel["ok"] is True and cancel["cancel_requested"] is True
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                final = await bridge.execute("alice", "desktop_job_status", {"job_id": jid}, "sid", "c5")
                if not final.get("pending"):
                    break
                await asyncio.sleep(.05)
            assert final["status"] == "cancelled" and final["ok"] is False and final["verified"] is False
            assert final["result"]["token"] == "[REDACTED]"
            assert len(bridge.engine.calls) == 1
            gone = await bridge.execute("alice", "desktop_cancel_job", {"job_id": jid}, "sid", "c6")
            assert gone["ok"] is False
        finally:
            await bridge.close()
    run(check())


@pytest.mark.parametrize("args", [{}, {"job_id": ""}, {"job_id": 5}, {"job_id": "a", "x": 1}, {"job_id": "nope"}])
def test_invalid_control_args_fail_closed(monkeypatch, tmp_path, backend, args):
    bridge = make_app(monkeypatch, tmp_path).state.desktop

    async def check():
        await bridge.start()
        try:
            for tool in ("desktop_job_status", "desktop_cancel_job"):
                result = await bridge.execute("alice", tool, args, "sid", "c")
                assert result["ok"] is False
        finally:
            await bridge.close()
    run(check())


def test_needs_approval_false_for_status_and_cancel(monkeypatch, tmp_path, backend):
    bridge = make_app(monkeypatch, tmp_path).state.desktop

    async def check():
        await bridge.start()
        try:
            for tool in ("desktop_job_status", "desktop_cancel_job"):
                assert bridge.needs_approval(tool, {"job_id": "x"}) is False
        finally:
            await bridge.close()
    run(check())


def test_close_requests_running_work_stop_before_join(monkeypatch, tmp_path, backend):
    app = make_app(monkeypatch, tmp_path)
    bridge = app.state.desktop

    async def check():
        await bridge.start()
        first = await bridge.execute("alice", "desktop_action", ARGS, "sid", "call", timeout=0)
        assert backend.started.wait(2)
        t0 = time.monotonic()
        await bridge.close()
        assert time.monotonic() - t0 < 4
        assert not bridge.thread.is_alive()
        assert bridge.engine.closed
        return first
    run(check())


def test_cancel_route_authenticated_and_owner_scoped(monkeypatch, tmp_path, backend):
    app = make_app(monkeypatch, tmp_path)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 40000)) as client:
        assert client.post("/api/desktop/jobs/x/cancel").status_code == 401
        assert client.post("/api/login", json={"token": TOKEN}).status_code == 200
        assert client.post("/api/desktop/jobs/missing/cancel").status_code == 404
        bridge = app.state.desktop
        job = bridge.queue.submit("ufo_task", {"value": "x"}, owner="someone-else", idempotency_key="k")
        assert client.post(f"/api/desktop/jobs/{job['id']}/cancel").status_code == 404
        assert bridge.get(job["id"], "someone-else")["cancel_requested"] is False
        backend.release.set()
