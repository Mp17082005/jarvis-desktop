from pathlib import Path
from unittest.mock import patch
import os


def test_discovery_and_allowlisted_launch(tmp_path):
    from companion.actions import ActionEngine, discover_apps
    apps = discover_apps()
    assert set(apps) == {'chrome', 'edge', 'excel', 'notepad', 'calculator', 'explorer'}
    engine = ActionEngine(tmp_path)
    with patch('companion.actions.discover_apps', return_value={**apps, 'chrome': str(tmp_path / 'chrome.exe')}), patch('companion.actions.subprocess.Popen') as launch:
        launch.return_value.pid = 123
        result = engine.execute('open_app', {'app': 'chrome'})
        assert result['ok'] and result['pid'] == 123
        assert launch.call_args.kwargs['shell'] is False
        assert launch.call_args.args[0] == [str(tmp_path / 'chrome.exe')]
    with patch('companion.actions.discover_apps', return_value={**apps, 'excel': None}):
        assert engine.execute('open_app', {'app': 'excel'})['error'] == 'unavailable'



def test_workbook_roundtrip_atomic_and_exclusive(tmp_path):
    from companion.actions import ActionEngine
    engine = ActionEngine(tmp_path)
    args = {'path': 'demo.xlsx', 'sheet': 'Data', 'rows': [['Item', 'Value'], ['Test', -4]]}
    assert engine.execute('create_workbook', args)['ok']
    assert not engine.execute('create_workbook', args)['ok']
    result = engine.execute('read_range', {'path': 'demo.xlsx', 'sheet': 'Data', 'range': 'A1:B2'})
    assert result['rows'] == args['rows']
    assert engine.execute('write_range', {'path': 'demo.xlsx', 'sheet': 'Data', 'start_cell': 'B2', 'rows': [[9]]})['ok']
    assert engine.execute('read_range', {'path': 'demo.xlsx', 'sheet': 'Data', 'range': 'B2'})['rows'] == [[9]]
    assert not list(tmp_path.glob('*.tmp'))


def test_workbooks_reject_unsafe_paths_values_and_bounds(tmp_path):
    from companion.actions import ActionEngine
    engine = ActionEngine(tmp_path)
    base = {'path': 'safe.xlsx', 'sheet': 'Data', 'rows': [['safe']]}
    for path in ('../escape.xlsx', str(tmp_path.parent / 'escape.xlsx'), 'bad.xlsx:stream', 'CON.xlsx'):
        assert not engine.execute('create_workbook', {**base, 'path': path})['ok']
    for value in ('=1+1', '+cmd', '-cmd', '@SUM(A1)', ' \t=1+1', 'x' * 10001):
        assert not engine.execute('create_workbook', {**base, 'rows': [[value]]})['ok']
    assert not engine.execute('create_workbook', {**base, 'rows': [[1] * 1001]})['ok']
    assert engine.execute('create_workbook', base)['ok']
    os.link(tmp_path / 'safe.xlsx', tmp_path / 'linked.xlsx')
    assert not engine.execute('read_range', {'path': 'linked.xlsx', 'sheet': 'Data', 'range': 'A1'})['ok']
    (tmp_path / 'linked.xlsx').unlink()
    for region in ('A:A', 'A1:XFD1048576', 'A1:B1001', 'A0', 'B2:A1'):
        assert not engine.execute('read_range', {'path': 'safe.xlsx', 'sheet': 'Data', 'range': region})['ok']
    assert not engine.execute('write_range', {'path': 'safe.xlsx', 'sheet': 'Data', 'start_cell': 'XFD1048576', 'rows': [[1, 2]]})['ok']


