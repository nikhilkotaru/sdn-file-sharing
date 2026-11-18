"use strict";

/* ------------------------------------------------------------------ content */
const RUBRIC = [
  { id: "d1-arch", d: "D1", name: "Problem understanding & architecture", marks: 2, note: "arch",
    criteria: "Clear objectives, architecture, components, communication flow and expected network behaviour." },
  { id: "d1-socket", d: "D1", name: "TCP socket implementation", marks: 4, demos: ["basic", "roles"],
    criteria: "Low-level TCP client/server, message exchange, concurrency and error handling." },
  { id: "d1-sdn", d: "D1", name: "Mininet + SDN implementation", marks: 4, demos: ["pingall", "flows"],
    criteria: "Correct Mininet topology, a working controller and appropriate flow rules." },
  { id: "d1-integ", d: "D1", name: "Socket–SDN integration", marks: 3, demos: ["bruteforce"],
    criteria: "Application events change how the SDN network behaves, end to end." },
  { id: "d1-test", d: "D1", name: "Initial testing & demo", marks: 2, demos: ["unittests"],
    criteria: "The basic system works and test cases are demonstrated." },
  { id: "d2-app", d: "D2", name: "Complete application functionality", marks: 4, demos: ["concurrent"],
    criteria: "All major features work for multiple users and flows under normal conditions." },
  { id: "d2-dyn", d: "D2", name: "Dynamic SDN functionality", marks: 6, demos: ["reroute", "policy"], admin: true,
    criteria: "The controller reacts to conditions: routing, filtering, load balancing, rerouting, monitoring, blocking, recovery." },
  { id: "d2-robust", d: "D2", name: "Challenging scenario / robustness", marks: 4, demos: ["failover", "loss", "overload"],
    criteria: "Link failure, packet loss and server overload, and how the system responds." },
  { id: "d2-perf", d: "D2", name: "Performance evaluation & comparison", marks: 5, demos: ["compare"], note: "perf",
    criteria: "Metrics measured under several conditions and compared with a static baseline." },
  { id: "d2-docs", d: "D2", name: "Documentation, final demo & viva", marks: 6, note: "docs",
    criteria: "Complete documentation and a reproducible final demonstration." },
];

const DEMOS = {
  basic: { title: "Upload, list and download over TCP", time: "about 10 s",
    what: "alice uploads README.md from h1; bob lists the files, reads the metadata and downloads it from h2 with a SHA-256 check." },
  roles: { title: "Authentication and roles", time: "about 15 s",
    what: "Wrong password, read-only account, deleting someone else's file and a path-traversal name are all refused by the server." },
  pingall: { title: "Connectivity", time: "about 5 s",
    what: "Every host pings every other host through the four OpenFlow switches." },
  flows: { title: "Flow rules on the switches", time: "about 3 s",
    what: "Reads the OpenFlow tables on s1–s4 and groups the rules the controller installed by purpose." },
  bruteforce: { title: "Brute-force login blocked by the network", time: "about 25 s",
    what: "Three wrong passwords from h3. The server tells the controller, which installs a drop rule, so even the right password then fails." },
  unittests: { title: "Automated unit tests", time: "about 15 s",
    what: "Runs the full test suite: socket protocol, server and client, routing logic and the controller with simulated switches." },
  concurrent: { title: "Four simultaneous downloads", time: "about 30 s",
    what: "h1–h4 download the same file at once. Watch the live map: transfers spread over both paths." },
  reroute: { title: "Cut a link under a live transfer", time: "about 40 s",
    what: "Starts an 8 MB download, finds the path it is on and cuts that link. The connection moves to the other path and finishes." },
  policy: { title: "Access policy and firewall", time: "about 15 s",
    what: "h5 is a denied guest host: it may ping the server but not use the file service. Other server ports are firewalled." },
  failover: { title: "Failover on vs off", time: "about 1–2 min",
    what: "s1–s2 fails 3 s into a download, once with controller failover off and once on. Compares the longest stall." },
  loss: { title: "Packet loss on core links", time: "about 2 min",
    what: "Four downloads with 0% and 2% loss on every core link, static vs dynamic routing." },
  overload: { title: "Server overload", time: "about 1 min",
    what: "The server gets only 3 session slots and 10 clients arrive. Extra clients get BUSY and retry with backoff." },
  compare: { title: "Static vs dynamic routing", time: "about 2–3 min",
    what: "1, 2 and 4 concurrent downloads with the static baseline and with dynamic load-aware routing." },
};

