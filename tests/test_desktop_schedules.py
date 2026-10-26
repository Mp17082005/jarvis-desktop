"""Durable owner-scoped scheduled desktop tasks. Never touches a real desktop/UFO; injected clock."""
import asyncio
import json
import sqlite3
import sys
import threading
import types
from datetime import datetime, timedelta, timezone
import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.schedule import ScheduleError, iana_available, next_run, parse_cron, validate_schedule, zone

TOKEN = "desktop-test-token-not-a-real-secret"
IST = zone("Asia/Kolkata")[1]  # Fixed +05:30 fallback when IANA data is absent.
NY = zone("America/New_York")[1] if iana_available() else None
needs_iana = pytest.mark.skipif(not iana_available(), reason="IANA tz database unavailable (no tzdata)")


def local(tz, *parts, fold=0):
    return datetime(*parts, tzinfo=tz, fold=fold)


# ---------------- cron parser ----------------

def test_cron_weekday_nine_am_skips_weekend():
    cron = parse_cron("0 9 * * 1-5")
    friday = local(IST, 2026, 10, 30, 9, 0)  # Friday
    assert cron.next_after(friday) == local(IST, 2026, 11, 2, 9, 0)  # strictly after -> Monday
    assert cron.next_after(local(IST, 2026, 10, 26, 8, 59)) == local(IST, 2026, 10, 26, 9, 0)


def test_cron_steps_ranges_and_lists():
    cron = parse_cron("*/15 9-17/4 * * *")
    assert cron.next_after(local(IST, 2026, 10, 26, 9, 50)) == local(IST, 2026, 10, 26, 13, 0)
    assert cron.next_after(local(IST, 2026, 10, 26, 17, 45)) == local(IST, 2026, 10, 27, 9, 0)
    lists = parse_cron("5,35 * * * *")
    assert lists.next_after(local(IST, 2026, 10, 26, 10, 5)) == local(IST, 2026, 10, 26, 10, 35)
    offset_step = parse_cron("10/20 * * * *")  # 10,30,50
    assert offset_step.next_after(local(IST, 2026, 10, 26, 10, 31)) == local(IST, 2026, 10, 26, 10, 50)
    names = parse_cron("0 8 * jan mon-fri")
    assert names.next_after(local(IST, 2026, 10, 26, 0, 0)) == local(IST, 2027, 1, 1, 8, 0)


def test_cron_dom_dow_or_semantics_and_sunday_seven():
    both = parse_cron("0 0 13 * 5")  # 13th OR any Friday
    assert both.next_after(local(IST, 2026, 11, 7, 0, 0)) == local(IST, 2026, 11, 13, 0, 0)  # Friday the 13th
    assert both.next_after(local(IST, 2026, 11, 13, 0, 0)) == local(IST, 2026, 11, 20, 0, 0)
    assert both.next_after(local(IST, 2026, 12, 5, 0, 0)) == local(IST, 2026, 12, 11, 0, 0)
    assert both.next_after(local(IST, 2026, 12, 11, 0, 0)) == local(IST, 2026, 12, 13, 0, 0)  # Sunday 13th by dom
    dom_only = parse_cron("0 0 13 * *")  # star dow -> AND (dow unrestricted)
    assert dom_only.next_after(local(IST, 2026, 11, 14, 0, 0)) == local(IST, 2026, 12, 13, 0, 0)
    assert parse_cron("0 7 * * 7").next_after(local(IST, 2026, 10, 26, 0, 0)) == local(IST, 2026, 11, 1, 7, 0)
    assert parse_cron("0 7 * * 0").next_after(local(IST, 2026, 10, 26, 0, 0)) == local(IST, 2026, 11, 1, 7, 0)


@needs_iana
def test_cron_dst_safe_spring_forward_and_fall_back():
    # 2026-03-08 02:30 does not exist in New York -> skipped, next day instead.
    cron = parse_cron("30 2 * * *")
    got = cron.next_after(local(NY, 2026, 3, 7, 3, 0))
    assert got == local(NY, 2026, 3, 9, 2, 30)
    # 2026-11-01 01:30 happens twice; it fires once.
    back = parse_cron("30 1 * * *")
    first = back.next_after(local(NY, 2026, 10, 31, 12, 0))
    assert first.replace(tzinfo=None) == datetime(2026, 11, 1, 1, 30)
    assert back.next_after(first).replace(tzinfo=None) == datetime(2026, 11, 2, 1, 30)
    assert back.next_after(first + timedelta(minutes=30)).replace(tzinfo=None) == datetime(2026, 11, 2, 1, 30)


