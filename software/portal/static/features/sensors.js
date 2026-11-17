/* Sensors page — current readings, charts (plain SVG), alerts, mesh-radio readings, admin settings.
   ES5 only (old iPhones): no arrow functions, template strings, let/const or optional chaining. */
(function () {
  "use strict";
  var B = window.Badger;
  var esc = B.esc, num = B.num;
  var RANGES = [["24h", "24 hours"], ["7d", "7 days"], ["30d", "30 days"], ["1y", "1 year"]];
  var page = { key: null, range: "24h", timer: null, data: null, admin: null };

  function lsGet(k, d) { try { var v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function el(id) { return document.getElementById(id); }
  function onPage() { return location.hash.replace(/^#/, "").split("?")[0] === "/sensors"; }
  function agoS(t) { return t ? B.ago(new Date(t * 1000).toISOString()) : "never"; }

  // ── units: stored metric; a viewer may prefer °F / inches on this device ──
  function units() { return lsGet("sensorUnits", page.data && page.data.units === "us" ? "us" : "metric"); }
  function conv(metric, v, unit, digits) {
    if (units() === "us") {
      if (unit === "°C") return { v: v * 9 / 5 + 32, unit: "°F", digits: 1 };
      if (unit === "cm") return { v: v / 2.54, unit: "in", digits: 1 };
      if (unit === "hPa") return { v: v * 0.02953, unit: "inHg", digits: 2 };
    }
    return { v: v, unit: unit, digits: digits };
  }
  function show(metric, v, unit, digits) {
    if (v === null || v === undefined) return "—";
    if (metric === "flood") return num(v) >= 0.5 ? "WET" : "dry";
    var c = conv(metric, num(v), unit, digits);
    var s = c.v.toFixed(c.digits);
    if (/^-0(\.0+)?$/.test(s)) s = s.slice(1);
    return s + (c.unit ? (c.unit === "%" || c.unit.charAt(0) === "°" ? "" : " ") + c.unit : "");
  }

  // ── chart ──
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function shortDate(t) { var d = new Date(t * 1000); return MONTHS[d.getMonth()] + " " + d.getDate(); }
  function chartSvg(points, metric, unit, digits, range, now) {
    var W = 640, H = 230, L = 56, R = 12, T = 12, Bm = 28;
    var span = { "24h": 86400, "7d": 604800, "30d": 2592000, "1y": 31536000 }[range];
    var t1 = now, t0 = now - span;
    if (!points.length) return '<p class="muted">No readings in this period yet.</p>';
    var lo = Infinity, hi = -Infinity, i, p;
    var pts = [];
    for (i = 0; i < points.length; i++) {
      p = points[i];
      var a = conv(metric, num(p[1]), unit, digits).v, mn = conv(metric, num(p[2]), unit, digits).v, mx = conv(metric, num(p[3]), unit, digits).v;
      pts.push([num(p[0]), a, mn, mx]);
      if (mn < lo) lo = mn; if (mx > hi) hi = mx;
    }
    if (metric === "flood") { lo = 0; hi = 1; }
    if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
    var pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
    if (metric === "flood") { lo = -0.1; hi = 1.1; }
    function X(t) { return L + (t - t0) / (t1 - t0) * (W - L - R); }
    function Y(v) { return T + (hi - v) / (hi - lo) * (H - T - Bm); }
    var step = pts.length > 1 ? (pts[pts.length - 1][0] - pts[0][0]) / (pts.length - 1) : span;
    var segs = [], cur = [];
    for (i = 0; i < pts.length; i++) {
      if (cur.length && pts[i][0] - cur[cur.length - 1][0] > Math.max(3 * step, span / 80)) { segs.push(cur); cur = []; }
      cur.push(pts[i]);
    }
    if (cur.length) segs.push(cur);
    var out = '<svg viewBox="0 0 ' + W + " " + H + '" width="100%" role="img" aria-label="chart" style="display:block;max-width:100%">';
    // grid + y labels
    for (i = 0; i <= 4; i++) {
      var v = lo + (hi - lo) * i / 4, y = Y(v).toFixed(1);
      out += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y + '" y2="' + y + '" stroke="currentColor" stroke-opacity=".12"/>';
      var lab = metric === "flood" ? (i === 0 ? "dry" : i === 4 ? "wet" : "") : v.toFixed(Math.abs(hi - lo) < 5 ? 1 : 0);
      out += '<text x="' + (L - 6) + '" y="' + (Number(y) + 4) + '" text-anchor="end" font-size="12" fill="currentColor" fill-opacity=".7">' + esc(lab) + "</text>";
    }
    // x labels
    for (i = 0; i <= 4; i++) {
      var t = t0 + (t1 - t0) * i / 4;
      var txt = range === "24h" ? B.tfmt(t * 1000, { short: true }) : shortDate(t);
      out += '<text x="' + X(t).toFixed(1) + '" y="' + (H - 8) + '" text-anchor="' + (i === 0 ? "start" : i === 4 ? "end" : "middle") +
        '" font-size="12" fill="currentColor" fill-opacity=".7">' + esc(String(txt).slice(0, 18)) + "</text>";
    }
    for (var s = 0; s < segs.length; s++) {
      var g = segs[s], band = "", line = "";
      for (i = 0; i < g.length; i++) band += (i ? "L" : "M") + X(g[i][0]).toFixed(1) + "," + Y(g[i][3]).toFixed(1);
      for (i = g.length - 1; i >= 0; i--) band += "L" + X(g[i][0]).toFixed(1) + "," + Y(g[i][2]).toFixed(1);
      for (i = 0; i < g.length; i++) line += (i ? "L" : "M") + X(g[i][0]).toFixed(1) + "," + Y(g[i][1]).toFixed(1);
      if (g.length > 1) out += '<path d="' + band + 'Z" fill="var(--accent)" fill-opacity=".15" stroke="none"/>';
      out += '<path d="' + line + '" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round"/>';
      if (g.length === 1) out += '<circle cx="' + X(g[0][0]).toFixed(1) + '" cy="' + Y(g[0][1]).toFixed(1) + '" r="3" fill="var(--accent)"/>';
    }
    return out + "</svg>";
  }

  function loadChart() {
    var box = el("sn-chart");
    if (!box || !page.key) return;
    var info = findMetric(page.key);
    var btns = RANGES.map(function (r) {
      return '<button class="btn' + (r[0] === page.range ? "" : " ghost") + '" data-range="' + r[0] + '">' + esc(r[1]) + "</button>";
    }).join("");
    box.innerHTML = '<div class="card section"><h2>' + esc(info ? info.title : page.key) + '</h2><div class="btns">' + btns +
      '</div><div id="sn-svg" class="section"><p class="muted">Loading…</p></div>' +
      '<p class="small muted">Line: average. Shaded: lowest to highest in each period.</p></div>';
    box.querySelectorAll("[data-range]").forEach(function (b) {
      b.onclick = function () { page.range = b.getAttribute("data-range"); loadChart(); };
    });
    B.api("/api/sensors/history?key=" + encodeURIComponent(page.key) + "&range=" + page.range).then(function (r) {
      var svg = el("sn-svg");
      if (!svg) return;
      if (r._status !== 200) { svg.innerHTML = '<p class="muted">' + esc(r.error || "Couldn’t load the chart.") + "</p>"; return; }
      var m = info ? info.m : { metric: page.key.split(":")[1], unit: r.unit, digits: 1 };
      svg.innerHTML = chartSvg(r.points || [], m.metric, m.unit, m.digits, page.range, num(r.now) || Date.now() / 1000);
    }).catch(function () { var svg = el("sn-svg"); if (svg) svg.innerHTML = '<p class="muted">The Badger didn’t answer.</p>'; });
  }

  function findMetric(key) {
    var d = page.data, all = [], i, j;
    if (!d) return null;
    all = (d.sensors || []).concat(d.remote || []);
    for (i = 0; i < all.length; i++) {
      var ms = all[i].metrics && all[i].metrics.length ? all[i].metrics : (all[i].last || []);
      for (j = 0; j < ms.length; j++) if (all[i].id + ":" + ms[j].metric === key) return { m: ms[j], title: all[i].name + " — " + ms[j].label };
    }
    return null;
  }

  // ── readings ──
  function tile(sensor, m, stale) {
    var key = sensor.id + ":" + m.metric;
    var danger = m.metric === "flood" && m.value >= 0.5;
    return '<a class="tile" href="#/sensors" data-key="' + esc(key) + '"' + (page.key === key ? ' style="border-color:var(--accent)"' : "") + ">" +
      '<span class="muted">' + esc(m.label) + "</span>" +
      '<b style="font-size:2rem;line-height:1.1' + (danger ? ";color:var(--danger)" : stale ? ";opacity:.5" : "") + '">' + esc(show(m.metric, m.value, m.unit, m.digits)) + "</b>" +
      "<span>" + esc(sensor.name) + "</span>" +
      '<span class="count">' + (stale ? "not responding · last " : "") + esc(agoS(sensor.at)) + "</span></a>";
  }

  function render(d) {
    page.data = d;
    var out = "";
    if (!d.running) out += '<div class="warnbox">The sensor service isn’t running, so these readings may be old. An admin can check it under Settings → Services.</div>';
    var al = d.alerts || [];
    if (al.length) {
      out += '<div class="list section">' + al.map(function (a) {
        return '<div class="card" style="border-color:var(--' + (a.level === "danger" ? "danger" : "warn") + ');border-width:2px"><b>' +
          (a.level === "danger" ? "⚠ " : "") + esc(a.text) + '</b><div class="meta">since ' + esc(agoS(a.since)) +
          (a.stale ? " · no new reading for a while" : "") + (a.remote ? " · reported over the mesh, not checked by the Badger" : "") + "</div></div>";
      }).join("") + "</div>";
    } else if ((d.sensors || []).length || (d.remote || []).length) {
      out += '<p class="small muted">No alerts right now.</p>';
    }
    var tiles = [];
    (d.sensors || []).forEach(function (s) {
      var ms = s.ok ? s.metrics : s.last;
      if (!ms || !ms.length) {
        tiles.push('<div class="tile"><span class="muted">' + esc(s.name) + '</span><b style="font-size:1.2rem">Not responding</b>' +
          (s.error ? '<span class="small muted">' + esc(s.error) + "</span>" : "") + "</div>");
        return;
      }
      ms.forEach(function (m) { if (m.metric !== "cpm") tiles.push(tile(s, m, !s.ok)); });
    });
    out += '<h2 class="section">At the Badger</h2>';
    out += tiles.length ? '<div class="grid">' + tiles.join("") + "</div>" :
      '<p class="muted">No sensors connected yet. See the manual (Sensors) for what to plug in — a weather sensor, a waterproof temperature probe, a flood switch or a Geiger counter.</p>';
    out += '<div id="sn-chart"></div>';
    if ((d.remote || []).length) {
      out += '<h2 class="section">From mesh radios</h2><p class="small muted">Reported over the radio by other people’s Meshtastic nodes. The Badger can’t check these readings.</p><div class="grid">';
      d.remote.forEach(function (r) {
        var rs = { id: r.id, name: (r.name || r.node) + (r.trusted ? "" : " (public channel)"), at: r.at };
        (r.metrics || []).forEach(function (m) { out += tile(rs, m, false); });
      });
      out += "</div>";
    }
    out += '<div class="btns section"><button class="btn ghost" id="sn-units">Show ' + (units() === "us" ? "°C, cm" : "°F, inches") + "</button>" +
      '<span class="small muted">Updated ' + esc(agoS(d.updated)) + "</span></div>";
    out += '<div id="sn-admin"></div>';
    el("sn-body").innerHTML = out;
    el("sn-body").querySelectorAll("[data-key]").forEach(function (a) {
      a.onclick = function (e) { e.preventDefault(); page.key = a.getAttribute("data-key"); render(page.data); var c = el("sn-chart"); if (c && c.scrollIntoView) c.scrollIntoView(); };
    });
    el("sn-units").onclick = function () { lsSet("sensorUnits", units() === "us" ? "metric" : "us"); render(page.data); };
    if (!page.key) {
      var first = el("sn-body").querySelector("[data-key]");
      if (first) page.key = first.getAttribute("data-key");
    }
    loadChart();
    if (B.isAdmin()) adminLoad();
  }

  function refresh() {
    B.api("/api/sensors").then(function (r) {
      if (!onPage()) return;
      if (r._status === 401) { B.needMember("Sensors"); return; }
      if (r._status !== 200) { el("sn-body").innerHTML = '<p class="muted">' + esc(r.error || "Couldn’t load the sensors.") + "</p>"; return; }
      if (adminBusy()) return;                          // don't wipe a half-edited settings form
      render(r);
    }).catch(function () { var b = el("sn-body"); if (b) b.innerHTML = '<p class="muted">The Badger didn’t answer. Are you on its Wi-Fi?</p>'; });
  }

  // ── admin settings ──
  function adminBusy() { var f = el("sn-adm-form"); return !!(f && f.getAttribute("data-dirty")); }
  function opt(v, label, sel) { return '<option value="' + esc(v) + '"' + (String(v) === String(sel) ? " selected" : "") + ">" + esc(label) + "</option>"; }
  function adminLoad() {
    B.api("/api/sensors/admin").then(function (r) {
      if (r._status !== 200 || !el("sn-admin")) return;
      page.admin = r;
      page.rules = JSON.parse(JSON.stringify(r.effective.rules || r.rules || []));
      adminRender();
    });
  }
  function sensorChoices() {
    var a = page.admin, list = [["*", "Every sensor here"]];
    (a.devices || []).forEach(function (d) { list.push([d.id, d.name + " (" + d.id + ")"]); });
    (a.effective.remote_nodes || []).forEach(function (n) { list.push(["mesh-" + n.slice(1), "Mesh radio " + n]); });
    return list;
  }
  function ruleRow(r, i) {
    var a = page.admin, metrics = Object.keys(a.metrics);
    function inp(name, val, w) { return '<label class="small">' + esc(name.label) + '<br><input type="number" step="any" data-i="' + i + '" data-f="' + name.f + '" value="' + esc(val === null || val === undefined ? "" : val) + '" style="width:' + (w || 6) + 'em"></label>'; }
    return '<div class="card" data-rule="' + i + '"><div class="btns">' +
      '<label class="small"><input type="checkbox" data-i="' + i + '" data-f="enabled"' + (r.enabled !== false ? " checked" : "") + "> On</label>" +
      '<select data-i="' + i + '" data-f="sensor">' + sensorChoices().map(function (s) { return opt(s[0], s[1], r.sensor); }).join("") + "</select>" +
      '<select data-i="' + i + '" data-f="metric">' + metrics.map(function (m) { return opt(m, a.metrics[m].label, r.metric); }).join("") + "</select>" +
      '<select data-i="' + i + '" data-f="level">' + opt("warning", "Warning", r.level) + opt("danger", "Danger", r.level) + "</select></div>" +
      '<div class="btns section">' +
      inp({ label: "Above", f: "above" }, r.above) + inp({ label: "Below", f: "below" }, r.below) +
      inp({ label: "× normal", f: "above_background" }, r.above_background, 5) + inp({ label: "Gap to clear", f: "hysteresis" }, r.hysteresis, 5) +
      inp({ label: "Readings in a row", f: "confirm" }, r.confirm, 4) + inp({ label: "Quiet for (min)", f: "cooldown_min" }, r.cooldown_min, 5) + "</div>" +
      '<div class="btns section"><select data-i="' + i + '" data-f="board">' + opt("community", "Post on the neighbourhood board", r.board) +
      opt("family", "Post on the family board", r.board) + opt("none", "Don’t post", r.board) + "</select>" +
      '<label class="small"><input type="checkbox" data-i="' + i + '" data-f="mesh"' + (r.mesh ? " checked" : "") + "> Also send over the mesh</label>" +
      '<input type="text" maxlength="60" placeholder="note, e.g. pipes may freeze" data-i="' + i + '" data-f="note" value="' + esc(r.note || "") + '">' +
      '<button class="btn ghost" data-del="' + i + '">Remove</button></div>' +
      '<p class="small muted">Units: ' + esc(a.metrics[r.metric] ? a.metrics[r.metric].unit || "1 = wet" : "") + " (always metric here)</p></div>";
  }
  function readRules() {
    var box = el("sn-adm-form");
    box.querySelectorAll("[data-f]").forEach(function (inp) {
      var i = Number(inp.getAttribute("data-i")), f = inp.getAttribute("data-f"), r = page.rules[i];
      if (!r) return;
      if (inp.type === "checkbox") r[f] = inp.checked;
      else if (inp.type === "number") r[f] = inp.value === "" ? null : Number(inp.value);
      else r[f] = inp.value;
    });
  }
  function adminRender() {
    var a = page.admin, e = a.effective, box = el("sn-admin");
    if (!box) return;
    var devs = (a.devices || []).map(function (d) {
      return '<div class="card"><div class="row"><div><b>' + esc(d.name) + '</b> <span class="pill ' + (d.ok ? "ok" : "bad") + '">' + (d.ok ? "working" : "not responding") + "</span>" +
        '<div class="meta mono">' + esc(d.id) + " · " + esc(d.type) + (d.port ? " · " + esc(d.port) : "") + (d.auto ? " · found automatically" : "") + "</div>" +
        (d.error ? '<p class="small">' + esc(d.error) + "</p>" : "") + "</div>" +
        '<label class="small">Name shown<br><input type="text" maxlength="32" data-name="' + esc(d.id) + '" value="' + esc((e.names || {})[d.id] || "") + '" placeholder="' + esc(d.name) + '"></label></div></div>';
    }).join("") || '<p class="muted small">No sensors found. Check the wiring guide in the manual, then [sensors] in prepper.toml.</p>';
    var errs = (a.config_errors || []).map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("");
    var allowed = {}; (e.remote_nodes || []).forEach(function (n) { allowed[n] = true; });
    var nodes = (a.seen_nodes || []).slice();
    Object.keys(allowed).forEach(function (n) { if (!nodes.some(function (s) { return s.node === n; })) nodes.push({ node: n, name: "", at: null }); });
    var nodeHtml = nodes.map(function (n) {
      return '<label class="small" style="display:block;margin:6px 0"><input type="checkbox" data-node="' + esc(n.node) + '"' + (allowed[n.node] ? " checked" : "") + "> " +
        esc(n.name || "(no name)") + ' <span class="mono">' + esc(n.node) + "</span> · " + (n.trusted ? "family channel" : "public channel") +
        " · heard " + esc(agoS(n.at)) + (n.metrics && n.metrics.length ? " · " + esc(n.metrics.join(", ")) : "") + "</label>";
    }).join("") || '<p class="small muted">No mesh radio has sent sensor readings yet.</p>';
    var hours = [0, 1, 2, 3, 6, 12, 24];
    box.innerHTML = '<div class="card section" id="sn-adm-form"><h2>Sensor settings (admin)</h2>' +
      (errs ? '<div class="warnbox"><b>Problems in prepper.toml [sensors]:</b><ul>' + errs + "</ul></div>" : "") +
      '<h3 class="section">Sensors</h3><div class="list">' + devs + "</div>" +
      '<h3 class="section">Mesh radio</h3>' +
      '<label class="small"><input type="checkbox" id="sn-mesh"' + (e.mesh_alerts ? " checked" : "") + "> Send alerts over the mesh (rules marked “also send over the mesh”)</label>" +
      '<div class="btns section"><label class="small">Channel<br><select id="sn-meshb">' + opt("community", "Neighbourhood (public)", e.mesh_board) + opt("family", "Family", e.mesh_board) + "</select></label>" +
      '<label class="small">Readings summary<br><select id="sn-sum">' + hours.map(function (h) { return opt(h, h ? "every " + h + " h" : "off", e.summary_every_h); }).join("") + "</select></label>" +
      '<label class="small">Summary channel<br><select id="sn-sumb">' + opt("community", "Neighbourhood (public)", e.summary_board) + opt("family", "Family", e.summary_board) + "</select></label></div>" +
      '<p class="small muted">LoRa airtime is shared: at most a few sensor messages an hour go out, urgent ones first.</p>' +
      '<h3 class="section">Alerts</h3><div class="list" id="sn-rules"></div>' +
      '<div class="btns section"><button class="btn ghost" id="sn-add">Add an alert</button><button class="btn ghost" id="sn-reset">Back to the standard alerts</button></div>' +
      '<h3 class="section">Mesh radios to show</h3><p class="small muted">Readings from other people’s radios appear on this page only if you tick them here.</p>' + nodeHtml +
      '<label class="small"><input type="checkbox" id="sn-untr"' + (e.remote_untrusted ? " checked" : "") + "> Also accept readings sent on the public channel (anyone can send those)</label>" +
      '<div class="btns section"><button class="btn" id="sn-save">Save settings</button><span id="sn-msg" class="small"></span></div>' +
      '<div class="btns section"><button class="btn ghost" id="sn-test">Post a test message</button><button class="btn ghost" id="sn-testm">Test message + mesh</button></div></div>';
    drawRules();
    if (page.msg) { el("sn-msg").textContent = page.msg; page.msg = ""; }
    var form = el("sn-adm-form");
    form.addEventListener("input", function () { form.setAttribute("data-dirty", "1"); });
    form.addEventListener("change", function () { form.setAttribute("data-dirty", "1"); });
    el("sn-add").onclick = function () {
      readRules();
      page.rules.push({ id: "my" + (page.rules.length + 1) + "-" + Math.floor(Math.random() * 900 + 100), enabled: true, sensor: "*", metric: "temperature",
        above: null, below: 0, hysteresis: 1, confirm: 2, cooldown_min: 60, board: "community", mesh: false, level: "warning", note: "" });
      form.setAttribute("data-dirty", "1"); drawRules();
    };
    el("sn-reset").onclick = function () {
      if (!confirm("Replace your alert settings with the standard ones (flood, freezing, heat, radiation)?")) return;
      save({ rules: null });
    };
    el("sn-save").onclick = function () { readRules(); save(collect()); };
    el("sn-test").onclick = function () { test(false); };
    el("sn-testm").onclick = function () { test(true); };
  }
  function drawRules() {
    var box = el("sn-rules");
    box.innerHTML = page.rules.map(ruleRow).join("") || '<p class="small muted">No alerts set.</p>';
    box.querySelectorAll("[data-del]").forEach(function (b) {
      b.onclick = function () { readRules(); page.rules.splice(Number(b.getAttribute("data-del")), 1); el("sn-adm-form").setAttribute("data-dirty", "1"); drawRules(); };
    });
  }
  function collect() {
    var names = {}, nodes = [];
    el("sn-adm-form").querySelectorAll("[data-name]").forEach(function (i) { if (i.value.trim()) names[i.getAttribute("data-name")] = i.value.trim(); });
    el("sn-adm-form").querySelectorAll("[data-node]").forEach(function (i) { if (i.checked) nodes.push(i.getAttribute("data-node")); });
    return { rules: page.rules, names: names, remote_nodes: nodes, remote_untrusted: el("sn-untr").checked,
      mesh_alerts: el("sn-mesh").checked, mesh_board: el("sn-meshb").value, summary_every_h: Number(el("sn-sum").value),
      summary_board: el("sn-sumb").value };
  }
  function save(body) {
    var msg = el("sn-msg");
    B.api("/api/sensors/admin", { body: body }).then(function (r) {
      if (r._status !== 200) { msg.textContent = r.error || "Not saved."; msg.style.color = "var(--danger)"; return; }
      var f = el("sn-adm-form"); if (f) f.removeAttribute("data-dirty");
      page.msg = "Saved. The sensor service picks it up within a few seconds.";
      adminLoad();
    });
  }
  function test(mesh) {
    var msg = el("sn-msg");
    B.api("/api/sensors/test", { body: { board: "community", mesh: mesh } }).then(function (r) {
      msg.textContent = r.ok ? "Test posted on the neighbourhood board" + (r.mesh ? " and queued for the mesh." : ".") : (r.error || r.message || "Test failed.");
    });
  }

  B.register("/sensors", function () {
    B.h('<h1>🌡️ Sensors</h1><div id="sn-body"><p class="muted">Loading…</p></div>');
    if (page.timer) clearInterval(page.timer);
    page.timer = setInterval(function () { if (!onPage()) { clearInterval(page.timer); page.timer = null; return; } refresh(); }, 30000);
    refresh();
  });
})();