const ALL_CHECKS = ["pingall", "flows", "basic", "roles", "unittests", "concurrent", "bruteforce", "policy", "reroute", "overload"];
const HOSTS = { "10.0.0.1": "h1", "10.0.0.2": "h2", "10.0.0.3": "h3", "10.0.0.4": "h4", "10.0.0.5": "h5", "10.0.0.100": "srv" };
const CORE = [["s1", "s2"], ["s2", "s4"], ["s1", "s3"], ["s3", "s4"]];

/* ------------------------------------------------------------------ state */
const S = {
  comp: "d1-socket",
  results: {},          // scenario -> last job view
  running: null,        // scenario currently running
  runAll: null,         // {queue, done}
  stats: null,
  view: "demo",
};

/* ------------------------------------------------------------------ helpers */
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 2600);
}

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data.error || r.statusText), { status: r.status, data });
  return data;
}

const size = (b) => !b ? "0 B" : b < 1024 * 1024 ? `${(b / 1024).toFixed(0)} KB` : `${(b / 1048576).toFixed(1)} MB`;
const pathClass =(p) => (p || "").includes("s2") ? "pa" : (p || "").includes("s3") ? "pb" : "idle";

/* ------------------------------------------------------------------ rubric nav */
function compStatus(c) {
  if (!c.demos) return "";
  if (c.demos.includes(S.running)) return "run";
  const rs = c.demos.map((d) => S.results[d]).filter(Boolean);
  if (!rs.length) return "";
  if (rs.some((r) => r.status === "failed" || r.status === "error")) return "fail";
  return rs.length === c.demos.length ? "pass" : "";
}

function renderRubric() {
  let html = "";
  for (const d of ["D1", "D2"]) {
    html += `<div><h2>${d === "D1" ? "Deliverable 1, 15 marks" : "Deliverable 2, 25 marks"}</h2><ul>`;
    for (const c of RUBRIC.filter((x) => x.d === d)) {
      html += `<li><button data-comp="${c.id}" class="${S.comp === c.id && !S.runAll ? "active" : ""}">
        <span class="dot ${compStatus(c)}"></span><span>${esc(c.name)}</span><span class="marks">${c.marks}</span></button></li>`;
    }
    html += "</ul></div>";
  }
  $("#rubric").innerHTML = html;
}

/* ------------------------------------------------------------------ stage */
function renderStage() {
  if (S.runAll) return renderScorecard();
  const c = RUBRIC.find((x) => x.id === S.comp);
  let html = `<div class="comp-head"><h2>${esc(c.name)}</h2>
    <p class="criteria">${esc(c.criteria)}</p><div class="meta">${c.d}, ${c.marks} marks</div></div>`;
  if (c.note === "arch") {
    html += `<div class="note"><p>The system has three parts that talk over sockets: TCP clients on h1–h5, a TCP file server on srv,
      and an os-ken SDN controller that programs the four switches over OpenFlow. The server also reports logins and transfers to the
      controller on a separate management link, which is what lets the application change the network.</p>
      <p>Expected behaviour: transfers spread over the two equal paths, a broken link is routed around, a denied host cannot use the
      service, and repeated failed logins get a host blocked at its switch.</p>
      <div class="actions"><button class="btn primary" data-goto="arch">Open the architecture page</button></div></div>`;
  }
  for (const sid of c.demos || []) html += demoCard(sid);
  if (c.admin) html += adminCard();
  if (c.note === "perf") {
    html += `<div class="note"><p>The full evaluation (E0–E5, two repetitions, about 35 minutes in this VM) was run with
      <code>experiments/run_experiments.py</code>. Its tables and charts are on the Results page. The button above runs a shorter live version.</p>
      <div class="actions"><button class="btn" data-goto="results">Open full results</button></div></div>`;
  }
  if (c.note === "docs") {
    html += `<div class="note"><p>Everything needed to reproduce the project is in the repository: <code>README.md</code> with setup and
      run steps, the topology, the controller and socket code, the policy file, 36 unit tests and the experiment scripts.</p>
      <p>For the final demo, use <b>Run all checks</b> in the top bar for a scorecard of every functional and robustness check,
      then show the architecture and the results.</p>
      <div class="actions"><button class="btn primary" data-goto="arch">Architecture</button>
      <button class="btn" data-goto="results">Results</button><button class="btn" id="run-all-2">Run all checks</button></div></div>`;
  }
  $("#stage").innerHTML = html;
  for (const sid of c.demos || []) paintResult(sid);
}

