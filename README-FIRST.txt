JARVIS FOR WINDOWS - READ THIS FIRST

Jarvis is a voice assistant for your PC. You talk, it answers and does tasks.
This download is source code plus an installer, not a signed .exe. It needs no
administrator rights and installs only into your own user account.

INSTALL IN 5 STEPS

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

3. Enter your API key in the window that opens
   Paste your AI provider's address (for OpenAI: https://api.openai.com/v1),
   your API key and a model name, choose how careful Jarvis should be, and click Save.
   The key is stored in Windows Credential Manager, never in a text file.

4. Say "Jarvis"
   Start Jarvis from the Start menu (Jarvis), then say
   "Jarvis" and ask something, for example "Jarvis, what's the weather?"

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
