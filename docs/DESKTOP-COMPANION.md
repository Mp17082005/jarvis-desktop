# Windows desktop companion

This is a **Python source distribution**, not a standalone compiled EXE. It runs Jarvis on the Windows PC where it is installed; it does not give a phone browser control over that phone. Windows 10/11 with a working microphone/audio driver is the intended personal-PC setup. Windows Server without audio hardware can run text and desktop tools, but cannot hear a wake word.

## Install

Prerequisites: a reviewed copy of this project, internet for dependency installation, [uv](https://docs.astral.sh/uv/getting-started/installation/), and installed Google Chrome or Microsoft Edge. The installer uses `uv sync --frozen --extra desktop --python 3.12`; uv may download Python and locked dependencies. This script does not execute a downloaded remote installer. No administrator rights are required.

From PowerShell in the extracted project folder:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_companion.ps1
```

The default destination is `%LOCALAPPDATA%\JarvisCompanion`. To select another directory, add `-Destination 'C:\Users\you\Apps\JarvisCompanion'`. An existing destination's source files are updated; its `.env` and data are left alone. Stop that installation first. Never choose an untrusted/shared destination.

Only allowlisted source/assets are copied. `.env`, `ACCESS.txt`, browser sessions, `.git`, `.venv`, tests, models and runtime data are **not** copied. `-SourceOnly` copies source without installing dependencies or creating shortcuts (useful for packaging checks; not a runnable installation by itself).

Neither login auto-start nor shortcuts are silently registered. Add `-StartMenu` for a Start Menu shortcut; add `-AutoStart` only if you want an explicit per-user Startup-folder shortcut. This does not enable microphone listening automatically. No registry Run key, service, firewall rule or scheduled task is installed.

## Configure your own provider and login

In the installed directory, copy `.env.example` to `.env`, then edit it locally. Replace the access password and provider placeholders with your own values. Never distribute this file or paste credentials into chat. Set `JARVIS_REALTIME_ENABLED=true` only when your configured provider supports the realtime protocol. Conversation audio is sent to that provider; the entire assistant is **not offline**.

The launcher enforces `JARVIS_DESKTOP_ENABLED=true`, `JARVIS_DESKTOP_LOCAL_ONLY=true`, loopback bind `127.0.0.1`, and disables proxy-header trust. It puts runtime data, workspace and isolated browser profile under `%LOCALAPPDATA%\JarvisCompanion\state` by default (override with `--state-dir`). Do not expose this desktop-control instance through a tunnel or proxy. Desktop backend routes must still enforce authentication, loopback and origin checks.

```powershell
& "$env:LOCALAPPDATA\JarvisCompanion\scripts\start_companion.cmd" --doctor
& "$env:LOCALAPPDATA\JarvisCompanion\scripts\start_companion.cmd"
```

The app opens at `http://127.0.0.1:8767` in an isolated Chrome/Edge app window. A busy port is refused rather than attaching to or killing another program; select another with `--port 8777`. Keep the launcher console open. **Ctrl+C stops its server. Closing only the browser window does not stop the server**; the backend must expire the hands-free client lease and close its microphone. If Windows forcibly kills the launcher, a child process may remain; inspect the process before stopping it rather than killing anything that happens to own a port.

For a bounded server smoke test without opening a browser:

```powershell
.\scripts\start_companion.cmd --no-browser --port 8777 --run-seconds 2
```

The JSON `--doctor` report shows browser, Python/package, microphone and wake readiness without displaying credentials or recording audio. An enumerated microphone is not proof that recording or acoustics work; `start_failed` can indicate driver/sample-rate/model problems. Check Windows Settings > Privacy & security > Microphone > Allow desktop apps, the input device selection, and drivers. Do not bypass OS permissions.

## Local wake setup and consent

Idle wake recognition uses Vosk on this PC with the constrained English grammar `jarvis` / `[unk]`, 16 kHz mono PCM. It does not call browser cloud speech recognition and sends no idle audio to a service. A bounded in-memory queue is used; there is no audio recording archive. This is a general speech recognizer with a small grammar, not a production-trained wake-word engine: accuracy and false triggers must be checked on the actual microphone in the actual room.

1. Download the optional model yourself from the official source: [vosk-model-small-en-us-0.15.zip](https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip), approximately 40 MB. See [model list/licensing](https://alphacephei.com/vosk/models). No model download happens automatically during install/start. Review the archive and extract with Windows Explorer into the installed `models` directory; preserve the model's `am`, `conf`, `graph` and other subdirectories.
2. The default discovered folder is `<installation>\models\vosk-model-small-en-us-0.15`. Alternatively set an absolute path in `.env`:
   ```dotenv
   JARVIS_WAKE_MODEL_PATH=C:/Users/you/Models/vosk-model-small-en-us-0.15
   ```
3. Run `--doctor`; both microphone availability and wake prerequisites must be present. There is no fake-microphone fallback. A model alone does not make a machine hear.
4. Open the app, log in, and explicitly start a conversation once. Grant microphone permission when Chrome/Edge requests it and unlock audio playback through that user gesture. Camera permission is separate and optional; no camera is opened by this launcher. No fake-media or permission-bypass browser switches are used.
5. Enable **Allow handsfree · local desktop wake** before or after that first successful Start. The checkbox alone cannot start recording until Start has succeeded. While talking, only the browser captures audio; the native listener is paused. Say exactly **“Stop Jarvis”** or **“go to sleep”**: the browser releases microphone/camera and closes the paid realtime conversation, then the consented local Vosk listener resumes. Say **“Jarvis”** to start a fresh conversation without another click when microphone permission is still granted. Follow-up questions do not need the wake word. This flow requires the authenticated, visible local desktop page; no public URL/mobile/background/lock-screen promise is made.
6. Desktop handsfree automatically returns to standby after **5 minutes** without interaction. Change **Desktop handsfree idle timeout** (30–3600 seconds, page-local; invalid values use 300). Speech, final transcripts, typing, tools and camera requests reset inactivity. Passive live-view frames do not keep a paid session alive; camera always turns off at standby and never resumes on wake. Pending approvals, active work/speech and a muted microphone prevent automatic standby. Mute retains the paid session but does not enable the native listener. Paid usage continues while talking/working; offline idle wake itself does not send audio to a provider. Local closure is not a provider-enforced billing cap.
7. **End conversation**, **“stop listening completely”**, unchecking handsfree, hiding/closing the page, authentication loss or network failure revokes handsfree consent. Return and opt in again; prior permission is queried, never bypassed. A failed listener handoff or failed session close does not loop or restart automatically. Standby is different from full privacy stop. Initial installation/login/browser permissions still require interaction; browser playback restrictions may also require Enable audio. Consequential actions remain button-approved, not voice-approved.
8. Verify wake, conversation, follow-ups, standby, full stop, and audible playback with your own microphone. Windows sleep, revoked permissions, a closed/hidden client or exclusive audio devices may interrupt this flow. Do not claim lock-screen or universal-device always-on listening.

Applications such as Chrome, Excel and music are controlled by the installed companion action adapter, not by arbitrary shell commands. Excel availability and app associations depend on the local PC; installing this companion does not install Microsoft Office or music providers. Review consequential-action confirmation screens before approving.

### Backend integration contract

Import `WakeController` from `companion.launcher` **in the uvicorn process**, not the parent launcher; Python memory is not shared across these processes.

```python
wake = WakeController()  # reads JARVIS_WAKE_MODEL_PATH; does not record
wake.status()           # {available, listening, reason, local_only}
wake.start()            # explicit consent only; returns bool, false on missing mic/model
wake.pop_event()        # returns one bool, expires after 10 s; cooldown 3 s
wake.stop()             # release stream, clear queued audio and wake events
```

Status `available` means prerequisites appear present, not acoustic verification. Handle `model_missing`, `dependencies_missing`, `microphone_unavailable`, `start_failed`, `recognition_failed` without claiming listening. Authenticated loopback endpoints expose `/api/desktop/status` and `/api/desktop/wake`; the frontend must explicitly opt in. Start/stop controls need CSRF/origin checks, short client leases, stop-on-logout/shutdown, and no automatic mic access merely from polling status. Only return wake to the consenting session. Never use wake recognition as authentication or consequential-action approval.

The existing `/api/desktop/wake-control` lease API is unchanged. The frontend posts `{enabled:false}` and reads `/api/desktop/status` back before **both manual and wake starts**; unverified shutdown prevents browser capture. `{enabled:true}` is permitted only after paid teardown has been acknowledged, consent is retained, permission is already `granted`, and no browser session/start/teardown is active. Idle polling every 2 seconds renews the backend lease; there is no wake polling/lease refresh while talking or browser-muted. End/privacy cancellation invalidates in-flight callbacks. Camera remains off and approval phrases are never replayed across sessions.

## Package / uninstall

Build a distributable source ZIP from the project or installation (no credentials, runtime data or model included):

```powershell
.\scripts\start_companion.cmd --package .\dist\jarvis-companion-windows-source.zip
```

Extract the ZIP, enter its `jarvis-companion` folder, then run the installer as above. A ZIP does not include Python or `.venv`; first install requires uv/network. Do not advertise it as an offline single executable.

To uninstall: stop the launcher, remove any explicitly created `Jarvis Companion.lnk` from your Start Menu Programs and Startup folders, then remove the installation directory. Decide separately whether to retain or delete `state` (chat/database/workspace/profile) and your `.env`. No other account or application settings need removal.

## Verification scope

`tests/test_companion_launcher.py` exercises launch environment, loopback-only command, browser isolation, missing-model and missing-mic states, mocked PCM wake lifecycle, credential-free packaging, the real PowerShell source-copy installer, and a real bounded uvicorn launch/shutdown. Mocked PCM is an integration test, not microphone proof. On this Windows Server VM actual device enumeration reported zero input devices; physical wake and audible playback remain unverified and unavailable here.