function demoCard(sid) {
  const d = DEMOS[sid];
  const busy = S.running !== null;
  return `<article class="demo" id="demo-${sid}">
    <div class="demo-head"><div><h3>${esc(d.title)}</h3><p>${esc(d.what)}</p></div>
      <button class="btn primary" data-run="${sid}" ${busy ? "disabled" : ""}>${S.running === sid ? '<span class="spinner"></span>Running' : "Run"}</button></div>
    <div class="demo-state">Takes ${esc(d.time)}</div>
    <div class="out"></div></article>`;
}

function paintResult(sid) {
  const box = $(`#demo-${sid} .out`);
  const r = S.results[sid];
  if (!box || !r) return;
  const state = $(`#demo-${sid} .demo-state`);
  const label = { running: "Running", passed: "All checks passed", failed: "Some checks failed", error: "Stopped with an error" }[r.status];
  state.innerHTML = `<b class="${r.status}">${label}</b> in ${r.elapsed}s`;
  let html = "";
  if (r.checks.length) {
    html += `<ul class="checks">${r.checks.map((c) => `<li class="${c.passed ? "pass" : "fail"}"><span class="mark">${c.passed ? "✓" : "✗"}</span>
      <span>${esc(c.name)}${c.detail && c.detail !== "None" ? `<span class="detail">${esc(c.detail)}</span>` : ""}</span></li>`).join("")}</ul>`;
  }
  if (r.metrics && r.metrics.chart) html += chartSVG(r.metrics.chart);
  html += `<details class="console" ${r.status === "running" ? "open" : ""}><summary>Commands and output (${r.lines.length} lines)</summary>
    <div class="log">${r.lines.map((l) => `<div class="${l.kind}"><span class="t">${l.t.toFixed(1).padStart(5)}</span>${esc(l.text)}</div>`).join("")}</div></details>`;
  box.innerHTML = html;
  const log = $(".log", box);
  if (log && r.status === "running") log.scrollTop = log.scrollHeight;
}

function adminCard() {
  const hosts = ["h1", "h2", "h3", "h4", "h5"];
  return `<article class="demo"><div class="demo-head"><div><h3>Operator controls</h3>
    <p>Change controller behaviour at runtime through its control socket (the same commands as <code>controller/ctl.py</code>).
    Routing mode and failover are also in the top bar.</p></div></div>
    <div class="admin">
      <div><label for="blk-host">Block a host at its switch</label>
        <div class="row"><select id="blk-host">${hosts.map((h) => `<option>${h}</option>`).join("")}</select>
        <input id="blk-dur" type="number" min="5" max="600" value="60" aria-label="Block duration in seconds">
        <button class="btn small" id="blk-go">Block</button></div></div>
      <div><label>Core links</label><div class="row" id="link-btns">${CORE.map(([a, b]) =>
        `<button class="btn small" data-link="${a},${b}">${a}–${b}</button>`).join("")}</div></div>
    </div></article>`;
}

