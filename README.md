# Jarvis — a voice assistant that runs your Windows PC

Say **"Jarvis"**, then talk. Jarvis answers out loud and does things on your computer: plays YouTube (and clicks *Skip Ad* as soon as it appears), opens apps and websites, works in spreadsheets, looks things up, sets the volume, and hands longer multi-step jobs in any app to [Microsoft UFO](https://github.com/microsoft/UFO). It tells you when background jobs finish, and it can run tasks on a schedule ("every weekday at 9…").

You bring your own AI key (OpenAI or any OpenAI-compatible provider). There is no web page and no account with us.

## Download and install

1. Go to **[Releases](../../releases/latest)** and download **`Jarvis-Setup-<version>.exe`**.
2. Run it. Windows may show *"Windows protected your PC"* because the installer is not code-signed yet — click **More info → Run anyway** if you trust this repository.
3. Wait 5–15 minutes while it downloads its Python packages, Microsoft UFO and the offline wake-word model (about 2 GB). No administrator rights are needed; everything goes into your user folder.
4. In the window that opens, paste your provider address (OpenAI: `https://api.openai.com/v1`), your API key and a model name, choose a safety level, click **Test**, then **Save**. The key is stored in Windows Credential Manager, never in a file.
5. Start **Jarvis** from the Start menu and say **"Jarvis"**.

Uninstall from **Settings → Apps → Jarvis**; this also removes the saved key.

Prefer not to run an .exe? Download `Jarvis-Setup.zip` from the same release, extract it, and double-click `Install-Jarvis.cmd` — it is the same installer.

**Requirements:** Windows 10/11 64-bit · microphone and speakers · internet · an OpenAI-compatible API key with a chat model. Costs are whatever your provider charges.

## Talking to it

| Say | What happens |
|---|---|
| "Jarvis, play lofi hip hop on YouTube" | Opens YouTube, plays it, skips skippable ads |
| "pause" / "resume" / "skip the ad" | Instant player control |
| "open Excel" / "open github.com" | Opens it immediately |
| "add a Total column to my sales sheet" | Desktop task in the background; Jarvis says when it's done |
| "every weekday at 9, open my sales sheet" | Asks once, then runs on schedule |
| "how's that task going?" / "stop it" | Job status / cancel |
| "stop" (while it talks) | Interrupts |
| "that's all" | Back to waiting for "Jarvis" |

## Safety

Jarvis reads back exactly what it is about to do and only acts on a clear spoken "yes" for anything consequential. You choose how often it asks:

- **Careful** — before every action that changes something.
- **Balanced** (default) — before sending, buying, deleting, installing, or creating schedules.
- **Autonomous** — only before buying, deleting or sending.

While Jarvis is speaking the microphone is ignored (except "stop"), so its own voice can never approve anything.

## Privacy

The wake word and speech-to-text run locally on your PC. What you say after the wake word (as text), plus screenshots during desktop tasks run by UFO, are sent to the AI provider you configured. Nothing is sent to us.

## Troubleshooting

Run `Start-Jarvis.cmd --doctor` in `%LOCALAPPDATA%\Jarvis` — it checks the microphone, speaker, wake model, key and provider connection. See [docs/INSTALL.md](docs/INSTALL.md) for more.

## For developers

```bat
uv sync --extra desktop --extra voice --extra app --extra dev
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m jarvis_desktop --doctor
```

Releases are built by GitHub Actions (`.github/workflows/release.yml`) when a `v*` tag is pushed. Design notes: [docs/DESKTOP-APP.md](docs/DESKTOP-APP.md). The earlier browser-based assistant is documented in [docs/WEB-ASSISTANT.md](docs/WEB-ASSISTANT.md).

Microsoft UFO is downloaded from its official repository at install time (MIT license) with two small fixes in `patches/`.