def test_youtube_controls_verify_state_and_route(tmp_path):
    from companion.youtube import YouTubeController
    from companion.actions import ActionEngine, ACTIONS
    class VideoPage:
        url = 'https://www.youtube.com/watch?v=example'
        def __init__(self):
            self.paused = True
            self.time = 1.0
            self.volume = 0.5
            self.blocked = False
        def evaluate(self, script, arg=None):
            if 'pause()' in script:
                self.paused = True
            if 'play()' in script and not self.blocked:
                self.paused = False
            if 'v.volume =' in script:
                self.volume = arg
            if not self.paused:
                self.time += 0.5
            return {'paused': self.paused, 'currentTime': self.time, 'volume': self.volume, 'ended': False}
        def wait_for_timeout(self, value):
            pass
        def is_closed(self):
            return False
        def locator(self, selector):
            class Empty:
                def count(self): return 0
            return Empty()
    controller = YouTubeController(tmp_path / 'data' / 'desktop-browser')
    controller.page = VideoPage()
    assert controller._perform('resume', {})['ok']
    assert controller._perform('pause', {})['ok']
    assert controller._perform('set_volume', {'volume': 30})['volume'] == 30
    assert controller._perform('skip_ad', {})['error'] == 'skip_unavailable'
    controller.page.blocked = True
    assert controller._perform('resume', {})['error'] == 'playback_unverified'
    engine = ActionEngine(tmp_path)
    engine._youtube = controller
    assert not engine.execute('set_volume', {'volume': 101})['ok']
    assert not engine.execute('play_youtube', {'query': ''})['ok']
    assert engine.execute('pause', {})['ok']
    names = {s['function']['name'] for s in ACTIONS}
    assert {'play_youtube', 'pause', 'resume', 'set_volume', 'skip_ad', 'stop', 'create_workbook', 'read_range', 'write_range'} <= names
    controller.close()


def test_youtube_search_does_not_bypass_blockers(tmp_path):
    from companion.youtube import YouTubeController
    class Links:
        def count(self): return 1
        def nth(self, i): return self
        def is_visible(self): return True
        def get_attribute(self, key): return '/watch?v=abc_123'
        def inner_text(self, **kwargs): return 'Actual DOM title'
    class SearchPage:
        url = 'https://www.youtube.com/results?search_query=test'
        def is_closed(self): return False
        def goto(self, url, **kwargs): self.url = url
        def wait_for_timeout(self, value): pass
        def locator(self, selector):
            if 'video-title' in selector: return Links()
            class Nothing:
                def count(self): return 0
            return Nothing()
    controller = YouTubeController(tmp_path / 'profile')
    controller.page = SearchPage()
    found = controller.executor.submit(controller._search, 'test music').result()
    assert found['title'] == 'Actual DOM title'
    assert found['url'] == 'https://www.youtube.com/watch?v=abc_123'
    assert '/results?' in controller.page.url  # Search alone does not play
    controller.page.url = 'https://consent.youtube.com/'
    assert controller._blocked()
    controller.close()


def test_blank_write_clears_cell_and_symlink_refused(tmp_path):
    from companion.actions import ActionEngine
    engine = ActionEngine(tmp_path)
    assert engine.execute('create_workbook', {'path': 'clear.xlsx', 'sheet': 'Data', 'rows': [['old']]})['ok']
    assert engine.execute('write_range', {'path': 'clear.xlsx', 'sheet': 'Data', 'start_cell': 'A1', 'rows': [[None]]})['ok']
    assert engine.execute('read_range', {'path': 'clear.xlsx', 'sheet': 'Data', 'range': 'A1'})['rows'] == [[None]]
    import pytest
    try:
        (tmp_path / 'link.xlsx').symlink_to(tmp_path / 'clear.xlsx')
    except OSError:
        pytest.skip('OS account cannot create symlinks')
    assert not engine.execute('read_range', {'path': 'link.xlsx', 'sheet': 'Data', 'range': 'A1'})['ok']


def test_unknown_action_is_not_a_shell(tmp_path):
    from companion.actions import ActionEngine, ACTIONS, action_risk
    engine = ActionEngine(workspace=tmp_path)
    assert engine.execute('cmd.exe /c whoami', {})['ok'] is False
    assert engine.execute('open_app', {'app': 'cmd.exe'})['ok'] is False
    assert engine.execute('open_app', {'app': 'chrome', 'path': 'evil.exe'})['ok'] is False
    assert isinstance(ACTIONS, list)
    assert action_risk('create_workbook') is True
    assert action_risk('open_app') is False
    assert engine.capabilities()['scope'] == 'installed_machine'