/* ------------------------------------------------------------------ charts */
function chartSVG(ch) {
  const names = Object.keys(ch.series);
  const colors = { static: css("--static"), dynamic: css("--ink"), off: css("--fail"), on: css("--pass"), succeeded: css("--ink") };
  const W = 520, H = 210, L = 40, B = 34, T = 14, R = 10;
  // round the axis up to a clean value so the four gridlines land on readable numbers
  const peak = Math.max(1, ...names.flatMap((n) => ch.series[n])) * 1.1;
  const mag = 10 ** Math.floor(Math.log10(peak));
  const max = Math.ceil(peak / mag * 2) / 2 * mag;
  const gw = (W - L - R) / ch.groups.length;
  const bw = Math.min(46, (gw - 24) / names.length);
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(ch.title)}">`;
  for (let i = 0; i <= 4; i++) {
    const v = (max / 4) * i, y = H - B - (v / max) * (H - B - T);
    s += `<line x1="${L}" x2="${W - R}" y1="${y}" y2="${y}" stroke="${css("--line")}" />
      <text x="${L - 6}" y="${y + 4}" text-anchor="end">${v.toFixed(v < 10 ? 1 : 0)}</text>`;
  }
  ch.groups.forEach((g, gi) => {
    const x0 = L + gi * gw + (gw - bw * names.length) / 2;
    names.forEach((n, ni) => {
      const v = ch.series[n][gi] || 0, h = (v / max) * (H - B - T), x = x0 + ni * bw;
      s += `<rect x="${x + 2}" y="${H - B - h}" width="${bw - 4}" height="${h}" rx="3" fill="${colors[n] || css("--ink")}"><title>${esc(n)}: ${v}</title></rect>
        <text x="${x + bw / 2}" y="${H - B - h - 4}" text-anchor="middle">${(+v).toFixed(v < 10 ? 2 : 1)}</text>`;
    });
    s += `<text x="${L + gi * gw + gw / 2}" y="${H - B + 16}" text-anchor="middle">${esc(g)}</text>`;
  });
  if (ch.xlabel) s += `<text x="${(W + L) / 2}" y="${H - 4}" text-anchor="middle">${esc(ch.xlabel)}</text>`;
  s += "</svg>";
  const legend = names.map((n) => `<span style="margin-right:14px"><i style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${colors[n] || css("--ink")};margin-right:5px"></i>${esc(n)}</span>`).join("");
  return `<div class="chart"><h4>${esc(ch.title)}</h4>${s}<div style="font-size:12px;color:${css("--ink-2")}">${legend}</div></div>`;
}

/* ------------------------------------------------------------------ running demos */
async function runDemo(sid) {
  if (S.running) { toast("Wait for the running demo to finish"); return false; }
  let job;
  try {
    job = await api(`/api/run/${sid}`, {});
  } catch (e) {
    toast(e.message);
    return false;
  }
  S.running = sid;
  S.results[sid] = job;
  refreshAll();
  let since = job.next;
  while (true) {
    await new Promise((r) => setTimeout(r, 700));
    let v;
    try { v = await api(`/api/job/${job.id}?since=${since}`); } catch { continue; }
    const cur = S.results[sid];
    cur.lines = cur.lines.concat(v.lines);
    Object.assign(cur, { status: v.status, checks: v.checks, metrics: v.metrics, elapsed: v.elapsed });
    since = v.next;
    if (S.runAll) renderScorecard(); else paintResult(sid);
    if (v.status !== "running") break;
  }
  S.running = null;
  refreshAll();
  return S.results[sid].status === "passed";
}

async function runAll() {
  if (S.running) { toast("Wait for the running demo to finish"); return; }
  S.runAll = { queue: ALL_CHECKS.slice(), started: Date.now() };
  ALL_CHECKS.forEach((sid) => delete S.results[sid]);
  refreshAll();
  for (const sid of ALL_CHECKS) {
    S.runAll.current = sid;
    await runDemo(sid);
  }
  S.runAll.finished = Date.now();
  S.runAll.current = null;
  renderScorecard();
}

function renderScorecard() {
  const ra = S.runAll;
  const done = ALL_CHECKS.filter((s) => S.results[s] && S.results[s].status !== "running");
  const passed = done.filter((s) => S.results[s].status === "passed").length;
  const checks = ALL_CHECKS.flatMap((s) => (S.results[s] || { checks: [] }).checks);
  const ok = checks.filter((c) => c.passed).length;
  let html = `<div class="comp-head"><h2>${ra.finished ? `${passed} of ${ALL_CHECKS.length} demos passed` : "Running every check"}</h2>
    <p class="criteria">${ok} of ${checks.length} individual checks passed so far. Performance runs (static vs dynamic, packet loss,
    failover) are left out because they take several minutes; run them from their own sections.</p>
    <div class="meta">${ra.finished ? `Finished in ${Math.round((ra.finished - ra.started) / 1000)} s` : `${done.length} of ${ALL_CHECKS.length} done`}</div></div>
    <div class="scorecard"><h3>Scorecard</h3><div class="score-grid">`;
  for (const sid of ALL_CHECKS) {
    const r = S.results[sid];
    const st = !r ? "" : r.status === "running" ? "run" : r.status === "passed" ? "pass" : "fail";
    const comp = RUBRIC.find((c) => (c.demos || []).includes(sid));
    html += `<div><span class="rubric"><span class="dot ${st}" style="display:inline-block"></span></span>
      <a href="#" data-jump="${comp.id}">${esc(DEMOS[sid].title)}</a></div>`;
  }
  html += `</div><div class="actions" style="margin-top:14px"><button class="btn" id="close-score">Back to the rubric</button></div></div>`;
  const cur = ra.current || ALL_CHECKS.slice().reverse().find((s) => S.results[s]);
  if (cur) html += demoCard(cur);
  $("#stage").innerHTML = html;
  if (cur) paintResult(cur);
}

