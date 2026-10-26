"""Dedicated visible browser; never uses the user's personal browser profile.

Automatic ad skipping only clicks YouTube's own visible, enabled Skip control as soon as it
appears (exactly what a user would do). Ads are never blocked, muted, sped up or stripped.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time


STATE = """() => { const v = document.querySelector('video'); return v ?
    {paused:v.paused,currentTime:v.currentTime,volume:v.volume,ended:v.ended} : null; }"""

SKIP_SELECTORS = ('button.ytp-ad-skip-button, .ytp-ad-skip-button-modern, .ytp-skip-ad-button, '
                  'button[id^="skip-button"]')
AD_STATE = """() => { const p = document.querySelector('#movie_player');
    return {player: !!p, ad_showing: !!(p && p.classList.contains('ad-showing'))}; }"""


class YouTubeController:
    def __init__(self, profile, *, headless=False, auto_skip=True, skip_interval=1.0):
        self.profile = Path(profile)
        self.headless = bool(headless)
        self.auto_skip = bool(auto_skip)
        self.skip_interval = max(0.2, float(skip_interval))
        self.page = None
        self.context = None
        self.playwright = None
        # Playwright's sync objects must remain on their creating thread.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='desktop-browser')
        # Auto-skip watcher: ticks run ON the executor; a Timer thread only re-submits them.
        self._watch_lock = threading.Lock()
        self._watch_generation = 0
        self._watch_timer = None
        self._closed = False
        self.ad_state = {'watching': False, 'ad_showing': False, 'auto_skips': 0, 'last_check': None, 'last_skip': None}

    # ---- automatic Skip-ad watcher -------------------------------------------------------
    def start_auto_skip(self):
        """Begin ~1 s polling for a visible, enabled Skip control on the open video page."""
        with self._watch_lock:
            if self._closed:
                return False
            self._watch_generation += 1
            generation = self._watch_generation
            self.ad_state['watching'] = True
        self._submit_tick(generation)
        return True

    def stop_auto_skip(self):
        with self._watch_lock:
            self._watch_generation += 1
            self.ad_state['watching'] = False
            timer, self._watch_timer = self._watch_timer, None
        if timer:
            timer.cancel()

    def _submit_tick(self, generation):
        with self._watch_lock:
            if self._closed or generation != self._watch_generation:
                return
            try:
                self.executor.submit(self._watch_tick, generation)
            except RuntimeError:  # executor shut down
                self.ad_state['watching'] = False

    def _schedule_tick(self, generation):
        with self._watch_lock:
            if self._closed or generation != self._watch_generation:
                return
            timer = threading.Timer(self.skip_interval, self._submit_tick, (generation,))
            timer.daemon = True
            self._watch_timer = timer
            timer.start()

    def _watch_tick(self, generation):
        """Runs on the Playwright executor thread only."""
        if generation != self._watch_generation or self._closed:
            return
        if self.page is None or self.page.is_closed():
            self.ad_state['watching'] = False
            return
        try:
            self._check_ad_once()
        except Exception:
            pass  # navigation in progress, page busy; try again next tick
        self._schedule_tick(generation)

    def _check_ad_once(self):
        page = self.page
        state = page.evaluate(AD_STATE)
        self.ad_state['ad_showing'] = bool(state and state.get('ad_showing'))
        self.ad_state['last_check'] = time.time()
        controls = page.locator(SKIP_SELECTORS)
        for i in range(min(controls.count(), 6)):
            button = controls.nth(i)
            if button.is_visible() and button.is_enabled():
                button.click(timeout=1500)  # a real, trusted user-style click on YouTube's control
                self.ad_state['auto_skips'] += 1
                self.ad_state['last_skip'] = time.time()
                return True
        return False

    def execute(self, name, args):
        return self.executor.submit(self._perform, name, args).result(timeout=60)

    def _ensure_page(self):
        if self.page is not None and not self.page.is_closed():
            return
        from playwright.sync_api import sync_playwright
        from .actions import discover_apps
        if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in (self.profile, *self.profile.parents)):
            raise ValueError('Browser profile cannot be linked')
        self.profile.mkdir(parents=True, exist_ok=True)
        if self.playwright is None:
            self.playwright = sync_playwright().start()
        options = {'user_data_dir': str(self.profile), 'headless': self.headless, 'timeout': 15000,
                   'ignore_default_args': ['--mute-audio'], 'args': ['--autoplay-policy=no-user-gesture-required']}
        if discover_apps().get('chrome'):
            options['channel'] = 'chrome'
        try:
            self.context = self.playwright.chromium.launch_persistent_context(**options)
        except Exception:
            if 'channel' not in options:
                raise
            options.pop('channel')
            self.context = self.playwright.chromium.launch_persistent_context(**options)
        self.context.set_default_timeout(4000)
        self.context.set_default_navigation_timeout(15000)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def _blocked(self):
        from urllib.parse import urlsplit
        if urlsplit(self.page.url).hostname not in ('www.youtube.com', 'youtube.com'):
            return True
        controls = self.page.locator('ytd-consent-bump-v2-lightbox, tp-yt-paper-dialog ytd-consent-bump-v2-lightbox, iframe[src*="recaptcha"], form[action*="consent"], #captcha-form, ytd-enforcement-message-view-model, yt-playability-error-supported-renderers')
        return any(controls.nth(i).is_visible() for i in range(min(controls.count(), 10)))

    def _block_reason(self):
        try:
            text = self.page.evaluate("() => { const e = document.querySelector('yt-playability-error-supported-renderers');"
                                      " return e ? e.innerText : ''; }") or ''
        except Exception:
            return 'unknown'
        if 'not a bot' in text or 'Sign in' in text:
            return 'youtube_sign_in_required'
        return 'playback_restricted' if text.strip() else 'consent_or_captcha'

    def _search(self, query):
        from urllib.parse import quote, urlsplit, parse_qs
        self._ensure_page()
        self.page.goto('https://www.youtube.com/results?search_query=' + quote(query, safe=''), wait_until='domcontentloaded', timeout=15000)
        self.page.wait_for_timeout(1500)
        if self._blocked():
            return {'ok': False, 'error': 'human_required', 'message': 'Consent, login, captcha or playback restriction needs the user. Nothing bypassed.'}
        links = self.page.locator('a#video-title[href^="/watch?"]')
        for i in range(min(links.count(), 30)):
            link = links.nth(i)
            if link.is_visible():
                href = link.get_attribute('href') or ''
                parsed = urlsplit(href)
                video_id = parse_qs(parsed.query).get('v', [''])[0]
                if parsed.path == '/watch' and not parsed.netloc and video_id:
                    return {'ok': True, 'title': link.inner_text(timeout=3000).strip()[:300], 'url': 'https://www.youtube.com/watch?v=' + quote(video_id, safe='')}
        return {'ok': False, 'error': 'no_video', 'message': 'No visible non-Short video result found.'}

    def _perform(self, name, args):
        if name == 'play_youtube':
            found = self._search(args['query'])
            if not found['ok']:
                return found
            self.page.goto(found['url'], wait_until='domcontentloaded', timeout=15000)
            self.page.wait_for_timeout(1500)
            if self._blocked():
                return {'ok': False, 'error': 'human_required', 'reason': self._block_reason(),
                        'message': 'User interaction or playback restriction; nothing bypassed.'}
            if self.auto_skip:
                # An ad may already be showing; give the watcher a first look immediately.
                try:
                    self._check_ad_once()
                except Exception:
                    pass
                self.start_auto_skip()
            result = self._perform('resume', {})
            return {**result, 'title': found['title'], 'url': found['url'], 'auto_skip': self.auto_skip}
        if name == 'state':
            if self.page is None or self.page.is_closed():
                return {'ok': False, 'error': 'no_active_video', **self.ad_state}
            try:
                self._check_ad_once() if self.ad_state['watching'] else None
            except Exception:
                pass
            video = self.page.evaluate(STATE)
            return {'ok': True, 'video': video, 'title': self.page.title()[:300], **self.ad_state}
        if self.page is None or self.page.is_closed():
            return {'ok': False, 'error': 'no_active_video', 'message': 'No companion video is open.'}
        page = self.page
        if name in ('pause', 'stop'):
            page.evaluate("() => { const v = document.querySelector('video'); if(v) v.pause(); }")
            state = page.evaluate(STATE)
            return {'ok': bool(state and state['paused']), 'status': 'paused' if state and state['paused'] else 'pause_unverified'}
        if name == 'resume':
            before = page.evaluate(STATE)
            page.evaluate("() => { const v = document.querySelector('video'); if(v) v.play().catch(()=>{}); }")
            page.wait_for_timeout(1200)
            after = page.evaluate(STATE)
            if before and after and not after['paused'] and not after['ended'] and after['currentTime'] > before['currentTime']:
                return {'ok': True, 'status': 'playing', 'playback_verified': True}
            return {'ok': False, 'error': 'playback_unverified', 'message': 'Playback did not advance; autoplay, login or media availability may block it.'}
        if name == 'set_volume':
            page.evaluate("level => { const v = document.querySelector('video'); if(v) v.volume = level; }", args['volume'] / 100)
            state = page.evaluate(STATE)
            return {'ok': bool(state and abs(state['volume'] * 100 - args['volume']) < 0.01), 'volume': state['volume'] * 100 if state else None, 'scope': 'companion_youtube_player'}
        if name == 'skip_ad':
            controls = page.locator(SKIP_SELECTORS)
            for i in range(min(controls.count(), 10)):
                button = controls.nth(i)
                if button.is_visible() and button.is_enabled():
                    button.click(timeout=3000)
                    page.wait_for_timeout(400)
                    skipped = not button.is_visible()
                    return {'ok': skipped, 'status': 'skip_control_clicked', 'skip_verified': skipped}
            return {'ok': False, 'error': 'skip_unavailable', 'message': 'No visible, enabled Skip ad control. Ads are not bypassed.'}
        return {'ok': False, 'error': 'unsupported_action'}

    def _close(self):
        self._closed = True
        if self.context:
            self.context.close()
        if self.playwright:
            self.playwright.stop()
        self.page = self.context = self.playwright = None

    def close(self):
        self.stop_auto_skip()
        with self._watch_lock:
            self._closed = True
        self.executor.submit(self._close).result(timeout=15)
        self.executor.shutdown(wait=True)
