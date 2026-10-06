"use strict";
// Gotcha ATP — single page over /api/* and the /api/events SSE stream.

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const LOADED_AT = Date.now() / 1000;

const S = {
  screen: "home", home: null, connection: null, site: "",
  stages: [], stageState: {}, rows: {}, current: null, selected: null,
  questions: [], answered: {}, finished: null, log: [], fullRun: true,
};

// ── plumbing ────────────────────────────────────────────────────────────────

async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

function toast(msg, err = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), err ? 7000 : 3000);
}

function show(screen) {
  S.screen = screen;
  for (const s of ["home", "run", "manual", "result"]) $("screen-" + s).classList.toggle("hidden", s !== screen);
  for (const b of document.querySelectorAll("#nav button")) b.classList.toggle("on", b.dataset.screen === screen);
}

const badge = (cls, text) => `<span class="badge badge-${cls}">${esc(text)}</span>`;
const isWarning = (r) => r.state === "fail" && (r.severity === "medium" || r.severity === "low");

// ── 1 · Unit ────────────────────────────────────────────────────────────────

function setBadge(el, ok, text) {
  el.className = "badge " + (ok === true ? "badge-ok" : ok === false ? "badge-err" : "badge-pend");
  el.textContent = text;
}

async function loadHome() {
  try { S.home = await api("/api/home"); } catch (e) { toast(e.message, true); return; }
  const h = S.home;
  setBadge($("st-tailnet"), h.tailnet.ok, h.tailnet.ok ? "● Tailnet" : "● Tailnet — " + (h.tailnet.error || "offline"));
  setBadge($("st-central"), h.bench_central.url ? h.bench_central.ok : null,
    !h.bench_central.url ? "bench-central not set" : "● bench-central" + (h.bench_central.outbox ? ` · ${h.bench_central.outbox} queued` : ""));
  setBadge($("st-fleet"), h.fleet.configured ? true : null, h.fleet.configured ? "● Fleet" : "Fleet not configured");
  $("st-release").innerHTML = h.release.ok
    ? `release <code>${esc(h.release.name)}</code> · <code>${esc(h.release.commit)}</code>`
    : `<span style="color:var(--red)">release.yaml: ${esc(h.release.error)}</span>`;

  const units = $("units");
  units.innerHTML = h.units.length ? "" : `<span class="empty">No Gotcha operator stations on the tailnet — type a site name below.</span>`;
  const rank = (u) => [!(u.operator?.online || u.server?.online), u.site];
  const sorted = [...h.units].sort((a, b) => {
    const ra = rank(a), rb = rank(b);
    for (let i = 0; i < ra.length; i++) if (ra[i] !== rb[i]) return ra[i] < rb[i] ? -1 : 1;
    return 0;
  });
  for (const u of sorted) {
    const b = document.createElement("button");
    const online = u.operator?.online || u.server?.online;
    const light = (p, label) => `<span class="light ${p ? (p.online ? "on" : "off") : ""}">${label}</span>`;
    b.className = "unit" + (online ? "" : " off") + (S.site === u.site ? " sel" : "");
    b.dataset.site = u.site;
    b.innerHTML = `<div class="name">${esc(u.site)}</div><div class="lights">${light(u.server, "server")}${light(u.operator, "operator")}</div>`;
    b.onclick = () => { S.site = u.site; $("site").value = ""; filterUnits(); markUnits(); };
    units.appendChild(b);
  }
  filterUnits();
  if (!$("engineer").value) $("engineer").value = h.creds.engineer || "";
  $("password").placeholder = h.creds.ssh_password_set ? "•••••• from config.toml" : "kela password";
  $("pw-note").textContent = h.creds.config_found ? "Leave empty to use the saved one." : `No ${h.creds.config_path}`;
  if (h.connection) showConfirm(h.connection);
}

function markUnits() {
  for (const b of $("units").querySelectorAll(".unit")) b.classList.toggle("sel", b.dataset.site === S.site);
}

function filterUnits() {
  const q = $("site").value.trim().toLowerCase();
  for (const b of $("units").querySelectorAll(".unit")) b.classList.toggle("hidden", !!q && !b.dataset.site.includes(q));
}

