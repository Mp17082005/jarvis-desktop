from companion.actions import ActionEngine


def test_engine_shutdown_releases_browser_once(tmp_path):
    engine=ActionEngine(tmp_path)
    calls=[]
    class Browser:
        def close(self):calls.append('closed')
    engine._youtube=Browser()
    assert callable(getattr(engine,'close',None)), 'Engine needs lifecycle cleanup'
    engine.close();engine.close()
    assert calls==['closed']


def test_engine_shutdown_without_browser_is_safe(tmp_path):
    engine=ActionEngine(tmp_path)
    assert callable(getattr(engine,'close',None))
    engine.close()
