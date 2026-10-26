"""Live end-to-end: real LLM chooses ufo_task from natural language, button approval, real UFO run.

Hits the running local companion (default http://127.0.0.1:8767). Evidence is independent
(job status + an artifact check), not the model's prose.
"""
import json, os, sys, time
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = os.getenv("JARVIS_E2E_URL", "http://127.0.0.1:8767")
env = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))
token = env["JARVIS_ACCESS_TOKEN"].strip().strip("'\"")
out = ROOT / "workspace" / "desktop" / f"e2e-note-{int(time.time())}.txt"
marker = f"Jarvis end to end {int(time.time()) % 100000}"
report = {"request": None}
with httpx.Client(base_url=BASE, timeout=120, headers={"Origin": BASE}) as c:
    assert c.post("/api/login", json={"token": token}).status_code == 200
    tools = c.get("/api/status").json()["tools"]
    assert {"desktop_action", "desktop_job_status", "desktop_schedule"} <= set(tools), tools
    text = f"Please open Notepad on this PC, write exactly: {marker} and save it as {out}"
    report["request"] = text
    reply = c.post("/api/chat", json={"text": text}).json()
    pending = reply.get("confirmation")
    assert pending, reply
    report["proposed"] = {"tool": pending["tool"], "arguments": pending["arguments"]}
    assert pending["tool"] == "desktop_action" and pending["arguments"]["action"] == "ufo_task", pending
    done = c.post("/api/confirm", json={"id": pending["id"], "approve": True, "session_id": reply["session_id"]}).json()
    result = done["results"][0]["result"]
    report["after_approve"] = {k: result.get(k) for k in ("ok", "pending", "status", "job_id")}
    report["spoken_reply"] = done.get("text")
    job_id = result["job_id"]
    deadline = time.time() + 900
    while time.time() < deadline:
        status = c.get(f"/api/desktop/jobs/{job_id}").json()
        if status["status"] not in ("queued", "running"):
            break
        time.sleep(5)
    report["job"] = {"status": status["status"], "result": status.get("result")}
    content = out.read_text(encoding="utf-8", errors="replace").strip() if out.exists() else None
    report["evidence"] = {"file": str(out), "content": content}
    report["verified"] = content == marker
print(json.dumps(report, indent=2))
(ROOT / "data" / "ufo-e2e-report.json").write_text(json.dumps(report, indent=2))
sys.exit(0 if report["verified"] else 1)
