# Verification

## Native realtime release checks

- Combined integration suite before final approval hardening: 199 Python tests + 36 subtests; 73 Node tests (native + legacy).
- `tests/live_realtime_check.py` exercised the deployed public HTTPS app with a real spoken MP3 injected as a microphone media track (not a physical microphone).
- Provider VAD detected speech; provider transcription matched the question; `ask_jarvis` reached the authenticated server; current_time returned actual time; native provider audio spoke the answer.
- Live Interrupt while a story was playing produced provider `output_audio_buffer.cleared`. End closed the peer and stopped microphone tracks.
- Live gateway hangup returned false. Do not claim provider-enforced session termination or spending cap.
- No page errors or mobile horizontal overflow. Physical acoustics/echo remain unverified.
- Independent review found unsafe native spoken approvals (delayed self echo and punctuation-changing identifiers). Native voice approval is disabled; consequential actions use exact visible button confirmation. Both attack regressions pass. Final suites: 199 Python tests plus 36 subtests, and 77 frontend tests.
- Source-only rollback archive and online SQLite backup exist locally under ignored data/. Backup integrity and committed WAL recovery tested. Dependencies locked in uv.lock.

## Earlier legacy checks


Executed against the implementation:

- Python suite: 48 passed; 6 subtests passed.
- Browser logic suite: 14 passed.
- Live public HTTPS: page 200; unauthenticated API 401; authenticated API 200; Secure HttpOnly session cookie; cross-origin mutation 403.
- Live browser at desktop and 390px mobile widths: chat/tool execution and explicit approval passed, zero page errors, no horizontal overflow.
- Live local Whisper base CPU/int8: real public speech recording uploaded and transcribed successfully.
- Live LLM called current_time and returned the actual tool result.
- File creation absent before approval; created after approval and read back; repeated approval rejected 409.
- Timer created and read back with UTC due time. Restart persistence covered by tests.
- Gmail authenticated and performed read-only inbox search; email content not printed.
- Telegram getMe authenticated; no live outbound test message sent.
- Weather returned current Open-Meteo result.
- Public search approval followed by live SearXNG results succeeded.
- Security review identified unapproved external-query leakage; reproduced for search/weather, fixed by explicit argument approval, both regression cases pass.

Not verified/available:
- Physical microphone, wake-word accuracy and audible playback on user's device.
- Live Gmail send or Telegram send (not sent merely for testing).
- Calendar authorization not configured.
- Arbitrary shell and control of browser device deliberately not provided.
- Reliable background/locked-phone listening, permanent public URL and automatic reboot startup not provided.

Runtime processes are running in this session with persistence enabled: Jarvis, Cloudflare tunnel and SearXNG. They do not survive a VM reboot automatically. Quick Tunnel has no uptime guarantee and its URL changes when restarted.
