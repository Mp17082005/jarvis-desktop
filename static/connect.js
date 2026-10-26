(function (root) {
  'use strict';
  const LOCAL = 'http://127.0.0.1:8767/';
  function safeOrigin(value) {
    if (typeof value !== 'string') return null;
    try {
      const url = new URL(value);
      if (url.origin !== value || url.username || url.password) return null;
      if (url.protocol === 'https:' || (url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))) return value;
    } catch (_) { /* Invalid input is never a navigation target. */ }
    return null;
  }
  function mountBrowser(document, window) {
    const el = id => document.getElementById(id);
    el('download-companion')?.addEventListener('click', event => {if (event.detail > 1) event.preventDefault();});
    const origin = safeOrigin(window.location.origin);
    let busy = false;
    el('site-origin').textContent = origin || 'Unsupported site origin';
    el('pair-device').hidden = true;
    el('open-device').hidden = true;
    el('manual-pair').hidden = !origin;
    if (origin) {
      el('manual-pair').href = LOCAL + 'pair-device.html#origin=' + encodeURIComponent(origin);
      el('manual-pair').rel = 'noopener noreferrer';
    }
    if (!origin) {
      el('connect-device').disabled = true;
      el('detection-status').textContent = 'Open setup from an HTTPS site (or local HTTP development origin) to detect this companion.';
      return { ready: Promise.resolve() };
    }
    async function probe(navigate = false) {
      if (busy || !origin) return;
      busy = true; el('connect-device').disabled = true;
      el('pair-device').hidden = true;
      el('open-device').hidden = true;
      el('detection-status').textContent = 'Checking the companion on this device…';
      const controller = new window.AbortController();
      const timer = window.setTimeout(() => controller.abort(), 3000);
      try {
        const response = await window.fetch(LOCAL + 'companion/discovery?origin=' + encodeURIComponent(origin), {
          method: 'GET', credentials: 'omit', cache: 'no-store', redirect: 'error', signal: controller.signal
        });
        if (!response.ok) throw new Error('Detection failed');
        const info = await response.json();
        if (!info || info.installed !== true || typeof info.paired !== 'boolean' ||
            typeof info.version !== 'string' || !info.version || typeof info.device_name !== 'string' ||
            !info.device_name || info.device_name.length > 256 || info.target !== LOCAL) throw new Error('Invalid discovery');
        if (!info.paired) {
          el('detection-status').textContent = 'Companion found on ' + info.device_name + '. Review and approve this site locally before connecting.';
          el('pair-device').href = LOCAL + 'pair-device.html#origin=' + encodeURIComponent(origin);
          el('pair-device').rel = 'noopener noreferrer';
          el('pair-device').hidden = false;
          return;
        }
        el('detection-status').textContent = 'Paired with ' + info.device_name + '. Controls will run on this Windows device after you open local Jarvis.';
        el('open-device').textContent = 'Open Jarvis on ' + info.device_name;
        el('open-device').href = LOCAL;
        el('open-device').hidden = false;
        if (navigate) window.location.assign(LOCAL);
      } catch (_) {
        el('detection-status').textContent = 'Companion not reachable or browser blocked detection. Start the companion, allow local network access if prompted, then retry. No cloud or VM actions were started.';
      } finally {
        window.clearTimeout(timer); busy = false; el('connect-device').disabled = false;
      }
    }
    el('connect-device').addEventListener('click', event => {if (event.detail > 1) return; return probe(true);});
    return { ready: probe() };
  }
  const api = { LOCAL, safeOrigin, mountBrowser };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else {
    root.JarvisConnect = api;
    if (root.document.getElementById('connect-device')) mountBrowser(root.document, root);
  }
})(typeof window === 'object' ? window : globalThis);
