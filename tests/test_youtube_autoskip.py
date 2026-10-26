"""YouTube automatic Skip-ad watcher against a local fixture page with real headless Chromium."""
import http.server
import threading
import time

import pytest

pytest.importorskip('playwright.sync_api')

FIXTURE = """<!doctype html><html><head><title>Fixture video</title></head><body>
<div id="movie_player" class="html5-video-player ad-showing">
  <video id="v" muted></video>
  <div class="ytp-ad-player-overlay"><button id="skip-button:5" class="ytp-skip-ad-button" style="display:none">Skip</button></div>
</div>
<script>
  window.skipClicks = 0;
  const btn = document.querySelector('.ytp-skip-ad-button');
  setTimeout(() => { btn.style.display = 'inline-block'; window.shownAt = performance.now(); }, 2000);
  btn.addEventListener('click', (e) => {
    window.skipClicks += 1; window.trusted = e.isTrusted; window.clickedAt = performance.now();
    btn.style.display = 'none';
    document.querySelector('#movie_player').classList.remove('ad-showing');
  });
</script></body></html>"""


@pytest.fixture(scope='module')
def server():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            data = FIXTURE.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{srv.server_address[1]}/watch'
    srv.shutdown()


@pytest.fixture
def controller(tmp_path):
    from companion.youtube import YouTubeController
    yt = YouTubeController(tmp_path / 'profile', headless=True, skip_interval=1.0)

    def launch():
        from playwright.sync_api import sync_playwright
        yt.playwright = sync_playwright().start()
        yt.context = yt.playwright.chromium.launch_persistent_context(str(yt.profile), headless=True)
        yt.page = yt.context.new_page()
    yt.executor.submit(launch).result(60)
    yield yt
    yt.close()


def _goto(yt, url):
    yt.executor.submit(lambda: yt.page.goto(url, wait_until='domcontentloaded')).result(30)


def _eval(yt, expr):
    return yt.executor.submit(lambda: yt.page.evaluate(expr)).result(30)


def test_skip_button_auto_clicked_within_three_seconds_of_appearing(controller, server):
    _goto(controller, server)
    assert controller.start_auto_skip()
    time.sleep(1.2)
    assert controller.ad_state['ad_showing'] is True  # ad state reported while the ad shows
    assert _eval(controller, 'window.skipClicks') == 0
    deadline = time.monotonic() + 2.0 + 3.0  # button appears at ~2 s; must be clicked within 3 s
    while time.monotonic() < deadline and _eval(controller, 'window.skipClicks') == 0:
        time.sleep(0.1)
    assert _eval(controller, 'window.skipClicks') == 1
    assert _eval(controller, 'window.trusted') is True  # a real user-style click, not a script hack
    assert _eval(controller, 'window.clickedAt - window.shownAt') < 3000
    assert controller.ad_state['auto_skips'] == 1
    time.sleep(1.3)
    assert controller.ad_state['ad_showing'] is False
    # The watcher runs on the Playwright executor thread, never its own thread.
    assert controller.ad_state['watching'] is True


def test_watcher_stops_and_manual_skip_still_available(controller, server):
    _goto(controller, server)
    controller.start_auto_skip()
    controller.stop_auto_skip()
    time.sleep(3.0)
    assert _eval(controller, 'window.skipClicks') == 0  # no auto click after stop
    result = controller.execute('skip_ad', {})
    assert result['ok'] is True and result['skip_verified'] is True
    assert _eval(controller, 'window.skipClicks') == 1


def test_no_skip_when_button_hidden_or_disabled(controller, server):
    _goto(controller, server)
    _eval(controller, "() => { const b = document.querySelector('.ytp-skip-ad-button'); b.disabled = true; }")
    controller.start_auto_skip()
    time.sleep(3.5)
    assert _eval(controller, 'window.skipClicks') == 0
    assert controller.ad_state['auto_skips'] == 0
