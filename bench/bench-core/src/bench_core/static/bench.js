/* Shared bench-UI helpers.
 *
 * Served by every tool at /shared/bench.js (mounted from this package by
 * bench_core.bench_ui and magos_bench). Each tool page used to carry its own
 * copy of these; centralising them keeps the XSS-safe escaping, resilient
 * WebSocket handling and error reporting consistent across all the tools. */

function $(id) { return document.getElementById(id); }

// Escape device-/user-supplied strings before injecting them via innerHTML.
function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtDur(sec) { return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0'); }

function slug(s) { return (s || '').trim().toLowerCase().replace(/[^a-z0-9-]/g, '-').replace(/^-+|-+$/g, ''); }

// POST JSON with robust error handling. Returns the parsed body, or an object
// with an `error` string on any failure. `opts.buttons` (ids or elements) are
// always re-enabled in `finally`, so a network/server error never leaves the
// operator staring at a permanently-disabled control. Set
// `opts.alertOnError = false` to handle the error yourself instead of alert().
async function postJSON(url, body, opts) {
  opts = opts || {};
  const buttons = opts.buttons || [];
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await r.json(); } catch (_e) { data = {}; }
    if (!r.ok && !data.error) data = { error: `Request failed (HTTP ${r.status}).` };
    if (data.error && opts.alertOnError !== false) alert(data.error);
    return data;
  } catch (e) {
    const data = { error: 'Network error — is the tool still running? ' + ((e && e.message) || '') };
    if (opts.alertOnError !== false) alert(data.error);
    return data;
  } finally {
    buttons.forEach(b => {
      const el = (typeof b === 'string') ? $(b) : b;
      if (el) el.disabled = false;
    });
  }
}

// Resilient WebSocket state feed.
//
// Calls `onState(state)` for every frame. Also bootstraps once via GET
// /api/state and again after each reconnect, so the page is never stuck on its
// static HTML defaults while the socket is (re)connecting. The #conn indicator
// reflects *socket* health — `up` (live) vs `down` (reconnecting) — independent
// of whether a device is currently detected. Reconnect uses a capped backoff
// and a single in-flight socket, and is torn down on page unload.
function connectBenchWS(onState, opts) {
  opts = opts || {};
  const connEl = $('conn');
  let ws = null, retry = 0, closed = false, bootstrapping = false;

  function setConn(text, up) {
    if (!connEl) return;
    connEl.textContent = text;
    connEl.classList.toggle('up', !!up);
    connEl.classList.toggle('down', !up);
  }

  async function bootstrap() {
    if (bootstrapping) return;
    bootstrapping = true;
    try {
      const r = await fetch('/api/state');
      if (r.ok) onState(await r.json());
    } catch (_e) { /* the socket frames will catch up shortly */ }
    finally { bootstrapping = false; }
  }

  function open() {
    if (closed) return;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws/state`);
    ws.onopen = () => { retry = 0; setConn('live', true); bootstrap(); };
    ws.onmessage = (e) => {
      let s;
      try { s = JSON.parse(e.data); } catch (_err) { return; }
      try { onState(s); } catch (err) { console.error('render error', err); }
    };
    ws.onerror = () => { try { ws.close(); } catch (_e) { /* onclose handles retry */ } };
    ws.onclose = () => {
      if (closed) return;
      setConn('reconnecting…', false);
      retry = Math.min(retry + 1, 6);
      setTimeout(open, Math.min(1500 * retry, 8000));
    };
  }

  window.addEventListener('beforeunload', () => {
    closed = true;
    if (ws) { try { ws.close(); } catch (_e) { /* unloading anyway */ } }
  });

  bootstrap();   // paint immediately, don't wait for the first socket frame
  open();
  return { close() { closed = true; if (ws) { try { ws.close(); } catch (_e) {} } } };
}

// Shared "live progress" log panel (Teltonika-style tools). Shows the live
// step log while configuring and keeps it pinned to the bottom unless the
// operator has scrolled up. No-op if the page has no #liveCard/#liveLog.
function renderLiveLog(s) {
  const lc = $('liveCard'), ll = $('liveLog');
  if (!lc || !ll) return;
  if (s.phase === 'configuring' && (s.live_steps || []).length) {
    lc.style.display = '';
    const txt = s.live_steps.map(t => `[${t.time}] [${t.sn}] ${t.msg}`).join('\n');
    if (ll.textContent !== txt) {
      const atBottom = ll.scrollHeight - ll.scrollTop - ll.clientHeight < 40;
      ll.textContent = txt;
      if (atBottom) ll.scrollTop = ll.scrollHeight;
    }
  } else {
    lc.style.display = 'none';
  }
}

// Render a verification table ([{item, ok, actual}]) into <table id=tableId>
// with body <tbody id=bodyId>. All device-supplied text is escaped.
function renderVerifyTable(verification, tableId, bodyId) {
  const vt = $(tableId), vb = $(bodyId);
  if (!vt || !vb) return;
  if (verification && verification.length) {
    const mark = (ok) => ok === true ? '<span class="badge badge-ok">PASS</span>'
      : ok === false ? '<span class="badge badge-err">FAIL</span>'
        : '<span class="badge badge-pend">skip</span>';
    vb.innerHTML = verification.map(c =>
      `<tr><td>${mark(c.ok)} ${esc(c.item)}</td><td class="mono">${esc(c.actual)}</td></tr>`).join('');
    vt.classList.remove('hidden');
  } else {
    vt.classList.add('hidden');
  }
}
