"""Noise configuration contract tests; not physical microphone verification."""
import json

import pytest
from fastapi.testclient import TestClient

from test_realtime import app, connect, enabled, gateway, login


@pytest.mark.parametrize("threshold,silence", [("0.75", "900"), ("0.1", "200"), ("0.95", "2000"), ("0.5", "650")])
def test_noise_environment_reaches_new_provider_session(app, monkeypatch, threshold, silence):
    monkeypatch.setenv("JARVIS_VAD_THRESHOLD", threshold)
    monkeypatch.setenv("JARVIS_VAD_SILENCE_MS", silence)
    app = enabled(app, monkeypatch)
    requests = gateway(app)
    with TestClient(app) as client:
        login(client)
        response = connect(client)
        assert response.status_code == 200
        assert response.json()["max_seconds"] == 3300
    config = json.loads(requests[0].content)["session"]["audio"]["input"]
    assert config["turn_detection"] == {
        "type": "server_vad", "threshold": float(threshold), "silence_duration_ms": int(silence),
        "prefix_padding_ms": 300, "create_response": True, "interrupt_response": True,
    }
    assert config["noise_reduction"] == {"type": "near_field"}


@pytest.mark.parametrize("name,field,valid_name,valid_field,valid_value,default,bad", [
    ("JARVIS_VAD_THRESHOLD", "threshold", "JARVIS_VAD_SILENCE_MS", "silence_duration_ms", "900", 0.65, bad)
    for bad in ("", "not-a-number", "NaN", "inf", "-inf", "0.09", "0.951", "-1", "1e999")
] + [
    ("JARVIS_VAD_SILENCE_MS", "silence_duration_ms", "JARVIS_VAD_THRESHOLD", "threshold", "0.75", 800, bad)
    for bad in ("", "not-a-number", "NaN", "inf", "199", "2001", "-1", "800.5", "1e999")
])
def test_invalid_noise_setting_falls_back_independently(monkeypatch, name, field, valid_name, valid_field, valid_value, default, bad):
    from jarvis.realtime import session_config
    monkeypatch.setenv(name, bad)
    monkeypatch.setenv(valid_name, valid_value)
    vad = session_config("test", None)["audio"]["input"]["turn_detection"]
    assert vad[field] == default
    assert vad[valid_field] == float(valid_value)
    json.dumps(vad, allow_nan=False)