async function connect() {
  const site = $("site").value.trim() || S.site;
  if (!site) { toast("Pick a unit or type a site name.", true); return; }
  $("connect").disabled = true;
  $("connect-status").innerHTML = `<span class="spin"></span> Connecting to ${esc(site)}…`;
  $("confirm").classList.add("hidden");
  try {
    const c = await api("/api/connect", { site, operator: $("operator").value.trim(),
      password: $("password").value, engineer: $("engineer").value });
    $("password").value = "";
    showConfirm(c);
  } catch (e) {
    toast(e.message, true);
  } finally {
    $("connect").disabled = false;
    $("connect-status").textContent = "";
  }
}

function showConfirm(c) {
  S.connection = c;
  S.site = c.site;
  const s = c.session, id = c.identity || {};
  const sv = id.server || {}, sb = sv.build_info || {};
  const op = id.operator, ob = (op && op.build_info) || {};
  const line = (k, v) => `<span class="k">${esc(k)}</span><span>${v}</span>`;
  let lines = line("Server", `<b>${esc(sv.hostname || c.site)}</b> — ` +
    (s.server_path === "via-operator" ? `through the operator ${badge("warn", "backup route")}` : `over the tailnet`) +
    ` · ${esc(s.rtt_ms ?? "?")} ms`);
  lines += line("Build-info", sb.site ? `site <code>${esc(sb.site)}</code> role <code>${esc(sb.role || "?")}</code> model <code>${esc(sb.model || "?")}</code>`
    : `<span style="color:var(--yellow)">missing on the server</span>`);
  lines += line("Operator", s.operator_ok
    ? `<b>${esc(op?.hostname || s.operator)}</b>` + (ob.site ? ` · site <code>${esc(ob.site)}</code>` : "")
    : `<span style="color:var(--yellow)">${esc(s.operator_error || "no session")}</span> — operator checks will be skipped`);
  const warn = !s.operator_ok || s.server_path === "via-operator";
  $("confirm").className = "status " + (warn ? "s-warn" : "s-pass");
  $("confirm-title").textContent = "Is this the unit in front of you?";
  $("confirm-lines").innerHTML = lines;
  $("confirm").classList.remove("hidden");
  $("advanced").classList.remove("hidden");
  $("unit-tag").textContent = c.site;
  $("unit-tag").classList.remove("hidden");
}

async function startRun(body, path = "/api/run") {
  try { await api(path, body); show("run"); } catch (e) { toast(e.message, true); }
}

// ── sub-checks ──────────────────────────────────────────────────────────────

function subChecks(r) {
  const cs = (r.detail && r.detail.checks) || [];
  return cs.length < 2 ? null : {
    all: cs, bad: cs.filter((c) => c.ok === false),
    unk: cs.filter((c) => c.ok === null), good: cs.filter((c) => c.ok === true),
  };
}

const li = (c, cls, mk) => `<li class="${cls}"><span class="mk">${mk}</span><b>${esc(c.label)}</b><span>${esc(c.actual)}</span></li>`;

function checksHtml(r) {
  const s = subChecks(r);
  if (!s) return null;
  const n = s.all.length;
  const verdict = s.bad.length ? `${s.bad.length} of ${n} failed` + (s.unk.length ? `, ${s.unk.length} not evaluated` : "")
    : s.unk.length ? `${s.unk.length} of ${n} not evaluated` : `all ${n} ok`;
  let h = `<div class="verdict-line">${verdict}</div>`;
  if (s.bad.length || s.unk.length) h += `<ul class="checks">${s.bad.map((c) => li(c, "bad", "✗")).join("")}${s.unk.map((c) => li(c, "unk", "?")).join("")}</ul>`;
  if (s.good.length) h += `<details><summary>${s.bad.length || s.unk.length ? `${s.good.length} ok` : "show"}</summary><ul class="checks">${s.good.map((c) => li(c, "ok", "✓")).join("")}</ul></details>`;
  return h;
}

function failedText(r) {
  const s = subChecks(r);
  if (!s || (!s.bad.length && !s.unk.length)) return r.actual;
  return [...s.bad, ...s.unk].map((c) => `${c.label}: ${c.actual}`).join("; ");
}

// ── 2 · Run ─────────────────────────────────────────────────────────────────


// A finished stage's badge, from its rows: what failed / warned / was not
// checked, so one unchecked row does not hide that everything else passed.
function stageCounts(st) {
  const rows = st.rows.map((m) => S.rows[m.id]).filter(Boolean);
  return {
    pass: rows.filter((r) => r.state === "pass").length,
    fail: rows.filter((r) => r.state === "fail" && !isWarning(r)).length,
    warn: rows.filter(isWarning).length,
    amber: rows.filter((r) => r.state === "amber").length,
  };
}

