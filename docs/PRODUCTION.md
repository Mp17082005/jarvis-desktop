# Production release gates and operations

**Status: not approved for production.** A passing HTTP smoke check is not a realtime-voice, security, durability, or availability certification. This document records requirements, not completed deployments. The existing shared-token personal assistant must not be advertised as a multi-user production service.

## Audit scope and observed baseline

Source reviewed: `jarvis/main.py`, `jarvis/store.py`, `start.cmd`, `public-link.cmd`, `pyproject.toml`, and existing tests. No `.env`, `ACCESS.txt`, personal database, email, or other personal data was read. No credentials, provider sessions, messages, installations, service restarts, or deployment changes were used for the live check.

Read-only check on 2026-10-21, approximately 17:22 UTC:

| Target | GET `/` | Unauthenticated GET `/api/status` |
| --- | --- | --- |
| `http://127.0.0.1:8765` | 200 | 401 |
| `https://your-jarvis.example.com` | 200 | 401 |

The public requests succeeded with Python's default trusted-certificate and hostname verification, without redirects or authentication. This shows reachability and denial at the tested endpoint at that time, not certificate-expiry monitoring, a stable origin, provider health, authenticated cookie behavior, or physical microphone quality. The Quick Tunnel hostname may change or stop working.

At this source snapshot:

- API sessions use a common access token, hashed session identifiers in SQLite, 30-day expiry, and HttpOnly/SameSite=Strict cookies. Secure depends on the trusted request scheme. Session ownership isolates conversations by login session, not by a durable named user; tool credentials are server-wide.
- Origin checks, request-size caps, login throttling, per-session chat throttling/locks, and owner/conversation-bound single-use confirmations exist. These are useful controls, but in-memory throttles/locks are not distributed admission or spend enforcement.
- The existing `/ws` is text chat, not proof of native audio/WebRTC operation. Native realtime support and provider capability are under separate review.
- SQLite stores conversation messages, tool results, and confirmation arguments. Conversation trimming is not a retention policy. Expired sessions/conversations are removed on login; comprehensive scheduled expiry of confirmations/timers/backups is not present in the reviewed store.
- `/api/status` reports configuration flags, model/tool metadata, and STT availability. It is authenticated, not a dependency readiness endpoint. Dedicated liveness/readiness/metrics routes were not present in the reviewed app.
- Startup scripts run foreground Uvicorn and a Quick Tunnel; they do not establish service-manager recovery or reboot persistence. This audit did not inspect installed services, so deployment outside these scripts remains unverified.

## Mandatory release gates

Every unchecked item is blocking for the relevant production rollout. Attach test evidence, release identifier, named owner, and approval before marking complete.

### 1. Stable origin and transport

- [ ] Provision an owned, stable HTTPS hostname with managed TLS renewal; replace Quick Tunnel as the production ingress. Monitor DNS, expiry, edge-to-origin availability, and renewal failures.
- [ ] Keep Uvicorn private/loopback. Trust proxy headers only from the actual ingress; verify spoofed forwarded headers cannot bypass origin checks or change cookie security. Validate Host against an explicit allowlist.
- [ ] Verify Secure, HttpOnly, SameSite cookies through the public ingress, cross-origin mutation rejection, WebSocket origin checks, redirect behavior, logout/revocation, and unauthenticated API denial.
- [ ] Set an intentional TLS/HSTS policy at ingress and test relevant security headers on errors as well as successful responses. Review CSP for exactly the provider endpoints native voice requires; do not replace `connect-src 'self'` with a wildcard.

### 2. Identity and authorization

- [ ] Replace the shared token with individually revocable user identities (prefer managed OIDC/passkeys and MFA for privileged access). If retained for a private single-owner canary, explicitly restrict audience and document that limitation; do not call it multi-user auth.
- [ ] Separate user identity, browser session, conversation, provider call, pending approval, and external tool account. Bind all of them server-side. A new login must not evade per-user quotas or inherit another user's tool authorization.
- [ ] Define idle/absolute login expiry, logout, compromised-session revocation, credential rotation, and server/provider termination on revocation. Restrict administrative tools and external integrations to their authorized owner.
- [ ] Store long-lived provider/integration credentials server-side with restricted filesystem/service-account access; no browser bundle, localStorage, SDP log, query string, debug response, or crash dump exposure.

