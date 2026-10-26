"""Speech follows the provider type: Claude/Gemini keys have no TTS endpoint; Azure uses api-key."""
import types

import httpx

from jarvis_desktop.tts import Speaker


class Recorder:
    def __init__(self):
        self.played = []

    def play(self, audio, block=True):
        self.played.append(audio)
        return True

    def interrupt(self):
        pass


def settings(**kw):
    base = dict(provider="", base_url="https://api.openai.com/v1", tts_model="tts-1", tts_voice="alloy", language="en")
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_claude_key_never_calls_a_speech_endpoint(monkeypatch):
    calls = []
    monkeypatch.setattr("jarvis_desktop.tts.sapi_speech", lambda text, language="": b"RIFFoffline")
    sp = Speaker(settings(provider="anthropic", base_url="https://api.anthropic.com", tts_model="tts-1"), lambda: "sk-ant-x",
                 player=Recorder(), transport=httpx.MockTransport(lambda r: (calls.append(str(r.url)), httpx.Response(404))[1]))
    assert sp.say("Hello there.") is True
    assert calls == [] and sp.last_engine == "offline"


def test_azure_speech_uses_api_key_header(monkeypatch):
    seen = {}

    def handle(r):
        seen.update(url=str(r.url), key=r.headers.get("api-key"))
        return httpx.Response(200, content=b"ID3audio", headers={"content-type": "audio/mpeg"})
    sp = Speaker(settings(provider="azure", base_url="https://res.openai.azure.com/openai/v1", tts_model="tts"), lambda: "az",
                 player=Recorder(), transport=httpx.MockTransport(handle))
    assert sp.say("Hi.") is True
    assert seen == {"url": "https://res.openai.azure.com/openai/v1/audio/speech", "key": "az"}
    assert sp.last_engine == "provider"
