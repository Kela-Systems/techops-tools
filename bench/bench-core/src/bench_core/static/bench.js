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

// Station-level operator box (TEC-345). Injected next to the #conn indicator
// so every tool gets it without per-page markup. The name is entered once at
// day start (typed, or a badge scanner acting as a keyboard) and is stamped
// into every run record by the server; setting it in any tool covers all of
// them (one shared station file). Returns {update(state)}.
function mountOperatorBox() {
  const conn = $('conn');
  if (!conn || $('operatorInput')) return { update() {} };

  // Group the box with the connection indicator on the header's right side.
  const wrap = document.createElement('div');
  wrap.style.cssText = 'display:flex;align-items:center;gap:14px;';
  conn.parentNode.insertBefore(wrap, conn);

  const box = document.createElement('div');
  box.className = 'operator-box';
  box.innerHTML = '<label for="operatorInput">Operator</label>' +
    '<input id="operatorInput" type="text" autocomplete="off" maxlength="64"' +
    ' placeholder="scan badge or type name">' +
    '<button id="operatorSet" class="btn-outline" type="button">Set</button>';
  wrap.appendChild(box);
  wrap.appendChild(conn);

  const input = box.querySelector('#operatorInput');
  const btn = box.querySelector('#operatorSet');
  let dirty = false, current = '';

  async function save() {
    btn.disabled = true;
    const r = await postJSON('/api/operator', { operator: input.value }, { buttons: [btn] });
    if (!r.error) { dirty = false; input.blur(); }
  }
  input.addEventListener('input', () => { dirty = true; });
  // A badge scanner "types" the ID and sends Enter — same path as a human.
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); });
  btn.addEventListener('click', save);

  return {
    update(s) {
      current = s.operator || '';
      if (s.station_id) box.title = 'Station ' + s.station_id +
        ' — the operator name is recorded into every run from all bench tools.';
      // Don't clobber a name mid-typing; otherwise mirror the station value
      // (it can change from another tool's page).
      if (document.activeElement !== input && !dirty) input.value = current;
      if (input.value === current) dirty = false;
      box.classList.toggle('missing', !current);
    },
  };
}

// Config self-check banner (TEC-356). Injected under the page header by
// connectBenchWS, so every tool surfaces its startup config warnings
// (placeholder values, expired tokens, missing fields) without per-page
// markup. Hidden while the server reports no warnings. Returns {update(state)}.
function mountConfigWarn() {
  let box = null;

  function ensure() {
    if (box) return box;
    box = document.createElement('div');
    box.id = 'configWarnBox';
    box.className = 'configwarn hidden';
    const header = document.querySelector('.container > header') ||
      document.querySelector('header');
    if (header && header.parentNode) header.insertAdjacentElement('afterend', box);
    else document.body.prepend(box);
    return box;
  }

  return {
    update(s) {
      const warns = s.config_warnings || [];
      if (!warns.length) {
        if (box) box.classList.add('hidden');
        return;
      }
      const el = ensure();
      const html = '<b>&#9888; Config check — fix before running devices</b><ul>' +
        warns.map(w => `<li>${esc(w)}</li>`).join('') + '</ul>';
      if (el.innerHTML !== html) el.innerHTML = html; // 1 Hz feed: avoid re-render churn
      el.classList.remove('hidden');
    },
  };
}

// QA-label printer banner (TEC-352). Same shape as mountConfigWarn: injected
// under the page header by connectBenchWS, so all seven tools surface it
// without seven copies of the markup.
//
// Deliberately a banner and not an alert. A label that did not print does not
// invalidate the run — the device passed and is still configured — so nothing
// here should interrupt an operator mid-batch. But it must not be silent
// either: an unlabelled unit in the done pile is precisely what "no label, it
// doesn't ship" exists to prevent, so the warning stays on screen until a
// later label prints, and it names the serial the operator has to go and
// label by hand.
function mountPrinterWarn() {
  let box = null;

  function ensure() {
    if (box) return box;
    box = document.createElement('div');
    box.id = 'printerWarnBox';
    box.className = 'configwarn hidden';
    const anchor = document.getElementById('configWarnBox');
    if (anchor) anchor.insertAdjacentElement('afterend', box);
    else {
      const header = document.querySelector('.container > header') ||
        document.querySelector('header');
      if (header && header.parentNode) header.insertAdjacentElement('afterend', box);
      else document.body.prepend(box);
    }
    return box;
  }

  return {
    update(s) {
      const warning = (s.printer || {}).warning;
      if (!warning) {
        if (box) box.classList.add('hidden');
        return;
      }
      const el = ensure();
      const html = '<b>&#9888; Label printer</b><ul><li>' + esc(warning) +
        '</li></ul>';
      if (el.innerHTML !== html) el.innerHTML = html; // 1 Hz feed: avoid churn
      el.classList.remove('hidden');
    },
  };
}