### 3. Native realtime security boundary (browser audio directly to provider)

- [ ] Demonstrate that the actual configured provider supports the required WebRTC negotiation, session configuration, tool events, server sideband, cancellation, call termination, and usage reporting. Chat-completion compatibility does not establish realtime support.
- [ ] Authenticate and origin-check session creation; enforce quotas **before** creating/billing a provider call. Server-mint short-lived, narrowly scoped credentials or server-negotiate the call. Never give a permanent API key to the browser. Prove credential replay/lifetime restrictions in the actual provider, not only an app timer.
- [ ] Maintain a server-owned mapping from provider call ID to authenticated user/session/conversation. Reject arbitrary browser-submitted call IDs or owner IDs. Validate expiry and ownership on attach, events, tools, confirmation, and termination.
- [ ] Treat browser data-channel messages and model tool arguments as untrusted. Tool execution must happen only through authenticated server sideband and the existing allowlist/schema/risk/owner checks. Browser tool results, model claims of approval, and spoken 'yes' are not authorization.
- [ ] Bind explicit human confirmation to the exact tool and arguments, owner, conversation, and call; short expiry, single use, replay resistance, deterministic full summary. Do not let the model approve itself or let playback/echo arm approval. Test stolen IDs, duplicate/out-of-order events, stale approvals after reconnect, denial, and changed arguments. Prefer an explicit UI approval for high-impact actions.
- [ ] Deduplicate provider call events and tool execution durably. A crash after an external action but before recording its result requires reconciliation, not automatic execution replay; existing consumed-confirmation protection is not an exactly-once guarantee for external systems.
- [ ] If sideband disconnects, owner auth expires, budget is exhausted, or the server restarts, fail closed for tools **and terminate the provider call**. Closing only sideband/browser signaling can leave direct audio and billing alive. If the provider cannot guarantee bounded lifetime after server loss, native production voice remains blocked.
- [ ] Preserve private-data/public-research isolation. Sending browser audio directly to a provider changes the data path and bypasses server audio inspection; disclose provider processing/retention, region, and subprocessors before microphone activation.

### 4. Duration, concurrency, quotas, and spend

- [ ] Enforce the planned **10-minute maximum voice session** on the server and at the provider where possible. This is a requirement, not a verified implementation. A client timeout or ephemeral credential expiry does not necessarily end an established call. Reconnect must not reset the deadline; test server crash and suspended/mobile tabs.
- [ ] Release manifest must specify numeric per-user daily voice-minute allowance, global daily allowance, per-user and global concurrent-call limits, session-creation rate, token/output bounds, idle timeout, and tool-call limits. No unlimited/unset values. Conservative canary recommendation: one active call per user; owner must explicitly approve remaining numeric limits.
- [ ] Reserve capacity atomically before upstream creation, count connecting/active/orphaned calls, release on every terminal path, and reconcile after restart. Enforce limits across workers/instances, not only in memory. Define 429/503 behavior and bounded queues without hidden retries.
- [ ] Set actual currency-denominated daily/monthly spend caps and alert thresholds with the budget owner. Enforce app-side reservations and provider/project hard limits where supported. If provider alerts are not hard limits, document that and implement a kill switch plus bounded in-flight exposure.
- [ ] Reconcile provider-reported audio/text usage with app estimates and invoice totals. Budget exhaustion blocks new calls and terminates active calls according to policy. Test silent calls, abandoned tabs, repeated login/reconnect, tool loops, and lost termination acknowledgments.

### 5. Privacy and data lifecycle

