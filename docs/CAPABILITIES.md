# Jarvis capabilities and limits

## Available now

| Capability | Scope / caveat |
|---|---|
| Native voice conversation | WebRTC microphone to realtime model; streamed spoken replies, optional captions |
| Interruption, mute, volume, end | Controls this page's audio; not device-wide sound |
| Public research | Search and source-page reading; answers should state dates, units, uncertainty; blocked pages can limit evidence |
| Weather and time | Open-Meteo/current server time tools |
| Gmail search/read | Configured shared owner's mailbox; private tool results go to configured model |
| Gmail send / Telegram send | Requires explicit button approval; Telegram destination is fixed server configuration |
| Timers/reminders | Stored on server; legacy page polls them; do not assume native-page audible alarms or closed-tab notifications |
| Files | Only allowed server workspace, new-file writes with approval, no overwrites or secret paths |
| PC utilities | Notepad/Calculator and allowlisted diagnostics on server VM, with approval |
| Music lookup | Search link only; no integrated music-player control |
| Text fallback | /legacy.html |

## Not connected / not guaranteed

- Calendar requires separate Google authorization.
- No smart-home devices, screen control on user's phone/laptop, or arbitrary shell.
- No reliable locked-phone/background microphone or automatic reconnect/unmute.
- Shared-token personal deployment, not multi-user production authentication.
- Provider-side hangup and hard spending limits are not verified.
- Camera snapshots and PNG/JPEG/WebP upload are available in native mode. Local preview only until explicit Show Jarvis or a disclosed exact camera voice request. Native provider image-to-speech and deployed upload tested; physical camera still needs device testing.

## Why a conversation can stop

- Server deployment/restart: recent development did interrupt service. Future work should test separately and batch deployments.
- Tab hidden / page closed: intentional privacy cleanup stops the mic and session.
- Session reaches its 10-minute local limit: requires explicit new Start.
- Peer/data-channel failure: mic muted immediately, 30-second network grace, then closed; reconnect is explicit.
- Noise triggering voice activity is a speech interruption, not necessarily a connection restart. Physical echo/noise diagnosis requires actual device observation.

## Sensible next additions

1. Explicit camera snapshot or image upload with spoken visual explanation.
2. Calendar OAuth and calendar/reminder UI in native mode.
3. Home Assistant integration for named lights/plugs, with per-device authorization.
4. Document OCR/read-aloud and searchable user-selected documents.
5. Opt-in durable preferences and memory with deletion controls.
6. Installed desktop/mobile companion for better background behavior and device-local actions.
7. Stable domain, managed service restart, authenticated user accounts, spending limits and monitoring.