// ── Device-label scan detection (TEC-349) ───────────────────────────────────
//
// Deciding whether some text is a scanned device label. Kept at the top level,
// separate from the UI that uses it, because it is the one piece of scan
// handling with enough edge cases to be worth testing on its own (see
// bench-core/tests/test_bench_js_scan.py).
//
// It is deliberately NOT a parser: it locates a payload and hands the raw
// string to the server, which owns the only parser
// (`bench_core.device_label`). These constants mirror that module — same keys,
// same 2-key minimum for what counts as a label.
const LABEL_SCAN_KEYS = 'SN|PW|I|M|U|B';
const LABEL_SCAN_PREFIX = '~';       // the scanner's configured scan marker
const LABEL_SCAN_MIN_KEYS = 2;
const _LABEL_KEY_AT =
  new RegExp('(?:^|[^A-Za-z0-9])((?:' + LABEL_SCAN_KEYS + ')\\s*:)', 'i');
const _LABEL_KEY_COUNT =
  new RegExp('(?:^|;)\\s*(?:' + LABEL_SCAN_KEYS + ')\\s*:', 'gi');
const _LABEL_HAS_PW = /(?:^|;)\s*PW\s*:/i;

// A label sitting in `text`: `{cut, payload}` — where to trim the field back
// to, and the raw string to send. Null when there isn't one.
//
// The marker is preferred because it is unambiguous. The shape fallback keeps
// a factory-reset scanner (no prefix configured) working, and covers a burst
// whose first character was lost to a focus change.
//
// A payload must carry the PW key, on top of the key-count minimum. Ending the
// burst is the flush timer's job, not this function's, but the two overlap: a
// scanner that stalls long enough mid-label would otherwise let a *prefix* of
// one through, and `SN:...;I:...` alone already clears the key count. Since
// the only reason to read a label at all is the password, "no PW yet" is a
// reliable proxy for "not finished". A label with an *empty* PW still has the
// key, so the server keeps its chance to explain that one properly.
function findLabelPayload(text) {
  if (!text) return null;
  const looksComplete = (s) =>
    (s.match(_LABEL_KEY_COUNT) || []).length >= LABEL_SCAN_MIN_KEYS &&
    _LABEL_HAS_PW.test(s);
  const marked = text.lastIndexOf(LABEL_SCAN_PREFIX);
  if (marked >= 0) {
    const payload = text.slice(marked + 1);
    if (looksComplete(payload)) return { cut: marked, payload: payload };
  }
  const m = _LABEL_KEY_AT.exec(text);
  if (m) {
    const at = m.index + m[0].length - m[1].length;
    const payload = text.slice(at);
    if (looksComplete(payload)) return { cut: at, payload: payload };
  }
  return null;
}

// Cheap "a scan may be in progress" test. `input` fires per character, so this
// is what arms the flush timer; findLabelPayload decides if it really was one.
function couldBeLabelScan(text) {
  return !!text && (text.indexOf(LABEL_SCAN_PREFIX) >= 0 || _LABEL_KEY_AT.test(text));
}

