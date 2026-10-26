# Jarvis Desktop — installable voice app (no web UI)

Goal: a Windows app anyone can install, enter their own AI provider key once, then use purely by voice.
Jarvis listens for "Jarvis", holds a conversation, does tasks on this PC (Microsoft UFO for general
app control, fast built-in tools where possible), runs scheduled tasks, and speaks results.

## Package layout (new, inside this repo)

```
jarvis_desktop/
  __main__.py        python -m jarvis_desktop  [--setup] [--doctor] [--text "say this"]
  config.py          load/save settings; secrets in Windows Credential Manager (keyring)
  setup_wizard.py    first-run Tk window: provider base URL, API key, chat model, voice, autonomy level
  audio.py           mic capture (sounddevice), energy+webrtc-free VAD endpointing, playback
  stt.py             provider /audio/transcriptions -> fallback local faster-whisper
  tts.py             provider /audio/speech -> fallback Windows System.Speech (offline)
  agent.py           LLM tool loop (OpenAI-compatible), tool registry, routing policy, memory
  tools.py           tool adapters: ufo_task, youtube (fast path), open_app, workbook, research,
                     schedule/list/cancel, job status/cancel, time/weather
  confirm.py         spoken confirmation policy (half-duplex, see below)
  proxy.py           in-process loopback LLM proxy for UFO (strips temperature/top_p/n)
  scheduler.py       wraps jarvis/schedule.py + durable SQLite, fires into the job runner
  jobs.py            background job runner around companion.queue.JobQueue + companion.ufo.UFORunner
  tray.py            optional tray icon: status, Pause listening, Settings, Quit
  app.py             wires everything; state machine IDLE->LISTENING->THINKING->SPEAKING->IDLE
```

Existing code reused, not rewritten: `companion/ufo.py` (UFORunner), `companion/queue.py`,
`companion/actions.py` (ActionEngine), `companion/youtube.py`, `companion/launcher.WakeController`,
`jarvis/schedule.py`, `jarvis_tools.py` (research/weather).

## Interaction contract

- Wake: offline Vosk "jarvis" (WakeController). After wake, conversation continues without re-wake
  until ~20 s of silence or "that's all / stop listening". "Stop" while speaking interrupts playback.
- Half-duplex: microphone input is ignored while Jarvis is speaking, except a short barge-in
  keyword check, so Jarvis's own voice can never approve anything.
- Speak naturally: no markdown/emoji/URLs/JSON in speech.

## Autonomy / confirmation (user picks in setup; default "balanced")

- `careful`:   every desktop action is read back and needs a spoken "yes"/"go ahead".
- `balanced`:  read-only and reversible actions run immediately (open app, play/pause/skip ad,
               search, read a sheet, status). Consequential ones need spoken yes: sending email or
               messages, purchases, deleting/overwriting files, creating schedules, ufo_task requests
               the router flags as consequential (send/pay/delete/submit/install/uninstall/format).
- `autonomous`: only purchases, deletions and sending messages need a spoken yes.
Confirmation reads back the exact action in plain words and accepts yes/no; anything else = no.

## Speed routing (the "take decisions that fasten things up" part)

The agent always prefers a deterministic fast tool and only falls back to UFO:
1. youtube_play / pause / resume / skip / volume -> Playwright controller with automatic ad skipping
   (background watcher clicks a visible enabled Skip button as soon as it appears; never blocks ads
   any other way).
2. open_app, workbook read/write (openpyxl), research/weather/time -> direct.
3. Anything else on the desktop -> ufo_task (background job; Jarvis says it started, reports when done,
   and announces completion proactively).
Independent tasks may run in parallel only when they don't share the screen (e.g. research while a
UFO job runs); screen-touching jobs are serialized.

## Install contract

`Install-Jarvis.cmd` (double-click, no admin):
1. Ensures uv (downloads the official standalone uv release zip from GitHub, verifies its published
   SHA-256; never pipes a remote script into a shell).
2. Installs Jarvis to `%LOCALAPPDATA%\Jarvis` with Python 3.12 deps (`--extra desktop --extra app`).
3. Installs Microsoft UFO to `%LOCALAPPDATA%\Jarvis\ufo` at a pinned commit with our patches applied
   (`patches/ufo-*.patch`), its own Python 3.11 venv, and `agents.yaml` pointing to the in-process
   proxy (no key in UFO config).
4. Downloads the Vosk wake model (verified size/zip-safe extraction).
5. Creates Start menu + optional "start with Windows" shortcut, then launches first-run setup.
Uninstall: `Uninstall-Jarvis.cmd` removes the folder, shortcuts and stored credential.