// The line under a stage name: one small counter per outcome, full words on hover.
function stageSummary(st, state) {
  if (!st.implemented && state === "amber") return `<span class="cnt dim">not built yet</span>`;
  if (st.rows.every((r) => r.cls === "manual") && state === "pending" && S.questions.length)
    return `<span class="cnt warn">your turn</span>`;
  if (state === "pending") return `<span class="cnt dim">waiting</span>`;
  if (state === "running") return `<span class="cnt run">running…</span>`;
  const n = stageCounts(st);
  const chip = (k, cls, mk, word) => n[k] ? `<span class="cnt ${cls}" title="${n[k]} ${word}">${mk} ${n[k]}</span>` : "";
  return chip("pass", "ok", "✓", "pass") + chip("fail", "err", "✗", "fail") +
    chip("warn", "warn", "!", "warning") + chip("amber", "dim", "?", "not checked") || `<span class="cnt dim">—</span>`;
}

function renderStages() {
  const list = $("stage-list");
  list.innerHTML = S.stages.length ? "" : `<div class="empty" style="padding:8px">No run yet.</div>`;
  for (const st of S.stages) {
    const state = S.stageState[st.id] || "pending";
    const d = document.createElement("div");
    d.className = "stage" + (st.id === (S.selected || S.current) ? " sel" : "");
    d.innerHTML = `<span class="sdot ${state}"></span><div class="sbody"><div class="sname"><span class="sid">${esc(st.id)}</span>${esc(st.name)}</div>` +
      `<div class="counts">${stageSummary(st, state)}</div></div>`;
    d.onclick = () => { S.selected = st.id; renderStages(); renderRows(); };
    list.appendChild(d);
  }
  renderRunStatus();
}

function renderRunStatus() {
  const rows = Object.values(S.rows);
  const total = S.stages.reduce((n, s) => n + s.rows.length, 0);
  const n = (f) => rows.filter(f).length;
  const pass = n((r) => r.state === "pass"), fail = n((r) => r.state === "fail"), amber = n((r) => r.state === "amber");
  const box = $("run-status");
  const open = S.questions.filter((q) => !S.answered[q.id]).length;
  if (!S.stages.length) { box.className = "status"; return; }
  if (S.finished) {
    box.className = "status " + (S.finished.summary?.verdict === "PASS" ? "s-pass" : S.finished.summary?.verdict === "FAIL" ? "s-fail" : "s-warn");
    $("run-phase").textContent = `Finished — ${S.finished.summary?.verdict || "error"}`;
    $("run-msg").textContent = "See the Result tab.";
  } else if (open) {
    box.className = "status s-warn";
    $("run-phase").textContent = "Your turn";
    $("run-msg").textContent = S.during ? "The run is waiting for you — see Questions."
      : `${open} question${open > 1 ? "s" : ""} waiting at the C2.`;
  } else {
    const st = S.stages.find((s) => s.id === S.current);
    box.className = "status s-run";
    $("run-phase").textContent = st ? `Running ${st.id} · ${st.name}` : "Starting…";
    $("run-msg").textContent = `${rows.length} of ${total} checks done · ${pass} pass · ${fail} fail · ${amber} not checked`;
  }
  const pct = (x) => total ? (100 * x / total).toFixed(1) + "%" : "0";
  $("run-bar").innerHTML = `<i class="p-pass" style="width:${pct(pass)}"></i><i class="p-fail" style="width:${pct(fail)}"></i><i class="p-amber" style="width:${pct(amber)}"></i>`;
  $("to-questions").classList.toggle("hidden", !open);
  $("cancel").classList.toggle("hidden", !!S.finished);
}

