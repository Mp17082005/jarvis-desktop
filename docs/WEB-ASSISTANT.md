# Jarvis — native realtime voice assistant

## Current default: native WebRTC (private canary)

The main page now uses microphone audio → `gpt-realtime-2.1` → streamed voice over WebRTC. It does not use browser SpeechRecognition or speechSynthesis. Press **Start conversation**, allow the microphone, then speak naturally without repeating a wake word. **Interrupt** clears playback; **End conversation** releases tracks and closes the peer. Captions are optional.

The authenticated server brokers session minting and SDP, so neither long-lived nor ephemeral provider credentials are sent to frontend JavaScript. The provider receives microphone audio directly. Tool requests pass through the authenticated server and existing action-confirmation rules. Native consequential-action approval is button-only: voice approvals are disabled because delayed speaker echo and spoken identifier changes are not a defensible authorization boundary. General realtime captions remain in the browser; tool questions/results still use existing SQLite conversation persistence.

The old browser-recognition/read-aloud console is retained at `/legacy.html` and is an explicit fallback, not the default.

**Release status: controlled private canary, not production certified.** Real deployed audio input, VAD transcription, server tool execution, spoken answer, native buffer interruption, peer cleanup and mobile layout were exercised. Gateway server-side hangup returned `provider_hangup:false`; local 600-second TTL is NOT a proven provider audio/billing cutoff. Shared-password auth, Quick Tunnel, in-memory quotas, absent verified sideband and managed reboot recovery remain production blockers. See `docs/PRODUCTION.md`.

Run reproducibly with `uv sync --frozen --extra voice --extra dev`. Enable native voice via `JARVIS_REALTIME_ENABLED=true` only for trusted private use. Run one worker; do not scale out while quotas and call deduplication are in memory. Use provider billing limits independently.

## Legacy console details


A private FastAPI assistant running on this Windows VM. Open its HTTPS link on a phone or computer, unlock it, and talk or type. The browser supplies microphone and speech playback; the VM supplies reasoning and tools.

## Start

```bat
uv sync --extra voice --extra dev
start.cmd
```

Open http://localhost:8765 on the VM. The generated private password is in `ACCESS.txt`. Never share `.env` or `ACCESS.txt` publicly. Use `public-link.cmd` in another terminal for an HTTPS Cloudflare Quick Tunnel. Keep both processes running. The temporary URL changes when the tunnel restarts and has no production uptime guarantee. A named Cloudflare tunnel is needed for a stable hostname.

## Voice behavior

- Unlock, then explicitly enable the microphone. Browsers require a user gesture and microphone permission.
- Supported browsers use their speech-recognition service and listen for **Jarvis**. Browser recognition may send audio to the browser vendor; it is not guaranteed local/offline.
- Unsupported recognition browsers use push-to-talk and server-side Whisper transcription. The recorded audio goes to this VM.
- Replies use the browser's installed speech-synthesis voices. Availability and sound differ by device.
- The tab must remain open. Mobile operating systems can suspend microphones in background tabs or when the screen locks. A link alone cannot provide a reliable always-listening locked-phone assistant.
- This VM has no detected microphone. Typed chat still works; a local microphone would need to be attached/redirected for voice input here.

## Access and actions

The password grants access to configured personal services. Share it only with trusted people. Sessions use HttpOnly cookies. A confirmation card shows exact arguments before protected actions; spoken “yes” is not an approval bypass.

PC operations target the **server VM**, not the device viewing the page. Apps and diagnostic commands are allowlisted; arbitrary shell access is deliberately not exposed to the public link. File tools are restricted to `workspace/`. Music search supplies a browser link rather than pretending to control a remote music player.

Gmail uses configured IMAP/SMTP credentials, Telegram uses the configured bot and destination, and Calendar needs a separately authorized Google Workspace adapter. Tools report failures instead of inventing results. Reminder storage is on the server; audible notification requires an active connected browser. Do not rely on this prototype for safety-critical alarms.

## Configuration

See `.env.example`. This instance has a project-local `.env` containing existing provider and integration credentials; it is ignored by Git. These credentials are never sent to the frontend. The LLM provider may charge for calls; no free usage is assumed.

For Calendar, authorize Google Workspace separately and set `JARVIS_GOOGLE_SCRIPT` to its `google_api.py` wrapper and `JARVIS_GOOGLE_PYTHON` to the interpreter containing its dependencies. Do not set the adapter until authorization is complete.

## Search service and privacy

Search uses `JARVIS_SEARCH_URL=http://127.0.0.1:8888`. Start the existing SearXNG service with your SearXNG start script if it is stopped. Searches and weather requests require confirmation of their exact outbound arguments to prevent retrieved private content from silently leaving via a query.

Messages and tool results are sent to the configured LLM provider for reasoning. This is not an entirely local assistant.

## Tests

```bat
.venv\Scripts\python.exe -m pytest tests -q
node --test tests/frontend.test.cjs
```

Live checks are recorded separately from isolated unit tests. Network integrations require connectivity; model availability and third-party rate limits can change.