/* ------------------------------------------------------------------ topology */
const POS = {
  h1: [30, 40], h2: [30, 85], h3: [30, 130], h4: [30, 175], h5: [30, 220],
  s1: [122, 130], s2: [215, 52], s3: [215, 208], s4: [308, 130], srv: [376, 130],
};

function topoSVG(st, interactive) {
  const links = {};
  (st?.links || []).forEach((l) => (links[l.name] = l));
  const blocked = st?.blocked || {};
  const busyPaths = new Set((st?.connections || []).filter((c) => c.transferring).map((c) => c.path));
  let s = `<svg viewBox="0 0 400 260" role="img" aria-label="Network topology">`;
  // access links
  for (const h of ["h1", "h2", "h3", "h4", "h5"]) s += `<line x1="${POS[h][0]}" y1="${POS[h][1]}" x2="${POS.s1[0]}" y2="${POS.s1[1]}" stroke="${css("--line")}" stroke-width="2"/>`;
  s += `<line x1="${POS.s4[0]}" y1="${POS.s4[1]}" x2="${POS.srv[0]}" y2="${POS.srv[1]}" stroke="${css("--line")}" stroke-width="3"/>`;
  // core links
  for (const [a, b] of CORE) {
    const name = `${a}-${b}`, l = links[name] || { up: true, mbps: 0, util_pct: 0 };
    const color = (a === "s2" || b === "s2") ? css("--path-a") : css("--path-b");
    const [x1, y1] = POS[a], [x2, y2] = POS[b];
    const w = 4 + Math.min(100, l.util_pct || 0) / 100 * 9;
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
    const onBusy = [...busyPaths].some((p) => p.includes(a) && p.includes(b) && p.indexOf(a) + 3 === p.indexOf(b));
    if (interactive) s += `<line class="link-hit" data-link="${a},${b}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="transparent" stroke-width="24" tabindex="0" role="button" aria-label="${l.up ? "Cut" : "Restore"} link ${a} to ${b}"><title>${l.up ? "Cut" : "Restore"} ${a}–${b}</title></line>`;
    if (l.up) {
      s += `<line class="link-line" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${color}" stroke-width="${w}" stroke-linecap="round" pointer-events="none"/>`;
      if (onBusy && l.mbps > 0.1) s += `<line class="flowing" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="#fff" stroke-opacity=".75" stroke-width="2" pointer-events="none"/>`;
      if (st) s += `<text x="${mx + (a === "s1" ? -18 : 18)}" y="${my + (b === "s3" || a === "s3" ? 18 : -10)}" text-anchor="middle" font-size="11" fill="${css("--ink-2")}" pointer-events="none">${(l.mbps || 0).toFixed(1)} Mb/s</text>`;
    } else {
      s += `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="#B9C0CC" stroke-width="3" stroke-dasharray="5 5" pointer-events="none"/>
        <g transform="translate(${mx},${my})" pointer-events="none"><circle r="10" fill="${css("--fail")}"/>
        <path d="M-4 -4 L4 4 M4 -4 L-4 4" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/></g>`;
    }
  }
  // switches
  for (const sw of ["s1", "s2", "s3", "s4"]) {
    const [x, y] = POS[sw];
    s += `<rect x="${x - 19}" y="${y - 13}" width="38" height="26" rx="6" fill="${css("--ink")}"/>
      <text x="${x}" y="${y + 4.5}" text-anchor="middle" font-size="12.5" font-weight="600" fill="#fff">${sw}</text>`;
  }
  // hosts
  for (const h of ["h1", "h2", "h3", "h4", "h5"]) {
    const [x, y] = POS[h];
    const ip = Object.keys(HOSTS).find((k) => HOSTS[k] === h);
    const isBlocked = ip in blocked;
    s += `<circle cx="${x}" cy="${y}" r="14" fill="${isBlocked ? css("--fail-bg") : "#fff"}" stroke="${isBlocked ? css("--fail") : css("--ink")}"
      stroke-width="2" ${h === "h5" ? 'stroke-dasharray="3 3"' : ""}><title>${h} ${ip}${h === "h5" ? " (policy-denied guest)" : ""}${isBlocked ? " - blocked" : ""}</title></circle>
      <text x="${x}" y="${y + 4.5}" text-anchor="middle" font-size="12" font-weight="600" fill="${isBlocked ? css("--fail") : css("--ink")}">${h}</text>`;
  }
  const [sx, sy] = POS.srv;
  s += `<rect x="${sx - 20}" y="${sy - 17}" width="40" height="34" rx="6" fill="#fff" stroke="${css("--ink")}" stroke-width="2.5"/>
    <text x="${sx}" y="${sy + 4.5}" text-anchor="middle" font-size="12.5" font-weight="700" fill="${css("--ink")}">srv</text>
    <text x="${sx}" y="${sy + 34}" text-anchor="middle" font-size="10.5" fill="${css("--muted")}">:5000</text>`;
  s += "</svg>";
  return s;
}