function renderRows() {
  const st = S.stages.find((s) => s.id === (S.selected || S.current));
  const box = $("rows");
  box.innerHTML = "";
  if (!st) return;
  for (const meta of st.rows) {
    const r = S.rows[meta.id];
    const d = document.createElement("div");
    if (!r) {
      d.className = "rowcard pending";
      d.innerHTML = `<div class="head"><span class="id">${esc(meta.id)}</span><span class="item">${esc(meta.item)}</span>${badge("pend", meta.cls === "manual" ? "question" : "waiting")}</div>`;
    } else {
      const warning = isWarning(r);
      const cls = warning ? "warn" : r.state;
      const sc = subChecks(r);
      // Partly checked: say how much passed, not just that something could not be checked.
      const partial = r.state === "amber" && sc && sc.good.length && !sc.bad.length;
      const b = r.state === "pass" ? badge("ok", "pass") : warning ? badge("warn", "warning")
        : r.state === "fail" ? badge("err", "fail")
        : partial ? badge("warn", `${sc.good.length} ok · ${sc.unk.length} not checked`)
        : badge("pend", "not checked");
      let body = checksHtml(r);
      if (body === null) body = r.state === "amber" && r.reason ? `<div class="why">${esc(r.reason)}</div>` : `<div class="actual">${esc(r.actual)}</div>`;
      if (r.state !== "pass") body += `<div class="expected">expected: ${esc(r.expected)}</div>`;
      if (r.hint) body += `<details><summary>How to fix</summary><div class="hint">${esc(r.hint)}</div></details>`;
      d.className = "rowcard " + cls;
      d.innerHTML = `<div class="head"><span class="id">${esc(r.id)}</span><span class="item">${esc(r.item)}</span>
        <span class="muted small">${esc(r.severity)}</span>${b}</div><div class="body">${body}</div>`;
    }
    box.appendChild(d);
  }
}

// ── 3 · Questions ───────────────────────────────────────────────────────────

function renderQuestions() {
  const box = $("questions");
  box.innerHTML = "";
  $("manual-empty").classList.toggle("hidden", S.questions.length > 0);
  const open = S.questions.filter((q) => !S.answered[q.id]).length;
  $("nav-q").textContent = open;
  $("nav-q").classList.toggle("hidden", !open);
  $("finish").classList.toggle("hidden", !S.questions.length || open > 0);
  $("finish").textContent = S.during && !S.finished ? "Back to the run →" : "See the result →";
  for (const q of S.questions) {
    const a = S.answered[q.id];
    const d = document.createElement("div");
    d.className = "q" + (a ? " answered" : "");
    d.innerHTML = `<div><div class="qid">${esc(q.id)}</div><div class="prompt">${esc(q.prompt)}</div></div><div class="opts"></div>`;
    const opts = d.querySelector(".opts");
    const note = document.createElement("input");
    note.className = "note";
    note.placeholder = "optional note";
    note.value = a ? a.note : "";
    for (const opt of ["yes", "no"].concat(q.skippable ? ["skip"] : [])) {
      const b = document.createElement("button");
      b.className = opt + (a && a.answer === opt ? " on" : "");
      b.textContent = opt[0].toUpperCase() + opt.slice(1);
      b.onclick = async () => {
        try {
          await api("/api/answer", { id: q.id, answer: opt, note: note.value });
          S.answered[q.id] = { answer: opt, note: note.value };
          renderQuestions(); renderRunStatus();
          if (S.during && !S.questions.some((x) => !S.answered[x.id])) show("run");
        } catch (e) { toast(e.message, true); }
      };
      opts.appendChild(b);
    }
    d.appendChild(note);
    box.appendChild(d);
  }
}

// ── 4 · Result ──────────────────────────────────────────────────────────────

function fixCard(r, warn = false) {
  const s = subChecks(r);
  const items = s && (s.bad.length || s.unk.length)
    ? [...s.bad.map((c) => li(c, "", "✗")), ...s.unk.map((c) => li(c, "unk", "?"))].join("")
    : `<li><span class="mk">✗</span><span style="grid-column: span 2">${esc(r.state === "amber" ? r.reason || r.actual : r.actual)}</span></li>`;
  const hint = r.hint ? `<details><summary>How to fix</summary><div class="hint">${esc(r.hint)}</div></details>` : "";
  return `<div class="fix${warn ? " warn" : ""}"><div class="title"><span class="id">${esc(r.id)}</span><b>${esc(r.item)}</b></div><ul>${items}</ul>${hint}</div>`;
}

function stageName(id) {
  return S.stages.find((s) => s.id === id.split(".")[0])?.name || "";
}

