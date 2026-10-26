# Desktop companion contract

This package controls only the machine where it is installed. It starts no service or network listener. The calling bridge must enforce local enablement, authentication, owner identity and approval policy. Do not accept owner identity directly from unauthenticated clients.

## Python API

- `ActionEngine(workspace=Path(...)).execute(name, args) -> dict` (`ok: bool`, error code/message on failure).
- `.capabilities() -> dict`: `scope=installed_machine`, `apps` mapping to `{available,path}`, optional-dependency availability `workbooks`/`youtube`. Dependency availability is not proof of working media or an audio device.
- `ACTIONS`: OpenAI function-tool schemas. `ACTION_RISKS` and `action_risk(name) -> bool`; create/write workbook require approval, unknown names default risky. Other actions still require authenticated locally-enabled access.
- Actions: `open_app(app)` allowlist chrome/edge/excel/notepad/calculator/explorer; `create_workbook(path,sheet,rows)`, `read_range(path,sheet,range)`, `write_range(path,sheet,start_cell,rows)`; `play_youtube(query)`, `pause()`, `resume()`, `set_volume(volume)` integer 0–100, `skip_ad()`, `stop()`, `capabilities()`.
- `JobQueue(db_path, engine).submit(action,args,owner,idempotency_key,depends_on=None) -> dict`.
- `.get(job_id,owner)`, `.cancel(job_id,owner)`, `.run_next() -> dict | None`, `.worker(stop_event=None,poll_interval=0.25)` foreground loop. Parent owns lifecycle.
- Job dictionaries: `id, owner, action, state, result, created, depends_on, cancel_requested`. No `status` alias. Missing/foreign IDs raise KeyError; invalid payload/key reuse/full queue raises ValueError.

Use ONE shared DB per companion desktop. Same-DB operations serialize through a process/thread and OS file lock. Each engine keeps browser calls on one dedicated executor thread because Playwright sync objects are thread-affine. Reopening while a worker is active does not interrupt it. A crashed worker's started jobs become `interrupted`, never auto-retried. Idempotency is per owner/key and includes exact canonical arguments/dependency. Dependency failure produces `cancelled` with `dependency_failed`. Cancellation of running work sets a flag, does not claim undo; the actual outcome remains succeeded/failed.

## Limits and caveats

Workbooks are restricted to the configured workspace (choose a dedicated Desktop subfolder if desired), existing parent directories, .xlsx only. Exclusive atomic new publication; atomic replacement on writes. No formula-like string writes (`=,+,-,@` after leading whitespace); negative numeric values are supported. At most 1000 cells/10000 characters per operation, 10MB archive and 50MB expanded ZIP. No symlink, junction, hardlink, traversal, NTFS stream or reserved-device targets. These checks assume the local account/workspace is trusted; they are not a sandbox against a malicious process racing filesystem changes. openpyxl edits can alter unsupported advanced Excel features; this is a routine table editor, not a full Excel fidelity engine.

Queue payload/result limit 64KB; 100 pending jobs maximum. Structured secret keys and recognizable credential strings are redacted from stored results; free-form content is not guaranteed secret-free. Queue arguments persist locally and are not encrypted. OS account permissions protect the database; owner IDs alone are not authentication. Retain the DB to retain idempotency history.

YouTube uses `workspace/data/desktop-browser`, never a personal Chrome profile. Visible installed Chrome first, Playwright Chromium fallback if installed. Search uses actual visible non-Short DOM links; consent/login/captcha/restrictions need the user. Resume verifies currentTime advances and paused=false; no claim about audible output. Skip clicks only an enabled visible Skip ad control. Stop pauses this player; the user can also close its window. `set_volume` changes this player only, not OS volume. Call `engine._youtube.close()` during shutdown if it was created. `worker --once` closes its browser when that CLI exits; use the persistent parent worker for ongoing playback.

## CLI

`python -m companion --workspace <folder> --db <sqlite-path> submit capabilities --owner local --key unique-key`

`python -m companion --db <sqlite-path> worker --once`

`python -m companion --db <sqlite-path> status <job-id> --owner local`

`python -m companion --db <sqlite-path> cancel <job-id> --owner local`

No startup daemon is installed automatically.

## Verification in this VM

Companion tests exercise actual workbook creation/read/write, atomic exclusive publication, limits, traversal/formula/unknown-command denial, links, owner isolation, persistence, idempotency, cancellation, dependencies, active-worker exclusion, bounded results, CLI, and real worker-process crash recovery. Player state/blocked autoplay tests use simulated DOM state.

Live execution: Chrome launch requested successfully; visible dedicated Chrome navigated to YouTube results and read an actual video title/link without playing. Excel discovery returned unavailable. Created and read back `data/companion-smoke-43eca1bc.xlsx` (Verification sheet, A1:B3); values included 42 and numeric -3. No audible playback or physical microphone verification was performed. No background worker/service was left running.