// Device-label scanning (TEC-349). Injected by connectBenchWS on the tools
// that opt in (`state.label_scan.enabled`), so the Teltonika pages get it
// without per-page markup and the other four are untouched.
//
// A 2D scanner in HID keyboard mode is a keyboard: it "types" the label and
// sends Enter. Three things follow from that, and shape everything below.
//
// 1. The characters land in whatever has focus — usually #siteName, which the
//    pages focus as soon as a device is detected. So capture is delegated from
//    `document` and works on any field: the payload is lifted back out and the
//    field restored, rather than trying to win a fight over focus.
// 2. `input` fires per character, so a payload is only complete once the
//    scanner's CR suffix arrives (or typing stops). Acting on the first
//    keystrokes that happen to look like a label would post half of one.
// 3. That trailing Enter would otherwise hit #siteName's own handler and
//    submit the form mid-scan, so it is swallowed — but only when a complete
//    payload is actually sitting there, or a human pressing Enter would stop
//    working.
//
// Nothing here parses the label or touches the password: the raw string goes
// to the server, which owns the one parser and keeps the password out of the
// page entirely. Returns {update(state)}.
function mountLabelScan(onState) {
  // Must exceed the scanner's worst inter-character gap — scan-probe.html
  // reports it. Only used when no CR suffix arrives to end the burst.
  const IDLE_FLUSH_MS = 200;

  let enabled = false;      // the tool opted in
  let manual = false;       // operator chose to type over an armed scan
  let lastMac = null;       // to reset `manual` when the device changes
  let pending = null;       // field a burst is currently landing in
  let timer = null;
  let returnFocus = null;   // {el, pos}: focus and caret a scan took over
  let banner = null, sink = null;
  let hintHTML = null, hintClass = null, placeholder = null;  // originals

  function ensureNodes() {
    if (banner) return;
    banner = document.createElement('div');
    banner.id = 'labelScanBanner';
    banner.className = 'scanwarn hidden';
    const header = document.querySelector('.container > header') ||
      document.querySelector('header');
    if (header && header.parentNode) header.insertAdjacentElement('afterend', banner);
    else document.body.prepend(banner);

    // Somewhere to put a burst that arrives with nothing focused, so it is not
    // simply lost. Never focused while the operator is in a real field.
    sink = document.createElement('input');
    sink.id = 'benchScanSink';
    sink.type = 'text';
    sink.className = 'scan-sink';
    sink.tabIndex = -1;
    sink.setAttribute('autocomplete', 'off');
    sink.setAttribute('aria-hidden', 'true');
    document.body.appendChild(sink);
  }

  function schedule(el) {
    pending = el;
    if (timer) clearTimeout(timer);
    timer = setTimeout(flush, IDLE_FLUSH_MS);
  }

  // Hand back the field and caret a scan took over, optionally re-inserting
  // `text` where the takeover happened. Every exit path that stole focus goes
  // through here, so a '~' that turned out not to begin a label costs the
  // operator nothing.
  function releaseFocus(text) {
    const target = returnFocus;
    returnFocus = null;
    if (sink) sink.value = '';
    const el = target && target.el;
    if (!el || !el.isConnected) return;
    if (text) {
      const pos = target.pos === null ? el.value.length : target.pos;
      el.value = el.value.slice(0, pos) + text + el.value.slice(pos);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      try { el.setSelectionRange(pos + text.length, pos + text.length); }
      catch (_) { /* not a text input; the value is still right */ }
    }
    try { el.focus(); } catch (_) { /* gone from the DOM mid-scan */ }
  }

  async function flush() {
    if (timer) { clearTimeout(timer); timer = null; }
    const el = pending;
    pending = null;
    if (!el) return;
    const hit = findLabelPayload(el.value);
    if (!hit) {
      // A stray '~' or half a label. Leave a real field alone; if the sink took
      // focus for this, give it back along with whatever followed the marker.
      if (returnFocus) releaseFocus(el === sink ? el.value : '');
      return;
    }

    // Take the payload out of the field before anything can submit or read it.
    el.value = el.value.slice(0, hit.cut);
    if (el !== sink) el.dispatchEvent(new Event('input', { bubbles: true }));
    if (returnFocus) releaseFocus('');
    else if (sink) sink.value = '';
    manual = false;

    const r = await postJSON('/api/label-scan', { raw: hit.payload },
                             { alertOnError: false });
    // Paint the outcome now rather than waiting up to a second for the next
    // state frame — a scan should feel immediate.
    if (r && r.error) showProblem(r.error);
    else if (r && onState) onState(r);
  }

  // The marker is the earliest moment a scan can be recognised, and
  // `beforeinput` is the only surface that offers it *cancelably* under every
  // scanner setting: with keypad emulation on, characters are composed by the
  // OS out of Alt+numpad sequences, so no `keydown` ever carries a '~'.
  // Cancelling that first insertion and moving focus to the sink keeps the rest
  // of the burst out of the operator's field entirely. The lift-and-restore
  // below still works without this — the pages focus #siteName as soon as a
  // device is detected, so a scan lands there and is pulled back out a frame
  // later — but the field visibly garbles itself in the meantime, which reads
  // as the tool malfunctioning.
  document.addEventListener('beforeinput', (e) => {
    if (!enabled || !sink) return;
    const el = e.target;
    if (!el || el === sink || typeof el.value !== 'string') return;
    if (typeof e.data !== 'string') return;   // deletion, composition, drag
    const at = e.data.indexOf(LABEL_SCAN_PREFIX);
    if (at < 0) return;
    e.preventDefault();
    returnFocus = {
      el: el,
      pos: typeof el.selectionStart === 'number' ? el.selectionStart : null,
    };
    // Keep the marker: it makes the payload unambiguous to findLabelPayload.
    // A scanner that delivers the whole burst as one insertion arrives here
    // complete, so carry the remainder across too.
    sink.value = e.data.slice(at);
    sink.focus();
    try { sink.setSelectionRange(sink.value.length, sink.value.length); }
    catch (_) { /* nothing to place the caret in yet */ }
    schedule(sink);
  }, true);

  document.addEventListener('input', (e) => {
    const el = e.target;
    if (!enabled || !el || typeof el.value !== 'string') return;
    if (couldBeLabelScan(el.value)) schedule(el);
  }, true);

  document.addEventListener('keydown', (e) => {
    if (!enabled) return;
    if (e.key === 'Enter') {
      const el = pending || e.target;
      if (el && typeof el.value === 'string' && findLabelPayload(el.value)) {
        // Capture phase on `document`, so this never reaches the field's own
        // Enter handler and cannot submit the form.
        e.preventDefault();
        e.stopPropagation();
        pending = el;
        flush();
      }
      return;
    }
    const active = document.activeElement;
    if (sink && (!active || active === document.body ||
                 active === document.documentElement)) sink.focus();
  }, true);

  function showProblem(message) {
    if (!banner) return;
    const html = '<b>&#9888; Scanned label rejected</b><div>' + esc(message) + '</div>';
    if (banner.innerHTML !== html) banner.innerHTML = html;
    banner.classList.remove('hidden');
  }

  function renderProblem(message) {
    if (!banner) return;
    if (message) { showProblem(message); return; }
    banner.classList.add('hidden');
  }

  function renderField(armed) {
    const input = $('labelPw'), hint = $('labelPwHint');
    if (!input) return;
    if (placeholder === null) placeholder = input.placeholder || '';
    if (hint && hintHTML === null) { hintHTML = hint.innerHTML; hintClass = hint.className; }

    const fromScan = !!(armed && armed.has_password) && !manual;
    input.disabled = fromScan;
    input.classList.toggle('from-scan', fromScan);
    if (fromScan && input.value) input.value = '';
    input.placeholder = fromScan ? 'read from the scanned label' : placeholder;
    if (!hint) return;

    if (!fromScan) {
      if (hint.innerHTML !== hintHTML) hint.innerHTML = hintHTML;
      hint.className = hintClass;
      return;
    }
    const bits = [];
    if (armed.serial) bits.push('SN ' + esc(armed.serial));
    if (armed.batch) bits.push('batch ' + esc(armed.batch));
    // matches_active === null means the device's own MAC was unreadable, so
    // the label could not be cross-checked. That is weaker than a match and
    // the operator should know which one they are looking at.
    const unchecked = armed.matches_active === null;
    const html = (unchecked ? '&#9888; ' : '&#10003; ') +
      'Password from the scanned label' +
      (bits.length ? ' (' + bits.join(', ') + ')' : '') +
      (unchecked ? ' — the device\u2019s own MAC could not be read, so this was '
                 + 'not checked against it' : '') +
      ' &middot; <a href="#" class="scan-manual">type it instead</a>';
    hint.className = 'hint ' + (unchecked ? 'hint-warn' : 'hint-ok');
    if (hint.innerHTML === html) return;   // 1 Hz feed: don't churn the DOM
    hint.innerHTML = html;
    const link = hint.querySelector('.scan-manual');
    if (link) link.addEventListener('click', (e) => {
      e.preventDefault();
      manual = true;                       // a typed value wins server-side
      renderField(armed);
      const box = $('labelPw');
      if (box) { box.disabled = false; box.focus(); }
    });
  }

  return {
    update(s) {
      const info = s.label_scan || { enabled: false };
      enabled = !!info.enabled;
      if (!enabled) return;
      ensureNodes();
      const mac = s.active_mac || null;
      if (mac !== lastMac) { manual = false; lastMac = mac; }
      renderProblem(info.problem);
      renderField(info.armed);
    },
  };
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
  const operatorBox = mountOperatorBox();
  const configWarn = mountConfigWarn();
  const printerWarn = mountPrinterWarn();
  let labelScan = null;
  const handleState = (s) => {
    operatorBox.update(s);
    configWarn.update(s);
    printerWarn.update(s);
    if (labelScan) labelScan.update(s);
    onState(s);
  };
  // Takes handleState so a scan can repaint immediately from its own response
  // instead of waiting for the next 1 Hz frame.
  labelScan = mountLabelScan(handleState);
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
      if (r.ok) handleState(await r.json());
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
      try { handleState(s); } catch (err) { console.error('render error', err); }
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

// A run is in flight, either kind (TEC-348). Pages use this instead of
// comparing against 'configuring', so a Verify pass gets the same live log,
// the same disabled controls and the same spinner without per-page changes.
const RUNNING_PHASES = ['configuring', 'verifying'];
// A run has finished and its result is on screen.
const FINISHED_PHASES = ['configured', 'verified', 'error'];

function isRunning(s) { return RUNNING_PHASES.includes(s.phase); }
function isFinished(s) { return FINISHED_PHASES.includes(s.phase); }
function isVerifyRun(s) { return s.phase === 'verifying' || s.phase === 'verified'; }

// Press Verify: check the connected device against what it was configured to
// be, changing nothing. Takes no password and no site name — the point of the
// mode is that the operator needs to know nothing about the unit in front of
// them (the server recovers that from the device's configure record).
async function submitVerify(btn, expected) {
  const el = (typeof btn === 'string') ? $(btn) : btn;
  if (el) el.disabled = true;
  return postJSON('/api/verify', { expected: expected || {} },
    { buttons: el ? [el] : [] });
}

// Wire up a #verifyBtn if the page has one and the tool supports the mode.
// Enabled whenever a device is detected and nothing is running; deliberately
// NOT gated on a form being filled in, because a QA sweep has nothing to fill.
//
// `opts.expected` is an optional function returning per-unit expectations the
// operator typed (a site name, say). Blanks are ignored server-side, so a tool
// can pass its form field unconditionally.
function mountVerifyButton(id, opts) {
  const btn = $(id || 'verifyBtn');
  if (!btn) return { update() {} };
  const expected = (opts || {}).expected;
  btn.addEventListener('click', () => submitVerify(btn, expected ? expected() : {}));
  return {
    update(s) {
      if (!s.verify_supported) { btn.style.display = 'none'; return; }
      btn.style.display = '';
      btn.disabled = !s.detected || s.busy;
      btn.textContent = s.phase === 'verifying' ? 'Verifying…' : 'Verify';
    },
  };
}

// Shared "live progress" log panel (Teltonika-style tools). Shows the live
// step log while a run is going and keeps it pinned to the bottom unless the
// operator has scrolled up. No-op if the page has no #liveCard/#liveLog.
function renderLiveLog(s) {
  const lc = $('liveCard'), ll = $('liveLog');
  if (!lc || !ll) return;
  if (isRunning(s) && (s.live_steps || []).length) {
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