@pytest.mark.parametrize("expr", ["60 * * * *", "* * *", "* * * * * *", "*/0 * * * *", "5-1 * * * *", "* * 32 * *",
                                  "* 24 * * *", "* * * 13 *", "* * * * 8", "a b c d e", "", "1,,2 * * * *", "-1 * * * *", 5])
def test_cron_rejects_invalid(expr):
    with pytest.raises(ScheduleError):
        parse_cron(expr)


def test_cron_requires_aware_datetime():
    with pytest.raises(ScheduleError):
        parse_cron("* * * * *").next_after(datetime(2026, 1, 1))


# ---------------- schedule spec validation ----------------

NOW = local(IST, 2026, 10, 26, 8, 0).timestamp()  # Monday 08:00 IST


def test_validate_at_every_cron():
    spec, tz, first = validate_schedule({"at": "2026-10-26T08:20:00+05:30"}, "Asia/Kolkata", NOW)
    assert spec == {"at": "2026-10-26T08:20:00+05:30"} and first == NOW + 1200
    spec, tz, first = validate_schedule({"every_minutes": 30}, None, NOW)
    assert tz == "Asia/Kolkata" and first == NOW + 1800
    spec, tz, first = validate_schedule({"cron": "0 9 * * 1-5"}, "Asia/Kolkata", NOW)
    assert first == local(IST, 2026, 10, 26, 9, 0).timestamp()
    if NY:
        _, _, ny = validate_schedule({"cron": "0 9 * * *"}, "America/New_York", NOW)
        assert datetime.fromtimestamp(ny, NY).hour == 9


def test_fixed_offset_fallback_for_kolkata_and_utc():
    from jarvis import schedule
    real = schedule.ZoneInfo

    def missing(name):
        raise KeyError(name)
    schedule.ZoneInfo = missing
    try:
        assert zone("Asia/Kolkata")[1].utcoffset(None) == timedelta(hours=5, minutes=30)
        assert zone("UTC")[1].utcoffset(None) == timedelta(0)
        with pytest.raises(ScheduleError):
            zone("America/New_York")  # DST zone never guessed as a fixed offset
    finally:
        schedule.ZoneInfo = real


@pytest.mark.parametrize("schedule,tz", [
    ({"at": "2026-10-26T08:20:00"}, None),  # no offset
    ({"at": "2026-10-26T07:59:00+05:30"}, None),  # past
    ({"at": "2028-10-26T07:59:00+05:30"}, None),  # > 366 days
    ({"at": "tomorrow"}, None),
    ({"every_minutes": 4}, None), ({"every_minutes": 10081}, None), ({"every_minutes": True}, None), ({"every_minutes": "30"}, None),
    ({"cron": "0 0 30 2 *"}, None),  # never fires
    ({"cron": "bad"}, None),
    ({}, None), ({"at": "2026-10-26T08:20:00+05:30", "every_minutes": 30}, None), ({"weekly": 1}, None), ("0 9 * * *", None),
    ({"every_minutes": 30}, "Mars/Olympus"), ({"every_minutes": 30}, 5), ({"every_minutes": 30}, "../etc"),
])
def test_validate_rejects(schedule, tz):
    with pytest.raises(ScheduleError):
        validate_schedule(schedule, tz, NOW)


def test_next_run_every_is_anchored_and_at_is_one_shot():
    assert next_run({"every_minutes": 10}, "Asia/Kolkata", NOW + 1, anchor=NOW) == NOW + 600
    assert next_run({"every_minutes": 10}, "Asia/Kolkata", NOW + 1300, anchor=NOW) == NOW + 1800
    assert next_run({"at": "2026-10-26T08:20:00+05:30"}, "Asia/Kolkata", NOW + 1200, anchor=NOW + 1200) is None


# ---------------- bridge ----------------

class Engine:
    def __init__(self, workspace):
        self.calls = []

    def capabilities(self):
        return {"scope": "test_machine"}

    def execute(self, name, args):
        self.calls.append((name, args))
        return {"ok": True}


