JARVIS FOR WINDOWS - READ THIS FIRST

Jarvis is a voice assistant for your PC. You talk, it answers and does tasks.
This download is source code plus an installer, not a signed .exe. It needs no
administrator rights and installs only into your own user account.

INSTALL IN 5 STEPS  (with the .exe from the release page: just run it, then step 3)

1. Extract
   Right-click Jarvis-Setup.zip and choose Extract All. Open the extracted
   Jarvis-Setup folder. Do not run anything from inside the ZIP.
   Then double-click Install-Jarvis.cmd in that folder (next step).

2. Double-click Install-Jarvis.cmd
   It downloads its own copy of uv (the Python installer tool) and checks its SHA-256.
   Keep the window open. The first install downloads Python packages, Microsoft
   UFO (desktop control) and the offline wake-word model: 5-15 minutes and
   about 2 GB of disk space. If Windows SmartScreen asks, choose More info > Run anyway
   only if you trust where you got this ZIP.

3. Paste your API key and click Connect
   In the window that opens, paste your API key. Jarvis recognises the provider
   from the key (OpenAI, Claude, OpenRouter, Groq, Gemini, xAI) and fills in the
   address and model. For Azure or another gateway, also paste the provider URL;
   for local Ollama choose it under Provider (no key needed). Click Connect: it
   runs a short test and saves. The key is stored in Windows Credential Manager,
   never in a text file.

4. Talk to Jarvis
   Start Jarvis from the Desktop or Start menu (Jarvis). Then say "Jarvis",
   or press Ctrl+Alt+J anywhere, or click the Jarvis tray icon (Talk now),
   and ask something, for example "what's the weather?"

5. Done
   Keep talking without repeating "Jarvis". Say "that's all" to let it rest.

REQUIREMENTS
   - Windows 10 or 11 (64-bit)
   - A microphone and speakers (or a headset)
   - Internet access for installation (and for chat with your AI provider)
   - An OpenAI-compatible API key (OpenAI or any compatible provider/gateway)

MORE HELP
   docs\INSTALL.md explains options, safety levels, privacy and troubleshooting.
   To remove Jarvis completely, double-click Uninstall-Jarvis.cmd.
