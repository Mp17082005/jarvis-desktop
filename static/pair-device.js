(function (root) {
  'use strict';
  const shared = typeof module === 'object' && module.exports ? require('./connect.js') : root.JarvisConnect;
  function mountBrowser(document, window) {
    const el = id => document.getElementById(id);
    const local = window.location.origin === shared.LOCAL.slice(0, -1);
    const params = new URLSearchParams(window.location.hash.slice(1));
    const origin = params.size === 1 ? shared.safeOrigin(params.get('origin')) : null;
    let authenticated = false, busy = false, origins = [];
    el('local-open').hidden = true; el('local-open').href = shared.LOCAL;
    el('return-site').hidden = !origin;
    if (origin) { el('return-site').href = origin + '/connect.html'; el('return-site').rel = 'noopener noreferrer'; }
    el('approve-site').disabled = true;
    el('local-token').disabled = !local; el('login-submit').disabled = !local;
    el('requested-origin').textContent = origin || 'No valid site origin supplied.';
    if (!local) {
      el('pair-status').textContent = 'Open this approval page on the local companion at http://127.0.0.1:8767. Never enter your local password on a public site.';
      return { ready: Promise.resolve() };
    }
    async function request(url, body) {
      const response = await window.fetch(url, { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error',
        headers: body === undefined ? {} : { 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) {
          authenticated = false; el('local-login').hidden = false; el('approve-site').disabled = true;
        }
        throw new Error('Local request failed. Sign in locally or retry; no permission change has been verified.');
      }
      return response.json();
    }
    function render() {
      el('approve-site').disabled = busy || !authenticated || !origin || origins.includes(origin);
      el('approved-origins').replaceChildren();
      for (const site of origins) {
        const row = document.createElement('li'), label = document.createElement('span'), button = document.createElement('button');
        label.textContent = site; button.textContent = 'Revoke'; button.type = 'button';
        button.setAttribute('aria-label', 'Revoke ' + site);
        button.title = 'Revoke pairing for ' + site + '. Local conversations are not stopped.';
        button.disabled = busy || !authenticated;
        button.addEventListener('click', () => mutate('revoke', site));
        row.append(label, button); el('approved-origins').append(row);
      }
      el('no-origins').hidden = origins.length !== 0;
    }
    async function refresh() {
      const info = await request('/api/pairing');
      if (!info || !Array.isArray(info.origins) || info.origins.some(site => !shared.safeOrigin(site)) || typeof info.device_name !== 'string' || !info.device_name || info.device_name.length > 256) {
        authenticated = false; render(); throw new Error('Local pairing response failed validation. No permission change has been verified.');
      }
      origins = [...new Set(info.origins)];
      authenticated = true; el('local-login').hidden = true;
      el('local-device').textContent = info.device_name;
      render();
      el('pair-status').textContent = origin ? 'Signed in locally. Review the exact site before approving.' : 'Invalid or missing site origin. Approval disabled; you can still revoke existing sites.';
      return info;
    }
    async function mutate(action, site) {
      if (!authenticated || busy || !shared.safeOrigin(site) || (action === 'approve' && (!origin || origins.includes(origin)))) return;
      busy = true; el('local-open').hidden = true; render();
      el('pair-status').textContent = 'Saving locally and verifying…';
      try {
        await request('/api/pairing/' + action, { origin: site });
        await refresh();
        if (origins.includes(site) !== (action === 'approve')) throw new Error('Permission change not verified. Refresh the list before trying again.');
        el('pair-status').textContent = action === 'approve' ? 'Approval verified. Open local Jarvis when you are ready.' : 'Revocation verified. This site can no longer discover a paired device; local controls stay local.';
        el('local-open').hidden = false;
      } catch (_) {
        authenticated = false;
        el('pair-status').textContent = 'Permission change failed or was not verified. Refresh the list before trying again; nothing is retried automatically.';
      } finally { busy = false; render(); }
    }
    el('approve-site').addEventListener('click', event => {if (event.detail > 1) return; return mutate('approve', origin);});
    el('refresh-pairing').addEventListener('click', async () => {
      if (busy) return;
      busy = true; render();
      try { await refresh(); } catch (_) { authenticated = false; el('pair-status').textContent = 'Local pairing refresh failed. Sign in locally or retry.'; }
      finally { busy = false; render(); }
    });
    el('local-login').addEventListener('submit', async event => {
      event.preventDefault();
      if (el('login-submit').disabled) return;
      const token = el('local-token').value; el('local-token').value = '';
      el('login-submit').disabled = true;
      try { await request('/api/login', { token }); await refresh(); }
      catch (error) { el('pair-status').textContent = error.message; }
      finally { el('local-token').value = ''; el('login-submit').disabled = false; }
    });
    return { ready: refresh().catch(error => { el('pair-status').textContent = error.message; }) };
  }
  if (typeof module === 'object' && module.exports) module.exports = { mountBrowser };
  else mountBrowser(root.document, root);
})(typeof window === 'object' ? window : globalThis);