/* ------------------------------------------------------------------ live panel */
function describeEvent(e) {
  const host = (c) => { const ip = String(c || "").split(":")[0]; return HOSTS[ip] || ip; };
  switch (e.event) {
    case "link_down": return `<b>Link ${esc(e.link)} down</b>`;
    case "link_up": return `Link ${esc(e.link)} up`;
    case "reroute": return `<b>Reroute</b> around ${esc(e.link)}: ${e.connections_moved} connection(s) moved in ${e.controller_ms} ms`;
    case "rebalance": return `<b>Rebalance</b> ${esc(host(e.conn))} to ${esc(e.path)}`;
    case "transfer_end": return `${esc(host(e.conn))} finished ${esc(e.file)} (${size(e.bytes)})${e.ok ? "" : ", failed"}`;
    case "mode": return `Routing <b>${esc(e.routing)}</b>, failover ${e.failover ? "on" : "off"}`;
    case "transfer_start": return `${esc(host(e.conn))} started ${esc(e.file)}`;
    case "block": return `<b>Blocked ${esc(HOSTS[e.ip] || e.ip)}</b> for ${e.duration_s}s: ${esc(e.reason)}`;
    case "unblock": return `Unblocked ${esc(HOSTS[e.ip] || e.ip)}`;
    case "policy_deny": return `<b>Policy drop</b> from ${esc(HOSTS[e.src] || e.src)}`;
    case "firewall_drop": return `<b>Firewall drop</b> ${esc(HOSTS[e.src] || e.src)} to port ${e.dst_port}`;
    case "auth_fail": return `Failed login from ${esc(HOSTS[e.client_ip] || e.client_ip)}`;
    default: {
      const rest = Object.entries(e).filter(([k]) => k !== "ts" && k !== "event").map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(", ");
      return `${esc(e.event)} ${esc(rest)}`;
    }
  }
}

