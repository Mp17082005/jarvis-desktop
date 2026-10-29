# Installing Jarvis Desktop on Windows

Jarvis Desktop is a voice-only Windows app: say "Jarvis", talk, and it answers and performs tasks on
this PC. It uses your own OpenAI-compatible AI provider. This page covers what the installer does,
options, safety levels, privacy, troubleshooting and removal.

## Requirements

- Windows 10 or 11, 64-bit, signed in to a normal desktop session (desktop control needs a visible desktop).
- Microphone and speakers or headset.
- Internet during install (~2 GB downloaded on first install: Python 3.12 + 3.11, app packages,
  Microsoft UFO with PyTorch, the 40 MB wake model).
- An API key for an OpenAI-compatible provider (OpenAI, Azure OpenAI via a compatible gateway,
  LiteLLM, OpenRouter, local servers that speak `/v1/chat/completions`).
- No administrator rights. Nothing is added to `PATH`; no services or scheduled tasks are registered.

## Quick install (3 steps)

1. **Download and run** `Jarvis-Setup-<version>.exe` from the GitHub Releases page (or extract
   `Jarvis-Setup.zip` and double-click `Install-Jarvis.cmd`). Wait while it downloads its packages.
2. **Paste your API key** into the setup window. Jarvis recognises the provider from the key
   (OpenAI `sk-…`, Claude `sk-ant-…`, OpenRouter `sk-or-…`, Groq `gsk_…`, Gemini `AIza…`, xAI `xai-…`),
   fills in the address and a default model, and lists the models your key can use. For Azure or another
   gateway, also paste the provider URL; for local Ollama pick it from *Provider* (no key needed).
3. **Click Connect.** Jarvis sends one short test message and one tool-call test; when both work it saves
   and closes. If the test fails you see the reason and can fix it, or click *Save anyway*.

Then talk to Jarvis in any of three ways:

- say **"Jarvis"**,
- press **Ctrl+Alt+J** anywhere (global shortcut; change it with `"hotkey"` in `settings.json`,
  e.g. `"win+shift+space"`. If another program already uses the combination, Jarvis logs that and the
  other two ways still work),
- or click the Jarvis **tray icon** (left-click = *Talk now*).

The installer starts Jarvis when you sign in (checked by default; untick *Start Jarvis when I sign in*
to opt out) and creates **Jarvis** shortcuts on the Desktop and in the Start menu.
*Advanced* in the setup window holds the voice model, voice name and the auto-filled URL; the
safety level defaults to *Balanced*.

## What the installer does

`Install-Jarvis.cmd` runs `scripts\install_app.ps1` with Windows PowerShell (`-NoProfile
-ExecutionPolicy Bypass` for that one script only). Steps:

1. **uv** – uses `uv` from `PATH` if present, otherwise downloads the official standalone release
   `uv-x86_64-pc-windows-msvc.zip` from GitHub plus its published `.sha256` file, verifies the
   SHA-256 **before** extracting, and places it in `%LOCALAPPDATA%\Jarvis\tools\uv`. No remote script
   is ever executed.
2. **App** – copies an allowlist of source files (`jarvis`, `companion`, `jarvis_desktop`,
   `jarvis_tools.py`, `pyproject.toml`, `uv.lock`, `scripts`, `docs`, `static`) to
   `%LOCALAPPDATA%\Jarvis\app` and runs
   `uv sync --frozen --extra desktop --extra voice --extra app --python 3.12`.
   If the lock file does not match (development builds), it re-resolves without `--frozen` and
   prints a warning.
