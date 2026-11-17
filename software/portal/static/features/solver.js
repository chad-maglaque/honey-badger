/* Problem solver — page script (feature "solver"). ES5, no frameworks.
   Deep solve: the problem becomes a job on the Badger; this page polls it (the job id is remembered,
   so reloading the page or coming back later finds it), shows each step and tool call live, then
   the answer with working, assumptions, checks and sources. Calculators work without the AI.
   Admins also get the AI models panel (roles, versions, self-test, roll back). */
(function () {
  var B = window.Badger;
  if (!B) return;
  var esc = B.esc;
  var pollTimer = null, tickTimer = null, curJob = null, seen = 0, lastJob = null, calcs = null;
  var KEY = "solverJob";
  var EXAMPLES = [
    "I have a 100 W solar panel, about 4 peak sun hours in winter, and a 12 V 100 Ah lead-acid battery. How many hours a night can I run a 30 W fridge, and is the panel big enough?",
    "Spring is 25 m above the cabin and 300 m away. With 3/4 inch PVC pipe, how many litres a minute will flow?",
    "How much unscented 6% bleach do I need to treat a 55 gallon drum of clear water?",
    "Two neighbours share a well pump. Each can repair (cost 3) or wait. If one repairs both get 5; if both repair each gets 5 minus 3; if nobody repairs both get 0. What are the equilibria?",
    "We walk 18 km with 700 m of climbing carrying 15 kg packs (I weigh 70 kg). How long will it take?",
    "Write a Python function that checks if a number is prime, and test it on 97 and 91."
  ];
  var STATE = { queued: "Waiting for its turn", running: "Working on it", done: "Done", error: "Stopped",
    cancelled: "Cancelled", interrupted: "Interrupted" };

  function $(id) { return document.getElementById(id); }
  function store(k, v) { try { if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) { /* private mode */ } }
  function load(k) { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } }
  function secs(s) {
    s = Math.max(0, Math.round(s || 0));
    if (s < 60) return s + " s";
    var m = Math.floor(s / 60);
    return m < 60 ? m + " min " + (s % 60) + " s" : Math.floor(m / 60) + " h " + (m % 60) + " min";
  }
  function stopPolling() { clearTimeout(pollTimer); clearInterval(tickTimer); pollTimer = null; tickTimer = null; }

  function eventHtml(e) {
    var icon = { plan: "📝", tool: "🔧", result: "↳", note: "ℹ️", model: "🧠", think: "💭", bench: "📏" }[e.kind] || "•";
    var txt = e.kind === "tool" ? "<b>" + esc(e.text) + "</b> <span class=\"mono small\">" + esc(e.args || "") + "</span>" :
      e.kind === "result" ? '<span class="mono small' + (e.ok === false ? " muted" : "") + '">' + esc(e.text) + "</span>" :
      e.kind === "plan" ? "<b>Plan</b><div class=\"small\" style=\"white-space:pre-wrap\">" + esc(e.text) + "</div>" : esc(e.text);
    return '<div class="result small"><span class="muted">' + esc(secs(e.t)) + "</span> " + icon + " " + txt + "</div>";
  }

  function resultHtml(r) {
    var out = "";
    if (r.redirect) return '<div class="answer">' + esc(r.answer) + "</div>";
    (r.warnings || []).forEach(function (w) { out += '<div class="warnbox">' + esc(w) + "</div>"; });
    out += '<div class="answer" id="sAnswer"></div>';
    if (r.confidence) out += '<p class="small">Confidence: <span class="pill ' + (r.confidence === "high" ? "ok" : r.confidence === "low" ? "bad" : "warn") + '">' + esc(r.confidence) + "</span></p>";
    if (r.sources && r.sources.length) out += '<h3 class="section">Sources</h3>' + r.sources.map(function (s) {
      return '<div class="result">[' + B.num(s.n) + '] <a href="' + B.href(s.url) + '">' + esc(s.title) + '</a> <span class="src">' + esc(s.source) + "</span></div>";
    }).join("");
    if (r.tools_used && r.tools_used.length) out += '<p class="small muted">Tools used: ' + esc(r.tools_used.join(", ")) + "</p>";
    if (r.bench) out += '<p>Self-test: <b>' + esc(r.bench.score) + "/" + esc(r.bench.total) + "</b> right, " + esc(r.bench.tps) + " tokens/s</p>";
    return out;
  }

  function render(j) {
    var box = $("sJob");
    if (!box) return false;
    var live = j.state === "queued" || j.state === "running";
    var head = '<h2>' + esc(STATE[j.state] || j.state) + (j.state === "queued" && j.position ? " (" + esc(j.position) + " ahead)" : "") + "</h2>" +
      (j.problem ? '<p class="small muted" style="white-space:pre-wrap">' + esc(j.problem) + "</p>" : "") +
      '<p class="small"><span id="sElapsed">' + esc(secs(j.elapsed)) + "</span>" +
      (j.tokens ? " · " + esc(j.tokens) + " tokens" : "") + (j.tps ? " · " + esc(j.tps) + " tokens/s" : "") +
      (j.model ? " · " + esc(j.model) : "") + "</p>" +
      (live ? '<div class="btns"><button class="btn ghost" id="sCancel">Cancel</button></div>' : "");
    $("sHead").innerHTML = head;
    var c = $("sCancel");
    if (c) c.onclick = function () {
      c.disabled = true;
      B.api("/api/solver/cancel", { body: { id: j.id } }).then(function () { poll(j.id); });
    };
    var evs = $("sEvents");
    (j.events || []).forEach(function (e) { evs.insertAdjacentHTML("beforeend", eventHtml(e)); });
    seen = j.event_count || seen;
    if (j.state === "done" && j.result) {
      $("sResult").innerHTML = resultHtml(j.result);
      var a = $("sAnswer"); if (a) a.textContent = j.result.answer || "";
    } else if (j.state === "error" || j.state === "interrupted") {
      $("sResult").innerHTML = '<div class="warnbox">' + esc(j.error || "Something went wrong.") + "</div>";
    } else if (j.state === "cancelled") {
      $("sResult").innerHTML = '<p class="muted">Cancelled.</p>';
    }
    return live;
  }

  function showJob(id) {
    stopPolling();
    curJob = id; seen = 0; lastJob = null;
    var box = $("sJob");
    if (!box) return;
    box.style.display = "";
    box.innerHTML = '<div id="sHead"><p class="muted">Loading…</p></div><details class="section"><summary class="small">Steps and tool calls</summary><div id="sEvents"></div></details><div id="sResult" class="section"></div>';
    poll(id);
  }

  function poll(id) {
    clearTimeout(pollTimer);
    if (!$("sJob") || curJob !== id) return;
    B.api("/api/solver/job?id=" + encodeURIComponent(id) + "&since=" + seen).then(function (j) {
      if (!$("sJob") || curJob !== id) return;
      if (j._status !== 200) { store(KEY, null); $("sHead").innerHTML = '<p class="muted">' + esc(j.error || "That problem has expired.") + "</p>"; return; }
      lastJob = { j: j, at: Date.now() };
      var live = render(j);
      clearInterval(tickTimer);
      if (live) {
        if (j.state === "running") tickTimer = setInterval(function () {
          var el = $("sElapsed");
          if (!el || !lastJob) { clearInterval(tickTimer); return; }
          el.textContent = secs(lastJob.j.elapsed + (Date.now() - lastJob.at) / 1000);
        }, 1000);
        pollTimer = setTimeout(function () { poll(id); }, j.state === "queued" ? 4000 : 2000);
      } else if (j.kind === "solve") {
        var btn = $("sGo"); if (btn) btn.disabled = false;
      }
    }).catch(function () { pollTimer = setTimeout(function () { poll(id); }, 5000); });
  }

  function submit() {
    var ta = $("sProblem"), msg = $("sMsg"), btn = $("sGo");
    var text = (ta.value || "").trim();
    if (text.length < 3) { msg.textContent = "Describe the problem first (numbers and units help)."; return; }
    btn.disabled = true; msg.textContent = "Sending…";
    B.api("/api/solver/solve", { body: { problem: text } }).then(function (r) {
      if (r.redirect) { btn.disabled = false; msg.textContent = ""; $("sJob").style.display = ""; $("sJob").innerHTML = '<div class="answer"></div>'; $("sJob").firstChild.textContent = r.redirect; return; }
      if (r._status !== 200) { btn.disabled = false; msg.textContent = r.error || "The Badger couldn’t take that."; return; }
      msg.textContent = r.position ? "In the queue (" + r.position + " ahead). You can leave this page and come back." : "Started. You can leave this page and come back.";
      store(KEY, r.id);
      showJob(r.id);
    }).catch(function () { btn.disabled = false; msg.textContent = "The Badger didn’t answer."; });
  }

  // ── calculators (no AI) ──
  function calcForm(c) {
    var f = '<p class="small muted">' + esc(c.desc) + "</p>";
    c.params.forEach(function (p) {
      var hint = p.unit ? " (" + p.unit + ', or e.g. "50 ft")' : p.kind === "list" || p.kind === "json" ? " (JSON)" : "";
      f += '<p><label class="small">' + esc(p.name) + (p.required ? " *" : "") + esc(hint) + "<br>";
      if (p.choices && p.choices.length) {
        f += '<select data-p="' + esc(p.name) + '"><option value="">–</option>' + p.choices.map(function (o) { return "<option>" + esc(o) + "</option>"; }).join("") + "</select>";
      } else if (p.kind === "bool") {
        f += '<select data-p="' + esc(p.name) + '"><option value="">–</option><option>true</option><option>false</option></select>';
      } else {
        f += '<input data-p="' + esc(p.name) + '" data-k="' + esc(p.kind) + '" style="width:100%">';
      }
      f += "</label></p>";
    });
    return f + '<div class="btns"><button class="btn" id="cGo">Calculate</button></div><div id="cOut" class="section"></div>';
  }
  function calcRun(c) {
    var args = {}, bad = "";
    var els = $("cForm").querySelectorAll("[data-p]");
    for (var i = 0; i < els.length; i++) {
      var el = els[i], v = (el.value || "").trim(), k = el.getAttribute("data-k");
      if (!v) continue;
      if (k === "list" || k === "json") { try { v = JSON.parse(v); } catch (e) { bad = el.getAttribute("data-p") + " must be JSON"; } }
      else if (v === "true" || v === "false") v = v === "true";
      else if (/^-?\d+(\.\d+)?$/.test(v) && k !== "str") v = parseFloat(v);
      args[el.getAttribute("data-p")] = v;
    }
    var out = $("cOut");
    if (bad) { out.innerHTML = '<p class="muted">' + esc(bad) + "</p>"; return; }
    B.api("/api/solver/calc", { body: { tool: c.name, args: args } }).then(function (r) {
      if (!r.ok) { out.innerHTML = '<div class="warnbox">' + esc(r.error || "That didn’t work.") + "</div>"; return; }
      var rows = "";
      Object.keys(r.result).forEach(function (k) {
        var v = r.result[k];
        rows += '<div class="result small"><b>' + esc(k.replace(/_/g, " ")) + ":</b> " + esc(typeof v === "object" ? JSON.stringify(v) : String(v)) + "</div>";
      });
      out.innerHTML = rows;
    }).catch(function () { out.innerHTML = '<p class="muted">The Badger didn’t answer.</p>'; });
  }
  function calcInit() {
    var sel = $("cPick");
    if (!sel) return;
    B.api("/api/solver/calculators").then(function (r) {
      calcs = r.calculators || [];
      sel.innerHTML = '<option value="">Choose a calculator…</option>' + calcs.map(function (c, i) {
        return '<option value="' + i + '">' + esc(c.group + " · " + c.name.replace(/_/g, " ")) + "</option>";
      }).join("");
    });
    sel.onchange = function () {
      var c = calcs && calcs[parseInt(sel.value, 10)];
      $("cForm").innerHTML = c ? calcForm(c) : "";
      if (c) $("cGo").onclick = function () { calcRun(c); };
    };
  }

  // ── admin: AI models ──
  function modelsHtml(m) {
    var mem = m.memory || {};
    var out = '<p class="small">Loaded now: <b>' + esc(m.loaded || "none") + "</b> · memory " +
      (mem.total_mb ? esc((mem.available_mb / 1024).toFixed(1)) + " GB free of " + esc((mem.total_mb / 1024).toFixed(1)) + " GB" : "unknown") +
      (m.endurance ? ' · <span class="pill warn">battery-saving mode</span>' : "") + "</p>";
    if (!m.switching) out += '<div class="warnbox">' + esc(m.switch_note || "Switching models needs the system helper's ai-model action.") + "</div>";
    m.roles.forEach(function (r) {
      var name = function (id) {
        for (var i = 0; i < r.candidates.length; i++) if (r.candidates[i].id === id) return r.candidates[i].title;
        return id || "–";
      };
      out += '<h3 class="section">' + esc(r.label) + "</h3><p class=\"small\">Active: <b>" + esc(name(r.active)) + "</b>" +
        (r.previous ? ' · previous: ' + esc(name(r.previous)) + ' <button class="btn ghost" data-op="rollback" data-role="' + esc(r.role) + '">Roll back</button>' : "") + "</p>";
      if (!r.candidates.length) { out += '<p class="small muted">No model for this role in the catalog yet.</p>'; return; }
      r.candidates.forEach(function (c) {
        var b = c.bench;
        out += '<div class="result small"><b>' + esc(c.title) + "</b> <span class=\"muted\">" + esc(c.size_gb) + " GB · " + esc(c.license) +
          (c.verified ? "" : ' · <span class="pill warn">checksum not verified yet</span>') + "</span><br>" +
          (c.installed ? "Downloaded" + (c.installed.version ? " (version " + esc(c.installed.version) + ")" : "") +
            (c.installed.previous_version ? ", previous version kept" : "") : "Not downloaded") +
          (b ? " · self-test " + esc(b.score) + "/" + esc(b.total) + ", " + esc(b.tps) + " tokens/s" : " · not tested") +
          (c.fits === false ? '<br><span class="muted">' + esc(c.fit_note) + "</span>" : "") +
          (c.installed ? '<div class="btns"><button class="btn ghost" data-op="bench" data-id="' + esc(c.id) + '">Run self-test</button>' +
            (r.active !== c.id ? '<button class="btn ghost" data-op="select" data-role="' + esc(r.role) + '" data-id="' + esc(c.id) + '">Make active</button>' : "") +
            '<button class="btn ghost" data-op="load" data-id="' + esc(c.id) + '">Load now</button></div>' : "") + "</div>";
      });
    });
    out += '<p class="small muted">Self-test: ' + esc((m.benchmark || []).map(function (p) { return p.topic; }).join(", ")) +
      '. A model only becomes active if it scores at least as well as the current one.</p><p class="small" id="mMsg"></p>';
    return out;
  }
  function modelsInit() {
    var box = $("sModels");
    if (!box) return;
    B.api("/api/solver/models").then(function (m) {
      if (m._status !== 200) { box.innerHTML = '<p class="muted">' + esc(m.error || "Not available.") + "</p>"; return; }
      box.innerHTML = modelsHtml(m);
      var btns = box.querySelectorAll("button[data-op]");
      for (var i = 0; i < btns.length; i++) btns[i].onclick = (function (b) {
        return function () {
          var op = b.getAttribute("data-op"), body = { op: op, role: b.getAttribute("data-role") || undefined, id: b.getAttribute("data-id") || undefined };
          b.disabled = true;
          B.api("/api/solver/models", { body: body }).then(function (r) {
            var msg = $("mMsg");
            if (r.ok === false || r._status !== 200) {
              msg.textContent = r.error || "That didn’t work.";
              if (op === "select" && /force/.test(r.error || "") && window.confirm("Make it active anyway?")) {
                body.force = true;
                B.api("/api/solver/models", { body: body }).then(function () { modelsInit(); });
              }
              b.disabled = false; return;
            }
            if (op === "bench" && r.id) { msg.textContent = "Self-test queued. Progress shows above."; showJob(r.id); return; }
            modelsInit();
          });
        };
      })(btns[i]);
    });
  }

  B.register("/solver", function () {
    stopPolling();
    B.h('<h1>🧮 Problem solver</h1><p class="muted">Loading…</p>');
    B.api("/api/solver/info").then(function (info) {
      if (info._status !== 200) {
        B.h('<h1>🧮 Problem solver</h1><p class="muted">' + esc(info.error || "Not available.") + "</p>");
        return;
      }
      var out = '<h1>🧮 Problem solver</h1><p class="muted">For real problems: power and solar, water and pipes, structures and ropes, heat and fuel, radio, maths, games and decisions, simple code, and survival planning. It plans, looks things up in the library, works every number out with calculators, checks the result, and shows its working.</p>' +
        '<div class="warnbox">The AI runs on this small box: a hard problem can take several minutes. It can still be wrong — read the working and the checks, and never rely on it alone for medical doses or anything life-critical.</div>';
      if (!info.enabled) out += '<div class="card section"><p>The problem solver is switched off on this Badger. The calculators below still work.</p></div>';
      else {
        out += '<div class="card section"><p><label for="sProblem"><b>Your problem</b></label></p>' +
          '<textarea id="sProblem" rows="5" style="width:100%" maxlength="' + esc(info.max_problem) + '" placeholder="Describe it with the numbers and units you know…"></textarea>' +
          '<div class="btns"><button class="btn" id="sGo"' + (info.ai ? "" : " disabled") + '>Solve</button></div>' +
          '<p class="small muted" id="sMsg">' + (info.ai ? (info.model ? "Model: " + esc(info.model) + ". " : "") : "The AI model isn’t running right now — the calculators below still work.") + "</p>" +
          (info.supplies ? '<p class="small muted">As a family member, the solver may read your Supplies list (read-only) when it helps, e.g. for rationing.</p>' : "") +
          '<details><summary class="small">Examples</summary><div id="sEx"></div></details></div>';
      }
      out += '<div class="card section" id="sJob" style="display:none"></div>';
      if (info.recent && info.recent.length) out += '<div class="card section"><h2>Your recent problems</h2>' + info.recent.map(function (r) {
        return '<div class="result small"><a href="#/solver" data-job="' + esc(r.id) + '">' + esc(r.problem) + '</a> <span class="pill">' + esc(STATE[r.state] || r.state) + "</span></div>";
      }).join("") + "</div>";
      out += '<div class="card section"><h2>Calculators</h2><p class="small muted">Use them directly, no AI needed.</p><select id="cPick" style="width:100%"></select><div id="cForm" class="section"></div></div>';
      if (info.admin) out += '<div class="card section"><h2>Admin: AI models</h2><div id="sModels"><p class="muted">Loading…</p></div>' +
        '<p><label><input type="checkbox" id="sOn"' + (info.enabled ? " checked" : "") + '> Problem solver switched on</label></p>' +
        '<p class="small muted">Python runner: ' + esc(info.python ? "on (sandbox verified)" : "off — " + (info.python_note || "")) + "</p></div>";
      B.h(out);
      var go = $("sGo"); if (go) go.onclick = submit;
      var ex = $("sEx");
      if (ex) {
        ex.innerHTML = EXAMPLES.map(function (t, i) { return '<div class="result small"><a href="#/solver" data-ex="' + i + '">' + esc(t) + "</a></div>"; }).join("");
        var links = ex.querySelectorAll("a[data-ex]");
        for (var i = 0; i < links.length; i++) links[i].onclick = function (e) {
          e.preventDefault(); $("sProblem").value = EXAMPLES[parseInt(this.getAttribute("data-ex"), 10)]; $("sProblem").focus();
        };
      }
      var jl = document.querySelectorAll("a[data-job]");
      for (var k = 0; k < jl.length; k++) jl[k].onclick = function (e) { e.preventDefault(); store(KEY, this.getAttribute("data-job")); showJob(this.getAttribute("data-job")); };
      calcInit();
      modelsInit();
      var on = $("sOn");
      if (on) on.onchange = function () { B.api("/api/solver/settings", { body: { enabled: on.checked } }); };
      var saved = load(KEY);
      if (saved) showJob(saved);
    }).catch(function () { B.h('<h1>🧮 Problem solver</h1><p class="muted">Couldn’t reach the Badger.</p>'); });
  });
})();
