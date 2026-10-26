import pytest
from fastapi import HTTPException
from jarvis.realtime import Sessions


def test_normal_reconnects_allowed_but_hourly_cap_has_retry_time(monkeypatch):
    monkeypatch.setenv('JARVIS_REALTIME_STARTS_PER_HOUR','30')
    monkeypatch.setattr('jarvis.realtime.time.monotonic',lambda:1000.0)
    sessions=Sessions()
    for _ in range(30):
        item=sessions.reserve('owner',None)
        sessions.sessions.pop(item.id)
    with pytest.raises(HTTPException) as exc:
        sessions.reserve('owner',None)
    assert exc.value.status_code==429
    assert exc.value.headers['Retry-After']=='3600'
    assert 'Try again in 60 minutes' in exc.value.detail


def test_start_limit_is_configurable(monkeypatch):
    monkeypatch.setenv('JARVIS_REALTIME_STARTS_PER_HOUR','2')
    sessions=Sessions()
    for _ in range(2):
        item=sessions.reserve('owner',None);sessions.sessions.pop(item.id)
    with pytest.raises(HTTPException):sessions.reserve('owner',None)
