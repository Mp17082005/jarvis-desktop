def test_youtube_search_is_an_external_disclosure_requiring_approval(monkeypatch):
    monkeypatch.setenv('JARVIS_DESKTOP_ENABLED','true')
    monkeypatch.setenv('JARVIS_DESKTOP_LOCAL_ONLY','true')
    from companion.actions import action_risk
    assert action_risk('play_youtube') is True
    assert action_risk('pause') is False
    assert action_risk('open_app') is False