function renderResult() {
  const f = S.finished;
  $("result-empty").classList.toggle("hidden", !!f);
  $("result").classList.toggle("hidden", !f);
  if (!f) return;
  const s = f.summary;
  if (!s) {
    $("verdict").className = "verdict v-FAIL";
    $("verdict").innerHTML = `<div class="big">ERROR</div><div><div class="line">The run did not finish.</div><div class="sub">${esc(f.error || "")}</div></div>`;
    return;
  }
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  const parts = [];
  if (s.failed.length) parts.push(plural(s.failed.length, "failure"));
  if (s.critical_amber.length) parts.push(plural(s.critical_amber.length, "critical check") + " not done");
  const line = s.stamp_eligible ? "Ready to stamp."
    : !s.full_run ? "Partial run — only a full run can be stamped."
    : `Not stampable: ${parts.join(" and ")}.`;
  $("verdict").className = "verdict v-" + s.verdict;
  $("verdict").innerHTML = `<div class="big">${esc(s.verdict)}</div>
    <div><div class="line">${esc(line)}</div><div class="sub">${esc(S.site)} · ${s.counts.rows} checks</div></div>
    <div class="tally">
      <div class="t green"><b>${s.counts.pass}</b><span>pass</span></div>
      <div class="t red"><b>${s.failed.length}</b><span>fail</span></div>
      <div class="t yellow"><b>${s.warnings.length}</b><span>warning</span></div>
      <div class="t"><b>${s.amber.length}</b><span>not checked</span></div>
    </div>`;

  const failedRows = s.failed.map((i) => S.rows[i]).filter(Boolean);
  $("fix-count").textContent = failedRows.length || "";
  $("fix-count").classList.toggle("hidden", !failedRows.length);
  $("res-fix").innerHTML = failedRows.length ? failedRows.map((r) => fixCard(r)).join("")
    : `<div class="empty">Nothing failed.</div>`;

  const warnRows = s.warnings.map((i) => S.rows[i]).filter(Boolean);
  $("warn-count").textContent = warnRows.length;
  $("warn-card").classList.toggle("hidden", !warnRows.length);
  $("res-warn").innerHTML = warnRows.map((r) => fixCard(r, true)).join("");

  // Not checked: grouped by why, so 50 not-yet-built rows are one line.
  const groups = new Map();
  for (const id of s.amber) {
    const r = S.rows[id];
    if (!r) continue;
    const why = (r.reason || r.actual || "").startsWith("not implemented") ? "not built yet"
      : (r.reason || r.actual || "").startsWith("prerequisite") ? r.reason
      : null;
    const key = why || `${id} ${r.item}: ${r.reason || r.actual}`;
    if (!groups.has(key)) groups.set(key, { ids: [], why });
    groups.get(key).ids.push(id);
  }
  const stagesOf = (ids) => [...new Set(ids.map((i) => i.split(".")[0]))].join(", ");
  $("amber-count").textContent = s.amber.length;
  $("amber-card").classList.toggle("hidden", !s.amber.length);
  $("res-amber").innerHTML = [...groups.entries()].map(([key, g]) => g.why
    ? `<li><span class="n">${g.ids.length}</span><span>${esc(g.why)} <span class="muted">(${esc(stagesOf(g.ids))})</span></span></li>`
    : `<li><span class="n">1</span><span>${esc(key)}</span></li>`).join("");

  const up = f.upload || {};
  $("upload").innerHTML = up.status === "uploaded" ? badge("ok", "✓ uploaded to bench-central")
    : `${badge("warn", "bench-central: " + (up.status || "?"))} <button id="retry" class="link">retry</button>`;
  const retry = $("retry");
  if (retry) retry.onclick = async () => {
    try { const r = await api("/api/upload/retry", {}); toast(`${r.results.length} tried, ${r.pending} still queued`); }
    catch (e) { toast(e.message, true); }
  };
  $("dl-pdf").href = "/api/report/pdf";
  $("dl-pdf").classList.toggle("hidden", !f.pdf);
  $("dl-html").href = "/api/report/html";
  $("rerun").classList.toggle("hidden", !s.failed.length && !s.critical_amber.length);
  $("result-foot").innerHTML = `Record <code>${esc(f.record)}</code>` + (f.pdf_note ? ` · ${esc(f.pdf_note)}` : "") +
    (f.error ? `<br><span style="color:var(--red)">Run error: ${esc(f.error)}</span>` : "");
}