3. **Microsoft UFO** – installs [microsoft/UFO](https://github.com/microsoft/UFO) at the commit pinned
   in `patches\ufo-manifest.json` into `%LOCALAPPDATA%\Jarvis\ufo` (git shallow fetch, or the GitHub
   ZIP when git is missing). Jarvis's patches are applied as hash-verified full-file replacements
   from `patches\ufo-files\` (the readable diffs are `patches\ufo-*.patch`):
   - `ufo/module/interactor.py`: never blocks or crashes on a console prompt when run headless;
     sensitive steps are denied unless `UFO_AUTO_APPROVE_SENSITIVE=1` (Jarvis sets it only after
     your approval of that exact task).
   - `ufo/automator/action_execution.py`: fixes the "control not interactable" error path so UFO retries.
   A file that differs from the pinned commit is never overwritten. UFO gets its own Python 3.11
   environment; its `requirements.txt` is installed with one change (`pandas==1.4.3` → `pandas>=2.0,<2.3`,
   since 1.4.3 has no Python 3.11 wheel).
   `config\ufo\agents.yaml` is generated from UFO's template: HOST, APP, BACKUP and EVALUATION agents
   all use `API_TYPE openai`, `API_BASE http://127.0.0.1:8780/v1`, `API_KEY local-proxy`, and
   `API_MODEL` = `ufo_model` from settings (default `gpt-4o`). That address is Jarvis's in-process
   loopback proxy: it holds your real key, forwards to your provider, removes parameters some
   gateways reject, and may substitute your configured model. **Your API key is never written to UFO's
   config.** `system.yaml` is left unchanged.
4. **Wake model** – downloads `vosk-model-small-en-us-0.15.zip` from alphacephei.com, checks the exact
   size (41,205,931 bytes), rejects unsafe archive paths, and extracts it to `models\`.
5. **Shortcuts** – Start menu `Jarvis` → `%LOCALAPPDATA%\Jarvis\Start-Jarvis.cmd` (starts minimized).
6. **Launchers** – writes `Start-Jarvis.cmd` and `Uninstall-Jarvis.cmd` into the install folder.
7. **First-run setup** – if there are no saved settings, opens the setup window
   (`python -m jarvis_desktop --setup`).

### Installer options

Pass them after the file name, e.g. from a terminal: `Install-Jarvis.cmd -AutoStart`.

| Option | Effect |
| --- | --- |
| `-Destination <folder>` | Install elsewhere (default `%LOCALAPPDATA%\Jarvis`). |
| `-AutoStart` | Also start Jarvis when you sign in (Startup folder shortcut). The `.exe` installer passes this unless you untick it. |
| `-NoShortcuts` | Create no shortcuts (default: Start menu and Desktop). |
| `-NoUfo` | Skip Microsoft UFO (saves ~1.5 GB; general "do X in app Y" tasks unavailable; YouTube, open app, spreadsheets, research still work). |
| `-SkipModels` | Skip the wake model download. |
| `-NoSetup` | Do not open the setup window (run `Start-Jarvis.cmd --setup` later). |
| `-UfoSource <folder>` | Use a local UFO git checkout instead of downloading (offline/dev). |
| `-UfoModel <name>` | Model name written into UFO's `agents.yaml`. |

Scripted setup without a window: set the environment variable `JARVIS_LLM_API_KEY`, then run
`Start-Jarvis.cmd --setup --non-interactive --base-url https://api.openai.com/v1 --model gpt-4o-mini`.

### Upgrading

Extract the new ZIP and run `Install-Jarvis.cmd` again. The app code is replaced; `settings.json`,
the stored API key, `data\`, `models\`, and an already-installed UFO environment are kept.
Re-running after a failed install continues where it stopped.

## Using Jarvis

- `Start-Jarvis.cmd` (or the Start menu entry) runs the app. `Start-Jarvis.cmd --setup` changes settings,
  `--doctor` prints a readiness check (never prints secrets), `--text "what time is it"` runs one
  request without the microphone.
- Say "Jarvis", press Ctrl+Alt+J, or click the tray icon, then talk. Follow-ups need no wake word until about 20 seconds of silence or
  "that's all". Saying "stop" while Jarvis speaks interrupts it.

## Autonomy levels

Chosen in setup (default **balanced**). Jarvis reads the exact action back and waits for a spoken
"yes"/"go ahead"; anything else counts as no. Your microphone is ignored while Jarvis speaks, so its
own voice can never approve anything.

| Level | Runs immediately | Needs your spoken yes |
| --- | --- | --- |
| careful | answers, research, time/weather | every desktop action |
| balanced | open apps, YouTube play/pause/skip, read a spreadsheet, status | sending email/messages, purchases, deleting or overwriting files, creating schedules, UFO tasks flagged consequential (send/pay/delete/submit/install/uninstall/format) |
| autonomous | almost everything | purchases, deletions, sending messages |

## Privacy

- **Wake word and speech-to-text run locally.** The "Jarvis" detector (Vosk) and, by default, speech
  recognition (faster-whisper) process audio on this PC; idle listening sends nothing anywhere.
- After the wake word, your **transcribed text** goes to the provider you configured for chat, and
  Jarvis's replies are sent there for text-to-speech (if provider TTS is used; otherwise Windows' offline
  voice). If you choose provider speech-to-text in setup, that audio goes to your provider too.
- Desktop tasks run by UFO send **screenshots** of the relevant windows to your provider so the model
  can see the screen. UFO logs (including screenshots) stay in `%LOCALAPPDATA%\Jarvis\ufo\logs` and are pruned.
- The API key is stored only in Windows Credential Manager (service `Jarvis`). Nothing is sent to
  the Jarvis authors.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "Extract the entire ZIP first" | You ran it inside the ZIP. Extract All, then run it from the folder. |
| uv download / SHA-256 mismatch | Network or proxy altered the download. Retry; or install uv yourself (https://docs.astral.sh/uv/) and rerun. |
| `uv sync` fails | Check internet/proxy; rerun Install-Jarvis.cmd. Corporate proxies: set `HTTPS_PROXY` first. |
| UFO install fails or too large | Rerun to continue, or install with `-NoUfo`. |
| "differs from pinned commit; refusing to overwrite" | The UFO folder was edited. Delete `%LOCALAPPDATA%\Jarvis\ufo` and rerun. |
| Wake model size mismatch | Download was cut off. Rerun, or install with `-SkipModels` and place the model in the `models` folder yourself. |
| Jarvis does not hear "Jarvis" | Windows Settings > Privacy > Microphone: allow desktop apps. Check the default input device. Run `Start-Jarvis.cmd --doctor`. |
| Wrong key / provider errors | `Start-Jarvis.cmd --setup` (or tray > Settings), paste the key again, click Connect. |
| Ctrl+Alt+J does nothing | Another program owns that shortcut. Set `"hotkey"` in `%LOCALAPPDATA%\\Jarvis\\settings.json` (e.g. `"ctrl+alt+shift+j"`) and restart Jarvis; saying "Jarvis" or the tray icon still works. |
| Desktop tasks fail | UFO needs an unlocked, visible desktop session (not a locked screen or disconnected RDP). |
| Shortcut missing | Rerun installer without `-NoShortcuts`. |

## Uninstall

Double-click `Uninstall-Jarvis.cmd` (in the extracted folder or in `%LOCALAPPDATA%\Jarvis`), answer `Y`.
It stops Jarvis processes running from the install folder, removes the Start menu, Desktop and Startup
shortcuts that point to this install, deletes the saved API key from Windows Credential Manager, and deletes the whole install
folder (app, UFO, models, settings, data, logs). Nothing is kept. uv's download cache
(`%LOCALAPPDATA%\uv\cache`) is shared with other tools and is not removed; delete it manually if wanted.

## Building the release ZIP (developers)

`powershell -NoProfile -ExecutionPolicy Bypass -File installer\build_release.ps1` writes
`dist\Jarvis-Setup.zip` from a positive allowlist and verifies that it contains no `.env`,
`ACCESS.txt`, `settings.json`, `agents.yaml`, data, workspace, tests, virtual environments or caches.
Regenerate UFO patches after changing the UFO checkout:
`git -C <UFO> diff -- ufo/module/interactor.py > patches/ufo-01-interactor-noninteractive.patch`
(and the same for `action_execution.py`), copy the patched files into `patches/ufo-files/` with LF
line endings, and update hashes/commit in `patches/ufo-manifest.json`.

Tests: `pytest tests/test_app_installer.py` (the `real` tests perform a full install into
`%LOCALAPPDATA%\hermes\cache\scratch\jarvis-install-test`; set `JARVIS_INSTALL_TEST_NETWORK=1` to
also download the wake model).
