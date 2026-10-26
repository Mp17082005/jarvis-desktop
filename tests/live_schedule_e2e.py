"""Live: natural-language scheduled task -> approval -> scheduler fires -> real UFO run -> artifact."""
import json, os, sys, time
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = os.getenv("JARVIS_E2E_URL", "http://127.0.0.1:8767")
env = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))
token = env["JARVIS_ACCESS_TOKEN"].strip().strip("'\"")
out = ROOT / "workspace" / "desktop" / f"sched-note-{int(time.time())}.txt"
marker = f"Scheduled by Jarvis {int(time.time()) % 100000}"
report = {}
with httpx.Client(base_url=BASE, timeout=120, headers={"Origin": BASE}) as c:
    assert c.post("/api/login", json={"token": token}).status_code == 200
    text = f"In 2 minutes, open Notepad on this PC, write exactly: {marker} and save it as {out}"
    reply = c.post("/api/chat", json={"text": text}).json()
    pending = reply.get("confirmation")
    assert pending, reply
    report["proposed"] = {"tool": pending["tool"], "arguments": pending["arguments"]}
    assert pending["tool"] == "desktop_schedule", pending
    done = c.post("/api/confirm", json={"id": pending["id"], "approve": True, "session_id": reply["session_id"]}).json()
    created = done["results"][0]["result"]
    report["created"] = {k: created.get(k) for k in ("ok", "schedule_id", "next_run", "next_run_text")}
    report["spoken_reply"] = done.get("text")
    assert created.get("ok"), created
    sid = created["schedule_id"]
    # A fresh login (new session) must still see it.
    c.cookies.clear(); assert c.post("/api/login", json={"token": token}).status_code == 200
    listed = c.get("/api/desktop/schedules").json()["schedules"]
    report["visible_after_relogin"] = any(s["id"] == sid for s in listed)
    deadline = time.time() + 1200
    job_id = None
    while time.time() < deadline:
        row = next(s for s in c.get("/api/desktop/schedules").json()["schedules"] if s["id"] == sid)
        job_id = row["last_job_id"]
        if job_id:
            job = c.get(f"/api/desktop/jobs/{job_id}").json()
            if job["status"] not in ("queued", "running"):
                break
        time.sleep(5)
    report["fired"] = {"runs": row["runs"], "active_after": row["active"], "last_fired": row["last_fired"]}
    report["job"] = {"status": job["status"], "result": job.get("result")} if job_id else None
    content = out.read_text(encoding="utf-8", errors="replace").strip() if out.exists() else None
    report["evidence"] = {"file": str(out), "content": content}
    report["verified"] = content == marker and report["visible_after_relogin"] and row["runs"] == 1 and not row["active"]
print(json.dumps(report, indent=2))
(ROOT / "data" / "ufo-schedule-report.json").write_text(json.dumps(report, indent=2))
sys.exit(0 if report["verified"] else 1)