- [ ] Default native voice to **no raw audio or transcript logging** in application, access/error logs, analytics, sideband event dumps, traces, and support captures. Redact tokens, SDP, call identifiers where sensitive, tool arguments/results, and private prompts. Add automated redaction tests.
- [ ] Make saved conversation history an explicit, documented choice distinct from operational logs. Existing `Store.save` persists messages/results; do not claim 'no transcripts stored' merely by disabling logger output. Define migration behavior before routing realtime transcripts into that store.
- [ ] Publish retention/deletion periods for sessions, conversations, confirmations, timers, consent records, minimal security audit metadata, and backups. Implement scheduled expiry, user deletion, and deletion propagation to restored backups; login-triggered cleanup is insufficient.
- [ ] Verify provider-side recording/transcription/retention settings and contractual limits independently of local logging settings. Diagnostic captures require explicit opt-in, limited access, short expiry, and redaction.

### 6. Health, metrics, and incident response

- [ ] Add minimal non-sensitive liveness (`/health/live`, proposed) and readiness (`/health/ready`, proposed). Liveness tests event-loop/process health; readiness tests required configuration, bounded storage access, capacity, and sideband availability. Avoid paid provider calls on every probe; cache bounded dependency probes and expose degraded status.
- [ ] Keep `/api/status` authenticated. Do not expose model/tool/credential metadata through public health or metrics endpoints. Restrict metrics access to operators.
- [ ] Collect aggregated active/connecting/orphan calls, duration/termination reason, admission rejection, setup latency, time to first audio, interruption latency, tool/approval failures, provider errors, auth failures, SQLite lock time, queue depth, and usage/spend. No transcript labels or unbounded user/call-ID cardinality.
- [ ] Define SLOs and alerts before launch; assign on-call ownership. Test alerts for 5xx, public TLS failure, provider outage, budget breach, stalled sideband, failed termination, and restart loop. Smoke scripts do not replace dependency health or audio-quality monitoring.

### 7. Process management and recovery

- [ ] Install app and stable ingress as separately managed Windows services under a least-privilege noninteractive account (an approved service wrapper or equivalent managed supervisor). Specify working directory, exact venv executable, environment/secret source, startup dependencies, logs, recovery backoff, and restart limits. No service installation is performed by this document.
- [ ] Preserve `start.cmd` safeguards: clear inherited `PYTHONPATH`, bind `127.0.0.1:8765`, and trust forwarded headers only from ingress. Pin tested dependencies in a reproducible release artifact; broad version ranges alone are not a reproducible build.
- [ ] Verify automatic restart after process kill, reboot without interactive login, ingress failure, provider loss, and graceful shutdown/draining. Stop admission first, terminate calls, reconcile uncertain tool outcomes, and never replay a mutation blindly.
- [ ] Validate multi-worker behavior before adding workers: present locks/rate limits live in-process; provider call ownership, quotas, and deduplication must work across workers. Define SQLite capacity and migration path rather than assuming horizontal scaling.

### 8. Backups, retention, and restore

- [ ] Declare RPO/RTO, backup schedule, encrypted destination, access owners, and backup expiry. Use SQLite's online backup API or a coordinated consistent snapshot; do not copy only `jarvis.sqlite3` from an active WAL database and assume it is complete.
- [ ] Back up only approved durable state; do not include raw voice/transcript diagnostics by default. Protect secrets separately. Retention must include WAL/checkpoint handling, replicas, snapshots, and expired backup deletion.
- [ ] Restore into an isolated environment with outbound mutations disabled; validate integrity, schema compatibility, owner isolation, expiry, and pending/consumed confirmations. Invalidate restored browser/provider sessions and stale approvals; do not resurrect actions or deleted personal data.
- [ ] Run a timed restore exercise and record actual RPO/RTO evidence. Backup existence alone is not a recovery test.

### 9. Canary and rollback