function renderLive() {
  const st = S.stats;
  $("#topo").innerHTML = topoSVG(st, true);
  const ctrl = $("#st-ctrl"), sw = $("#st-sw");
  ctrl.className = "pill " + (st ? "ok" : "bad");
  ctrl.lastChild.textContent = st ? "Controller" : "Controller offline";
  const n = st ? st.switches.length : 0;
  sw.className = "pill " + (n === 4 ? "ok" : "bad");
  sw.lastChild.textContent = `Switches ${n}/4`;
  if (!st) return;
  document.querySelectorAll("#mode-seg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === st.mode));
  if (!renderLive.touching) $("#failover").checked = !!st.failover;

  // active transfers first; idle sessions (flows waiting to time out) are summarised
  const all = st.connections || [];
  const active = all.filter((c) => c.transferring);
  const idle = all.filter((c) => !c.transferring);
  const shown = active.concat(idle.slice(0, Math.max(0, 6 - active.length)));
  const rest = all.length - shown.length;
  $("#conns").innerHTML = shown.length ? shown.map((c) => {
    const h = HOSTS[c.client.split(":")[0]] || c.client;
    return `<li><b>${esc(h)}</b><span class="badge ${c.transferring ? pathClass(c.path) : "idle"}">${esc(c.path)}</span>
      <span class="muted">${c.transferring ? "transferring " + esc(c.file) : "idle"}</span></li>`;
  }).join("") + (rest > 0 ? `<li class="muted">and ${rest} more idle</li>` : "") : `<li class="muted">No open connections</li>`;

  const bl = Object.entries(st.blocked || {});
  $("#blocked").innerHTML = bl.length ? bl.map(([ip, secs]) =>
    `<span class="chip">${esc(HOSTS[ip] || ip)} ${Math.round(secs)}s <button data-unblock="${ip}" aria-label="Unblock ${esc(HOSTS[ip] || ip)}">×</button></span>`).join("")
    : `<span class="muted">None</span>`;

  const ev = (st.recent_events || []).slice(-8).reverse();
  $("#events").innerHTML = ev.length ? ev.map((e) => `<li><span class="when">${new Date(e.ts * 1000).toLocaleTimeString([], { hour12: false })}</span><span>${describeEvent(e)}</span></li>`).join("")
    : `<li class="muted">No events yet</li>`;

  document.querySelectorAll("#link-btns button").forEach((b) => {
    const [a, c] = b.dataset.link.split(","), l = (st.links || []).find((x) => x.name === `${a}-${c}`);
    b.textContent = `${l && !l.up ? "Restore" : "Cut"} ${a}–${c}`;
  });
}

async function poll() {
  try {
    const s = await api("/api/state");
    S.stats = s.stats;
  } catch {
    S.stats = null;
  }
  renderLive();
  setTimeout(poll, 1000);
}

async function toggleLink(a, b) {
  const l = (S.stats?.links || []).find((x) => x.name === `${a}-${b}`);
  const state = l && !l.up ? "up" : "down";
  try {
    await api("/api/admin", { action: "link", a, b, state });
    toast(state === "down" ? `Link ${a}–${b} cut` : `Link ${a}–${b} restored`);
  } catch (e) { toast(e.message); }
}

/* ------------------------------------------------------------------ results page */
function mdToHtml(md) {
  const lines = md.split("\n");
  let html = "", i = 0;
  while (i < lines.length) {
    const ln = lines[i];
    if (ln.startsWith("|")) {
      const rows = [];
      while (i < lines.length && lines[i].startsWith("|")) rows.push(lines[i++]);
      const cells = (r) => r.split("|").slice(1, -1).map((c) => c.trim());
      const head = cells(rows[0]);
      const body = rows.slice(2).map(cells);
      const num = (v) => /^-?[\d.]+$/.test(v);
      html += `<table class="tbl"><tr>${head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr>${body.map((r) =>
        `<tr>${r.map((c) => `<td class="${num(c) ? "num" : ""}">${c === "PASS" ? `<b style="color:${css("--pass")}">PASS</b>` : c === "FAIL" ? `<b style="color:${css("--fail")}">FAIL</b>` : esc(c)}</td>`).join("")}</tr>`).join("")}</table>`;
      continue;
    }
    if (ln.startsWith("## ")) html += `<h3>${esc(ln.slice(3))}</h3>`;
    else if (ln.startsWith("# ")) { /* page has its own title */ }
    else if (ln.startsWith("- ")) {
      html += "<ul>";
      while (i < lines.length && lines[i].startsWith("- ")) html += `<li>${esc(lines[i++].slice(2))}</li>`;
      html += "</ul>";
      continue;
    } else if (ln.trim()) html += `<p>${esc(ln)}</p>`;
    i++;
  }
  return html;
}

function jsonTable(rows) {
  if (!Array.isArray(rows) || !rows.length) return "";
  const keys = Object.keys(rows[0]);
  return `<table class="tbl narrow"><tr>${keys.map((k) => `<th>${esc(k.replace(/_/g, " "))}</th>`).join("")}</tr>${rows.map((r) =>
    `<tr>${keys.map((k) => `<td class="${typeof r[k] === "number" ? "num" : ""}">${esc(typeof r[k] === "object" ? JSON.stringify(r[k]) : r[k])}</td>`).join("")}</tr>`).join("")}</table>`;
}

async function renderResults() {
  const doc = $("#results-doc");
  let r;
  try { r = await api("/api/results"); } catch (e) { doc.innerHTML = `<p>Could not load results: ${esc(e.message)}</p>`; return; }
  const captions = { "e1_concurrency.png": "E1: throughput and completion time vs concurrent clients",
    "e2_loss.png": "E2: throughput under packet loss", "e3_failover.png": "E3: link failure during a transfer" };
  let html = `<h2>Performance evaluation</h2>
    <p class="lede">Full run of <code>experiments/run_experiments.py</code> (two repetitions per point) in this VM, compared against
    static shortest-path routing as the baseline. Absolute numbers are limited by the VM's CPU; the comparison is what matters.</p>`;
  if (r.images.length) html += `<h3>Charts</h3><div class="img-grid">${r.images.map((f) =>
    `<figure class="${f.startsWith("e1") ? "wide" : ""}"><img src="/results/${encodeURIComponent(f)}" alt="${esc(captions[f] || f)}"><figcaption>${esc(captions[f] || f)}</figcaption></figure>`).join("")}</div>`;
  html += r.summary_md ? mdToHtml(r.summary_md)
    : `<p class="muted">No full results yet. Stop this console, run <code>sudo python3 experiments/run_experiments.py</code> and then <code>python3 experiments/plot_results.py</code>.</p>`;
  const q = Object.entries(r.quick || {});
  if (q.length) {
    html += `<h3>Quick runs from this console</h3>`;
    for (const [name, rows] of q) html += `<p class="muted" style="margin:14px 0 6px">${esc(name)}</p>${jsonTable(rows)}`;
  }
  doc.innerHTML = html;
}

/* ------------------------------------------------------------------ views + events */
function showView(v) {
  S.view = v;
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.view === v);
    t.setAttribute("aria-selected", t.dataset.view === v);
  });
  for (const id of ["demo", "arch", "results"]) $(`#view-${id}`).classList.toggle("hidden", id !== v);
  if (v === "results") renderResults();
  if (v === "arch") $("#arch-map").innerHTML = topoSVG(null, false) +
    `<div class="legend" style="margin-top:8px"><span><i class="sw pa"></i>Path A s1-s2-s4</span><span><i class="sw pb"></i>Path B s1-s3-s4</span></div>`;
  window.scrollTo({ top: 0 });
}

