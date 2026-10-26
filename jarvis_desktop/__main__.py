"""python -m jarvis_desktop [--setup [--non-interactive ...]] [--doctor] [--text "..."]"""
from __future__ import annotations

import argparse
import sys


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="jarvis", description="Jarvis Desktop voice assistant")
    p.add_argument("--setup", action="store_true", help="Open the setup window (first run happens automatically)")
    p.add_argument("--non-interactive", action="store_true",
                   help="With --setup: save settings without a window; API key read from JARVIS_LLM_API_KEY")
    p.add_argument("--provider", help="openai, anthropic, azure, gemini, openrouter, groq, ollama, litellm, ... (default: detect)")
    p.add_argument("--base-url")
    p.add_argument("--model")
    p.add_argument("--autonomy", choices=("careful", "balanced", "autonomous"))
    p.add_argument("--tts-model")
    p.add_argument("--tts-voice")
    p.add_argument("--stt-mode", choices=("local", "provider"))
    p.add_argument("--language")
    p.add_argument("--test", action="store_true", help="With --non-interactive: test the connection before saving")
    p.add_argument("--doctor", action="store_true", help="Check microphone, wake model, key presence, provider")
    p.add_argument("--text", help="Run one utterance through the agent, print and speak the reply")
    p.add_argument("--no-speak", action="store_true", help="With --text: print only")
    p.add_argument("--offline-tts", action="store_true", help="Use offline Windows voice only")
    p.add_argument("--no-tray", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    from . import config

    if args.setup and args.non_interactive:
        from .setup_wizard import non_interactive
        return non_interactive(args)
    if args.doctor:
        from .app import doctor_report, print_json
        print_json(doctor_report())
        return 0
    if args.setup or not config.is_configured():
        if args.text and config.get_api_key() and not args.setup:
            pass  # headless: env key + default settings is enough
        else:
            from .setup_wizard import run_wizard
            if not run_wizard() and not config.is_configured():
                print("Setup was not completed.")
                return 1
            if args.setup:
                return 0
    settings = config.load_settings()
    if args.text is not None:
        from .agent import Agent
        from .app import agent_config, run_text
        from .tts import Speaker

        class _Mute:
            def say(self, text, block=True):
                return False

        speaker = _Mute() if args.no_speak else Speaker(settings, config.get_api_key, offline_only=args.offline_tts)
        agent = Agent(agent_config(settings, config.get_api_key()), speak_async=lambda t: None,
                      confirm=lambda q: False)
        try:
            run_text(agent, speaker, args.text)
        finally:
            agent.close()
        return 0
    from .app import build_and_run
    return build_and_run(settings, use_tray=not args.no_tray, offline_tts=args.offline_tts)


if __name__ == "__main__":
    sys.exit(main())
