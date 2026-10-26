# UFO desktop tasks (opt-in)

`ufo_task {request}` is a normal `desktop_action` action: risky, so it needs the existing exact approval. Job id, owner scoping, polling and cancel-request flags stay in `JobQueue`/`DesktopBridge`; there are no global UFO handles and no `ufo_status`/`ufo_cancel` entries in `ACTIONS`.

Enable (both required): `JARVIS_UFO_ENABLED=true`, `JARVIS_UFO_HOME=<absolute UFO checkout>` (uses `<home>\.venv\Scripts\python.exe`).
Optional: `JARVIS_UFO_TIMEOUT_SECONDS` (default 600, max 1800), `JARVIS_UFO_LOG_RETAIN` (default 20 `jarvis-*` log dirs).

Behaviour
- `ActionEngine.execute_cancellable(name, args, cancel_check)` runs the UFO subprocess synchronously on the queue worker; `cancel_check()` is polled ~10x/s (an exception counts as cancel). `engine.close()` cancels an active run first.
- Fixed argv (`-m ufo --task=jarvis-… --mode=normal --request=<text> --log-level=OFF`), no shell, stdin closed, scrubbed environment, Windows Job Object kills the whole process tree on timeout/cancel. Request only: no plan/path arguments, so no arbitrary file reads. One run at a time; no retry.
- Outcome comes from UFO's `evaluation.log` (`complete` yes/no/unsure), never exit code alone. Missing/garbled evaluation => `outcome: uncertain`, `ok: false`. Timeout/cancel/crash report possible partial side effects.
- Logs/screenshots stay in `<home>\logs\jarvis-*`, pruned to the retention count; never served.
- Capabilities report `enabled/configured/available`, `tested: false`; availability does not promise any given task works. UFO's own provider config receives the request and screenshots.