- [ ] Version backend and browser assets together. Retain an immutable last-known-good artifact and dependency set; record configuration/migration version without secrets.
- [ ] Gate native voice behind an owner allowlist and a server-side kill switch. Canary first with explicitly consenting users, strict budget/concurrency caps, non-destructive tools, and real microphone/speaker tests on intended browsers/devices/networks.
- [ ] Test setup, two-turn conversation, barge-in, exact confirmation, denial, stop, reconnect, microphone permission denial, Bluetooth/echo, mobile background/lock, network loss, and session deadline. Synthetic recognition and mocked APIs do not prove acoustic quality.
- [ ] Set rollback thresholds before canary: any auth/owner-boundary breach or unconfirmed action requires immediate stop; define numeric error/latency/spend thresholds separately. On rollback disable creation, terminate active provider calls, preserve redacted incident evidence, and reconcile uncertain actions.
- [ ] Restore compatible backend/assets/config, retain typed chat fallback, run smoke and authenticated regression tests, and verify termination/billing. Use forward-compatible schema changes; restore a database only under an approved recovery plan, never overwrite newer user state casually.

## Read-only smoke checker

From the repository in Git Bash on this Windows host:

```bash
env -u PYTHONPATH JARVIS_PUBLIC_URL=https://your-jarvis.example.com \
  .venv/Scripts/python.exe scripts/check_readiness.py

env -u PYTHONPATH .venv/Scripts/python.exe -m unittest discover -s tests -p test_readiness.py -v
```

Any Python 3.12+ interpreter works; the checker and its unit tests are stdlib-only. It never imports the application or loads `.env`. `--public-url https://your-owned-origin.example` overrides `JARVIS_PUBLIC_URL`; `--local-url` accepts a loopback origin only. Both origins are checked. Use `--timeout 10` for the per-network-operation timeout (maximum 60 seconds); this is not a strict whole-process deadline, especially for DNS. Supervisors should impose their own total job timeout.

The checker issues four unauthenticated GETs, consumes no response bodies, uses no cookie jar, follows no redirects, disables ambient proxy configuration/credentials, and checks the public HTTPS certificate chain and hostname with the platform trust store. Error output excludes raw exception messages and supplied URLs. It does not accept credentials in URL userinfo, query, or fragment. It does not open personal files, mint provider sessions, test logins, send tool actions, or generate billable voice calls. A network requiring an explicit proxy will need a separately reviewed transport change, not disabled TLS verification.

Exit codes:

- `0`: root returned exactly 200 and unauthenticated status returned exactly 401 at both origins; public TLS verification succeeded.
- `1`: at least one response/transport check failed. All remaining targets are still checked.
- `2`: missing/invalid public origin, unsafe origin, invalid timeout, or CLI usage error; no probes for invalid configuration.

JSON always declares `production_ready: false`: the script does not evaluate the release checklist. A 200 page could still be the wrong application, and a 401 alone does not prove owner isolation or that every protected endpoint is guarded. No claim about provider health, physical audio, caps, backups, or service recovery follows from exit 0.

## Test evidence and scope limits

The checker was developed through failing-test/implementation/pass cycles. Its dedicated stdlib suite passed 9 tests; existing frontend tests passed 52 tests. The live smoke check was repeated successfully after implementation.

The broader Python suite was run with `dotenv.load_dotenv` mocked off before app import and `JARVIS_DATA_DIR`/pytest temporary storage redirected to a disposable scratch directory, so it did not load production secrets or open the personal database. Concurrent native-voice work changed the suite during this audit: the latest run returned **129 passed, 1 failed, 36 subtests passed**. The remaining failure was `tests/test_realtime.py::test_broker_exchanges_server_only_tokens_and_configures_ga_audio`, receiving HTTP 405 instead of 200. An earlier run had neural-speech redaction/deadline failures that were absent in the subsequent run. Those files are owned by other workstreams and were not modified here. Re-run the full suite after integration; this audit does not certify the in-flight realtime implementation.

## Release evidence template

Before approval, fill in: release/artifact ID; stable origin; named service/security/privacy/budget owners; intended audience; provider/model/region and verified capability report; numeric quota/spend settings; duration/concurrency failure-test results; real-device audio evidence; auth/confirmation regression results; TLS and smoke report; restore drill; reboot/restart drill; retention settings; rollback artifact and trigger thresholds. Unknown or untested items remain blockers, not implied defaults.