function refreshAll() {
  renderRubric();
  renderStage();
  $("#run-all").disabled = !!S.running;
}

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button, a, .link-hit");
  if (!t) return;
  if (t.dataset.comp) { S.runAll = S.runAll && !S.runAll.finished ? S.runAll : null; S.comp = t.dataset.comp; if (S.runAll) S.runAll = null; refreshAll(); window.scrollTo({ top: 0 }); return; }
  if (t.dataset.run) { runDemo(t.dataset.run); return; }
  if (t.dataset.view) { showView(t.dataset.view); return; }
  if (t.dataset.goto) { showView(t.dataset.goto); return; }
  if (t.dataset.jump) { ev.preventDefault(); if (S.runAll?.finished) S.runAll = null; S.comp = t.dataset.jump; refreshAll(); return; }
  if (t.dataset.link) { const [a, b] = t.dataset.link.split(","); toggleLink(a, b); return; }
  if (t.dataset.unblock) { await api("/api/admin", { action: "unblock", ip: t.dataset.unblock }).catch((e) => toast(e.message)); toast("Host unblocked"); return; }
  if (t.dataset.mode) {
    try { await api("/api/admin", { action: "mode", value: t.dataset.mode }); toast(`Routing set to ${t.dataset.mode}`); } catch (e) { toast(e.message); }
    return;
  }
  if (t.id === "run-all" || t.id === "run-all-2") { showView("demo"); runAll(); return; }
  if (t.id === "close-score") { S.runAll = null; refreshAll(); return; }
  if (t.id === "blk-go") {
    const h = $("#blk-host").value, ip = Object.keys(HOSTS).find((k) => HOSTS[k] === h);
    try { await api("/api/admin", { action: "block", ip, duration: +$("#blk-dur").value || 60 }); toast(`${h} blocked`); } catch (e) { toast(e.message); }
  }
});

document.addEventListener("keydown", (ev) => {
  const t = ev.target;
  if ((ev.key === "Enter" || ev.key === " ") && t.classList && t.classList.contains("link-hit")) {
    ev.preventDefault();
    const [a, b] = t.dataset.link.split(",");
    toggleLink(a, b);
  }
});

$("#failover").addEventListener("change", async (ev) => {
  renderLive.touching = true;
  try { await api("/api/admin", { action: "failover", value: ev.target.checked }); toast(`Failover ${ev.target.checked ? "on" : "off"}`); }
  catch (e) { toast(e.message); }
  setTimeout(() => (renderLive.touching = false), 1500);
});

refreshAll();
poll();
