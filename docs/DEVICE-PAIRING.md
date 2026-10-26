# One-link Windows device connection

## Scope

The public setup page discovers a running companion at `http://127.0.0.1:8767` **on the device running the browser**. It never scans a network or silently falls back to executing tasks on the cloud VM.

The connection is a local handoff, not remote desktop access: after explicit pairing, the setup page opens that device's local Jarvis app. Desktop actions remain loopback-only and locally authenticated. The public site never receives local login cookies, API keys, or desktop action credentials.

## First-time setup

1. Open `/connect.html` on the public Jarvis origin.
2. Download the source ZIP if the companion is not installed. In File Explorer, choose **Extract All** first, open the extracted `jarvis-companion` folder, and read `README-FIRST.txt`. **Double-click `Install-Jarvis.cmd`**; no PowerShell commands are needed. The console stays open to show the result. If uv is missing, follow its [official installation instructions](https://docs.astral.sh/uv/getting-started/installation/) and retry; this installer does not run a remote uv bootstrap script. Configure your own `.env` in `%LOCALAPPDATA%\JarvisCompanion` using the supplied `.env.example`, then **double-click `Start-Jarvis.cmd`**. File Explorer can open that installation path directly. Keep the companion console open. You can choose the browser option instead if local setup is not suitable; browser voice does not provide local desktop control.
3. Allow the browser's local-network permission if prompted. Failure to discover can also mean the companion is stopped or the browser blocks access; it does not prove installation is missing.
4. Open **Pair this device**. In the local page, sign in to that installation and approve the exact public origin displayed.
5. Return to the public setup page and connect. It opens the local Jarvis app on this device.

Initial installation, configuration, pairing and microphone/camera permissions are explicit. Pairing does not install Microsoft Office or grant unrestricted operating-system access. This source installer still requires uv/Python dependencies and provider configuration; it is not a zero-configuration signed executable.

## Later visits

The same public setup link can recognize the locally approved origin and open that machine's local Jarvis app. Keep the companion running. The configured local port is 8767; custom ports require a separately reviewed discovery setting rather than arbitrary network probing.

A Quick Tunnel hostname changes when restarted. A new hostname is a new origin and requires pairing again. A stable owned hostname is needed for a durable one-link production experience.

## Security

- Cross-origin discovery exposes only minimal installation/pair status; device name is revealed only to an already paired origin.
- Pair approval/revocation requires local authenticated same-origin access. A remote page cannot pair itself.
- Pairing grants discovery and navigation only. It never creates cross-origin action access or passes long-lived credentials to the public page.
- Revoking an origin stops it being recognized as paired. Existing local login/session expiry remains a separate control.
- Cloud and local apps are distinct origins and sessions; a browser cannot use the cloud session as local authentication.

## Platforms and limitations

Desktop control currently targets Windows with an installed companion. macOS/Linux require their own reviewed adapters, and mobile browsers cannot obtain whole-device app control from this link. Browser permission policy can require a click before localhost detection or opening the local app. Do not bypass that policy.