SCHEMAS = [
    {"type": "function", "function": {"name": "ufo_task", "description": "t", "parameters": {"type": "object",
        "properties": {"request": {"type": "string"}}, "required": ["request"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "pause", "description": "t", "parameters": {"type": "object",
        "properties": {}, "required": [], "additionalProperties": False}}},
]


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setitem(sys.modules, "companion.actions", types.SimpleNamespace(
        ACTIONS=SCHEMAS, ActionEngine=Engine, action_risk=lambda name: name == "ufo_task"))
    sys.modules.pop("companion.queue", None)


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


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


SCHED = {"action": "ufo_task", "arguments": {"request": "open my sales sheet and add today's row"},
         "schedule": {"cron": "0 9 * * 1-5"}, "timezone": "Asia/Kolkata"}
NINE = local(IST, 2026, 10, 26, 9, 0).timestamp()


def scheduled_jobs(bridge):
    with sqlite3.connect(bridge.db_path) as db:
        return db.execute("SELECT owner, idem, action, args FROM jobs WHERE idem LIKE 'schedule:%' ORDER BY created").fetchall()


async def create(bridge, owner, args=SCHED, sid="sid", call="call"):
    approval = "approval-" + owner + call
    bridge.bind_approval(owner, sid, approval, call)
    assert bridge.approval_call(owner, sid, approval) == call  # what /api/confirm does on Approve only
    return await bridge.execute(owner, "desktop_schedule", args, sid, call)


def with_bridge(monkeypatch, tmp_path, check, clock=NOW):
    bridge = make_app(monkeypatch, tmp_path).state.desktop
    bridge.now = Clock(clock)

    async def go():
        await bridge.start()
        try:
            await check(bridge)
        finally:
            await bridge.close()
    asyncio.run(go())
    return bridge


def test_schemas_names_and_approval(monkeypatch, tmp_path, backend):
    async def check(bridge):
        assert {"desktop_schedule", "desktop_list_schedules", "desktop_cancel_schedule"} <= bridge.tool_names
        schemas = {s["function"]["name"]: s["function"]["parameters"] for s in bridge.schemas}
        assert schemas["desktop_schedule"]["required"] == ["action", "arguments", "schedule"]
        assert schemas["desktop_schedule"]["additionalProperties"] is False
        assert schemas["desktop_cancel_schedule"]["required"] == ["schedule_id"]
        assert bridge.needs_approval("desktop_schedule", SCHED) is True
        # Even low-risk actions need approval: it authorizes unattended future runs.
        assert bridge.needs_approval("desktop_schedule", {**SCHED, "action": "pause", "arguments": {}}) is True
        assert bridge.needs_approval("desktop_list_schedules", {}) is False
        assert bridge.needs_approval("desktop_cancel_schedule", {"schedule_id": "x"}) is False
        # Invalid calls are never proposed; they go to execute for denial.
        for i, bad in enumerate(({**SCHED, "action": "shell"}, {**SCHED, "arguments": {}}, {**SCHED, "arguments": {"request": "x", "extra": 1}},
                    {**SCHED, "arguments": {"request": 5}}, {**SCHED, "schedule": {"every_minutes": 1}}, {**SCHED, "bogus": 1},
                    {**SCHED, "arguments": []}, {**SCHED, "timezone": "Nowhere/City"}, [])):
            assert bridge.needs_approval("desktop_schedule", bad) is False
            result = await create(bridge, "alice", bad, call=f"bad-{i}")
            assert result["ok"] is False
        assert bridge.list_schedules("alice") == []
    with_bridge(monkeypatch, tmp_path, check)


def test_create_requires_bound_approval_and_is_idempotent(monkeypatch, tmp_path, backend):
    async def check(bridge):
        unapproved = await bridge.execute("alice", "desktop_schedule", SCHED, "sid", "never-proposed")
        assert unapproved["ok"] is False and bridge.list_schedules("alice") == []
        made = await create(bridge, "alice")
        assert made["ok"] is True and made["next_run"] == "2026-10-26T09:00:00+05:30"
        assert "09:00" in made["next_run_text"] and "Monday" in made["next_run_text"]
        again = await bridge.execute("alice", "desktop_schedule", SCHED, "sid", "call")
        assert again["schedule_id"] == made["schedule_id"]
        assert len(bridge.list_schedules("alice")) == 1
        # Approval bound to another owner/session does not authorize.
        bridge.bind_approval("bob", "sid", "appr-b", "bob-call")
        bridge.approval_call("bob", "sid", "appr-b")
        assert (await bridge.execute("alice", "desktop_schedule", SCHED, "sid", "bob-call"))["ok"] is False
        # Proposed (bound) but never approved -- e.g. denied -- never authorizes a reused call id.
        bridge.bind_approval("alice", "sid", "appr-denied", "denied-call")
        assert (await bridge.execute("alice", "desktop_schedule", SCHED, "sid", "denied-call"))["ok"] is False
        assert len(bridge.list_schedules("alice")) == 1
    with_bridge(monkeypatch, tmp_path, check)


def test_max_active_per_owner(monkeypatch, tmp_path, backend):
    async def check(bridge):
        for i in range(50):
            assert (await create(bridge, "alice", call=f"c{i}"))["ok"] is True
        assert (await create(bridge, "alice", call="c50"))["ok"] is False
        assert (await create(bridge, "bob", call="b0"))["ok"] is True
        first = bridge.list_schedules("alice")[0]["id"]
        assert bridge.cancel_schedule(first, "alice")["active"] is False
        assert (await create(bridge, "alice", call="c51"))["ok"] is True
    with_bridge(monkeypatch, tmp_path, check)


def test_owner_isolation_list_cancel(monkeypatch, tmp_path, backend):
    async def check(bridge):
        made = await create(bridge, "alice")
        sid = made["schedule_id"]
        assert (await bridge.execute("bob", "desktop_list_schedules", {}, "s2", "c"))["schedules"] == []
        listed = await bridge.execute("alice", "desktop_list_schedules", {}, "s", "c")
        assert [s["id"] for s in listed["schedules"]] == [sid] and "owner" not in listed["schedules"][0]
        assert listed["schedules"][0]["arguments"] == SCHED["arguments"]
        denied = await bridge.execute("bob", "desktop_cancel_schedule", {"schedule_id": sid}, "s2", "c")
        assert denied["ok"] is False and bridge.list_schedules("alice")[0]["active"] is True
        for bad in ({}, {"schedule_id": 5}, {"schedule_id": "x", "y": 1}):
            assert (await bridge.execute("alice", "desktop_cancel_schedule", bad, "s", "c"))["ok"] is False
        assert (await bridge.execute("alice", "desktop_list_schedules", {"x": 1}, "s", "c"))["ok"] is False
        ok = await bridge.execute("alice", "desktop_cancel_schedule", {"schedule_id": sid}, "s", "c")
        assert ok["ok"] is True and bridge.list_schedules("alice")[0]["active"] is False
        assert (await bridge.execute("alice", "desktop_cancel_schedule", {"schedule_id": sid}, "s", "c"))["ok"] is False
    with_bridge(monkeypatch, tmp_path, check)


def test_due_fire_submits_once_even_if_tick_runs_twice(monkeypatch, tmp_path, backend):
    async def check(bridge):
        made = await create(bridge, "alice")
        assert bridge.tick_schedules() == [] and scheduled_jobs(bridge) == []
        bridge.now.t = NINE + 5
        threads = [threading.Thread(target=bridge.tick_schedules) for _ in range(4)]
        [t.start() for t in threads]
        [t.join(5) for t in threads]
        bridge.tick_schedules()
        jobs = scheduled_jobs(bridge)
        assert jobs == [("alice", f"schedule:{made['schedule_id']}:{int(NINE)}", "ufo_task", json.dumps(SCHED["arguments"]))]
        row = bridge.list_schedules("alice")[0]
        assert row["runs"] == 1 and row["active"] is True and row["last_job_id"]
        assert row["next_run"] == "2026-10-27T09:00:00+05:30"
    with_bridge(monkeypatch, tmp_path, check)


def test_one_shot_fires_once_then_deactivates(monkeypatch, tmp_path, backend):
    args = {"action": "pause", "arguments": {}, "schedule": {"at": "2026-10-26T08:20:00+05:30"}}

    async def check(bridge):
        made = await create(bridge, "alice", args)
        assert made["next_run"] == "2026-10-26T08:20:00+05:30"
        bridge.now.t = NOW + 1200
        bridge.tick_schedules()
        bridge.now.t = NOW + 999999
        bridge.tick_schedules()
        assert len(scheduled_jobs(bridge)) == 1
        row = bridge.list_schedules("alice")[0]
        assert row["active"] is False and row["next_run"] is None and row["runs"] == 1
    with_bridge(monkeypatch, tmp_path, check)


def test_cancelled_never_fires(monkeypatch, tmp_path, backend):
    async def check(bridge):
        made = await create(bridge, "alice")
        bridge.cancel_schedule(made["schedule_id"], "alice")
        bridge.now.t = NINE + 1
        bridge.tick_schedules()
        assert scheduled_jobs(bridge) == []
    with_bridge(monkeypatch, tmp_path, check)


def test_not_ready_never_fires(monkeypatch, tmp_path, backend):
    async def check(bridge):
        await create(bridge, "alice")
        bridge.now.t = NINE + 1
        bridge.ready = False
        assert bridge.tick_schedules() == []
        assert scheduled_jobs(bridge) == []
        assert bridge.list_schedules("alice")[0]["runs"] == 0
    with_bridge(monkeypatch, tmp_path, check)


@pytest.mark.parametrize("late,fires", [(30 * 60, True), (2 * 3600, False)])
def test_restart_catch_up_once_within_grace(monkeypatch, tmp_path, backend, late, fires):
    holder = {}

    async def setup(bridge):
        holder["id"] = (await create(bridge, "alice"))["schedule_id"]
    with_bridge(monkeypatch, tmp_path, setup)

    async def after_restart(bridge):
        bridge.tick_schedules()
        bridge.tick_schedules()
        jobs = scheduled_jobs(bridge)
        row = bridge.list_schedules("alice")[0]
        assert row["next_run"] == "2026-10-27T09:00:00+05:30"
        if fires:
            assert [j[1] for j in jobs] == [f"schedule:{holder['id']}:{int(NINE)}"] and row["runs"] == 1
        else:
            assert jobs == [] and row["skipped"] == 1 and row["runs"] == 0
    with_bridge(monkeypatch, tmp_path, after_restart, clock=NINE + late)


def test_every_minutes_collapses_missed_intervals(monkeypatch, tmp_path, backend):
    args = {"action": "pause", "arguments": {}, "schedule": {"every_minutes": 10}}

    async def check(bridge):
        await create(bridge, "alice", args)
        bridge.now.t = NOW + 600 * 3 + 5  # three intervals elapsed, within grace
        bridge.tick_schedules()
        assert len(scheduled_jobs(bridge)) == 1
        assert bridge.list_schedules("alice")[0]["next_run_ts"] == NOW + 2400
    with_bridge(monkeypatch, tmp_path, check)


def provider(name, args):
    def handle(request):
        payload = json.loads(request.content)
        if payload["messages"][-1]["role"] == "tool" or not payload.get("tools"):
            message = {"role": "assistant", "content": "Done"}
        else:
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "stable-call-1", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]}
        return httpx.Response(200, json={"choices": [{"message": message}]})
    return httpx.MockTransport(handle)