function summaryText() {
  const f = S.finished, s = f?.summary;
  if (!s) return "";
  const line = (i) => { const r = S.rows[i] || {}; return `  ${i} ${r.item}: ${failedText(r)}`; };
  return [`Gotcha ATP ${S.site} — ${s.verdict}, stamp eligible: ${s.stamp_eligible ? "Yes" : "No"}`,
    s.failed.length ? "Needs fixing:\n" + s.failed.map(line).join("\n") : "Needs fixing: nothing",
    s.warnings.length ? "Warnings:\n" + s.warnings.map(line).join("\n") : "Warnings: none",
    `Record: ${f.record}`].join("\n");
}

// ── events ──────────────────────────────────────────────────────────────────

function onEvent(ev) {
  const live = ev.ts >= LOADED_AT - 1;
  switch (ev.type) {
    case "run_started":
      Object.assign(S, { stages: ev.stages, stageState: {}, rows: {}, current: null, selected: null,
        questions: [], answered: {}, during: false, finished: null, log: [], fullRun: ev.full_run });
      if (ev.site) { S.site = ev.site; $("unit-tag").textContent = ev.site; $("unit-tag").classList.remove("hidden"); }
      if (live) show("run");
      break;
    case "stage_started": S.stageState[ev.stage] = "running"; S.current = ev.stage; break;
    case "row": S.rows[ev.row.id] = ev.row; break;
    case "stage_finished": S.stageState[ev.stage] = ev.state; break;
    case "needs_input":
      // `during`: a stage is waiting on the engineer (S9's power cut) — go straight to it.
      S.questions = ev.questions; S.during = !!ev.during;
      if (live && ev.during) { show("manual"); toast("Your turn — the run is waiting for you."); }
      else if (live) toast("Automated checks done — questions are waiting.");
      break;
    case "log": S.log.push(ev.msg); $("log").textContent = S.log.slice(-400).join("\n"); break;
    case "run_finished":
      S.finished = ev;
      if (live && (S.screen === "run" || S.screen === "manual")) show("result");
      break;
  }
  renderStages(); renderRows(); renderQuestions(); renderResult();
}

function listen() {
  const es = new EventSource("/api/events");
  es.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch (e) { console.error(e); } };
}

// ── wiring ──────────────────────────────────────────────────────────────────

for (const b of document.querySelectorAll("#nav button")) b.onclick = () => show(b.dataset.screen);
$("refresh").onclick = loadHome;
$("connect").onclick = connect;
$("site").addEventListener("keydown", (e) => { if (e.key === "Enter") connect(); });
$("site").addEventListener("input", filterUnits);
$("disconnect").onclick = async () => {
  try { await api("/api/disconnect", {}); } catch (e) { toast(e.message, true); }
  $("confirm").classList.add("hidden"); $("advanced").classList.add("hidden");
  $("unit-tag").classList.add("hidden"); S.connection = null;
};
$("run-full").onclick = () => startRun({ soak: false });
$("run-soak").onclick = () => {
  const ok = confirm([
    "Run ATP + power-cycle soak (S9)",
    "",
    "After the normal checks, you will be asked to CUT MAINS to the PwF and the Edge box,",
    "wait 30 seconds and restore it. The tool then checks the unit comes back on its own",
    "and re-checks everything — about 20 minutes on top of a normal run.",
    "",
    "• Someone must be at the breaker / plug.",
    "• The unit must NOT be in operational use — every sensor and the C2 go down.",
    "",
    "Start the run with the power-cycle soak?",
  ].join("\n"));
  if (ok) startRun({ soak: true });
};
$("run-stages").onclick = () => {
  const stages = $("stages").value.split(/[\s,]+/).filter(Boolean);
  if (!stages.length) { toast("Type the stages to run, e.g. S1,S2", true); return; }
  startRun({ stages, soak: false });
};
$("to-questions").onclick = () => show("manual");
$("finish").onclick = () => show(S.during && !S.finished ? "run" : "result");
$("rerun").onclick = () => startRun({}, "/api/rerun-failed");
$("full-again").onclick = () => show("home");
$("cancel").onclick = async () => { if (confirm("Cancel the run? Remaining checks are marked not checked.")) await api("/api/cancel", {}); };
$("copy").onclick = async () => {
  try { await navigator.clipboard.writeText(summaryText()); toast("Summary copied."); }
  catch { toast("Clipboard not available.", true); }
};

loadHome();
listen();