def test_chat_proposes_then_button_approval_creates_and_routes_scoped(monkeypatch, tmp_path, backend):
    app = make_app(monkeypatch, tmp_path)
    app.state.llm_transport = provider("desktop_schedule", SCHED)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 40000)) as client:
        assert client.get("/api/desktop/schedules").status_code == 401
        assert client.post("/api/login", json={"token": TOKEN}).status_code == 200
        bridge = app.state.desktop
        bridge.now = Clock(NOW)
        reply = client.post("/api/chat", json={"text": "every weekday at 9 update my sheet"}).json()
        pending = reply["confirmation"]
        assert pending["tool"] == "desktop_schedule" and pending["arguments"] == SCHED
        assert client.get("/api/desktop/schedules").json()["schedules"] == []
        done = client.post("/api/confirm", json={"id": pending["id"], "approve": True, "session_id": reply["session_id"]})
        assert done.status_code == 200, done.text
        result = done.json()["results"][0]["result"]
        assert result["ok"] is True, result
        listed = client.get("/api/desktop/schedules").json()["schedules"]
        assert [s["id"] for s in listed] == [result["schedule_id"]]
        # A later login with the same access token is the same person: schedules made earlier
        # must remain visible and cancellable, never orphaned yet still firing.
        client.cookies.clear()
        assert client.post("/api/login", json={"token": TOKEN}).status_code == 200
        assert [s["id"] for s in client.get("/api/desktop/schedules").json()["schedules"]] == [result["schedule_id"]]
        assert client.post("/api/desktop/schedules/missing/cancel").status_code == 404
        other = bridge._create_schedule_row("someone-else", "ufo_task", {"request": "x"}, {"every_minutes": 30}, "Asia/Kolkata", NOW + 1800, "k")
        assert client.post(f"/api/desktop/schedules/{other}/cancel").status_code == 404
        assert bridge.list_schedules("someone-else")[0]["active"] is True
        cancelled = client.post(f"/api/desktop/schedules/{result['schedule_id']}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["active"] is False
        client.cookies.clear()
        assert client.post(f"/api/desktop/schedules/{result['schedule_id']}/cancel").status_code == 401
