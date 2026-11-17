/* Honey Badger launcher — vanilla JS, no build step, no network beyond the Badger.
   Written for old browsers too (no optional chaining / nullish coalescing). */
(function () {
  "use strict";
  if (window.NodeList && !NodeList.prototype.forEach) NodeList.prototype.forEach = Array.prototype.forEach;

  var ICONS = { reference: "📚", medical: "⛑️", survival: "🔥", "food-water": "💧", "energy-repair": "🔧",
    comms: "📻", maps: "🗺️", learn: "🎓", books: "📖", video: "🎬", ai: "💬", tools: "🧰", files: "💾" };
  // Sign-ins live in HttpOnly cookies the Badger sets: no script on this origin (not even another
  // app under /ai/ or /learn/) can read them. Web storage only keeps non-secret hints for the UI.
  var state = { lib: null, status: null, admin: sessionGet("isAdmin") === "1", member: null };
  var view = document.getElementById("view");

  // ── storage helpers (every access guarded: private mode, iOS quirks) ──
  function lsGet(k, d) { try { var v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function sessionGet(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
  function lsDel(k) { try { localStorage.removeItem(k); } catch (e) {} }
  function sessionSet(k, v) { try { if (v === null) sessionStorage.removeItem(k); else sessionStorage.setItem(k, v); } catch (e) {} }

  // ── tiny DOM helpers ──
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  // Any URL that came from the server (catalog, kiwix search, USB names) goes
  // through here before it becomes an href: relative links and http(s) only, so
  // a "javascript:" or "data:" URL can't run script when tapped.
  function safeUrl(u) {
    u = String(u == null ? "" : u);
    // Browsers drop control characters, spaces, tabs and newlines before reading the scheme.
    var m = /^([a-z][a-z0-9+.\-]*):/i.exec(u.replace(/[\u0000-\u0020\u007f]+/g, ""));
    return m && !/^https?$/i.test(m[1]) ? "#" : u;
  }
  function href(u) { return esc(safeUrl(u)); }
  function num(n) { n = Number(n); return isFinite(n) ? n : 0; }
  function h(html) { view.innerHTML = html; view.focus(); window.scrollTo(0, 0); }
  function human(n) { if (n == null) return "?"; var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; } return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + " " + u[i]; }
  function gb(x) { return x >= 1 ? x.toFixed(x < 10 ? 1 : 0) + " GB" : Math.round(x * 1000) + " MB"; }
  function tfmt(v, o) { return window.NuTime ? NuTime.fmt(v, o) : String(v); }
  function tickClock() { var el = document.getElementById("clocktxt"); if (el) el.textContent = tfmt(Date.now(), { short: false }); }
  setInterval(tickClock, 864);                         // one Nu:Time™ tick
  document.addEventListener("click", function (e) {
    var a = e.target.closest ? e.target.closest("#clockbit") : null;
    if (!a || !window.NuTime) return;
    e.preventDefault(); NuTime.cycle(); tickClock(); route();          // re-render the page in the new format
  });
  function ago(iso) { if (!iso) return "never"; var s = (Date.now() - Date.parse(iso)) / 1000;
    if (isNaN(s)) return iso; if (s < 90) return "just now"; if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 129600) return Math.round(s / 3600) + " h ago"; return Math.round(s / 86400) + " days ago"; }

  // credentials: "same-origin" — older Safari leaves cookies out of fetch() unless told.
  function asJson(r) {
    return r.json().catch(function () { return {}; }).then(function (j) { j = j || {}; j._status = r.status; noteAuth(j, r.status); return j; });
  }
  function noteAuth(j, status) {                       // keep the UI hints in step with what the Badger says
    if (status === 401 && j.key === "unknown") { lsDel("isMember"); }
    if (status === 401 && state.admin && j.error === "admin login required") { state.admin = false; sessionSet("isAdmin", null); }
  }
  function api(path, opts) {
    opts = opts || {};
    var headers = { "Content-Type": "application/json" };
    if (opts.upload) {                                   // raw file upload for a feature route marked raw
      headers = { "Content-Type": opts.type || "application/octet-stream", "X-Badger-Upload": "1" };
      return fetch(path, { method: "POST", headers: headers, body: opts.upload, credentials: "same-origin" }).then(asJson);
    }
    return fetch(path, { method: opts.method || (opts.body ? "POST" : "GET"), headers: headers, credentials: "same-origin",
      body: opts.body ? JSON.stringify(opts.body) : undefined }).then(asJson);
  }
  // Older launchers kept the member key in localStorage, where any script on this origin could read
  // it. Hand it to the Badger once (it answers with the HttpOnly cookie), then forget it — but only
  // when that worked, or when the Badger says the key is unknown (never because it was unreachable).
  (function migrateOldKeys() {
    sessionSet("adminToken", null);                    // an old admin session: just sign in again
    var old = lsGet("memberKey", "");
    if (!old) return;
    fetch("/api/members/adopt", { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Member-Token": old }, body: "{}" })
      .then(asJson).then(function (r) {
        if (r.ok) { lsDel("memberKey"); lsSet("isMember", "1"); }
        else if (r.key === "unknown") { lsDel("memberKey"); lsDel("isMember"); }
      }).catch(function () {});
  })();
  function refreshWho() {                              // is this device a member / an admin? (from the cookies)
    return api("/api/members/me").then(function (r) {
      if (r.member) lsSet("isMember", "1"); else if (r.key === "unknown" || r._status === 401) lsDel("isMember");
      if (r.admin === true || r.admin === false) { state.admin = r.admin; sessionSet("isAdmin", r.admin ? "1" : null); }
      approvalPrompt(r.approvals || []);
      return r;
    }).catch(function () { return {}; });
  }
  // A device someone (the admin) added to THIS member waits until one of the member's own devices says yes.
  function approvalPrompt(list) {
    var box = document.getElementById("approvals");
    if (!list.length) { if (box) box.parentNode.removeChild(box); return; }
    if (!box) {
      box = document.createElement("div"); box.id = "approvals";
      view.parentNode.insertBefore(box, view);
    }
    box.innerHTML = list.map(function (a) {
      return '<div class="card warnbox" style="margin:10px"><p><b>A new device wants to join as you:</b> “' + esc(a.label) + "”" +
        (a.created ? " (" + esc(tfmt(a.created * 1000, { date: true, short: true })) + ")" : "") + ".</p>" +
        '<p class="small">Someone with the admin PIN added it to your name. Only say yes if it is yours (or you asked for it). If not, say no and tell your family.</p>' +
        '<div class="btns"><button class="btn" data-appr="' + esc(a.device) + '" data-yes="1">Yes, it’s mine</button>' +
        '<button class="btn danger" data-appr="' + esc(a.device) + '" data-yes="">No, remove it</button></div></div>';
    }).join("");
    box.querySelectorAll("[data-appr]").forEach(function (b) { b.onclick = function () {
      var yes = !!b.getAttribute("data-yes");
      api("/api/members/approve", { body: { device: b.getAttribute("data-appr"), approve: yes } }).then(function (r) {
        if (!r.ok) alert(r.error || "That didn’t work.");
        refreshWho();
      });
    }; });
  }

  // ── theme & text size ──
  function applyPrefs() {
    var t = lsGet("theme", "auto");
    if (t === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
    document.documentElement.setAttribute("data-text", lsGet("text", "normal"));
  }
  applyPrefs();

  // ── renderer choice: viewer's own pick (localStorage) beats the device default ──
  function pickRenderer(item) {
    var mine = lsGet("rend:" + item.kind, null);
    var list = item.renderers || [];
    for (var i = 0; i < list.length; i++) if (list[i].id === mine) return list[i];
    for (i = 0; i < list.length; i++) if (list[i].id === item.default_renderer) return list[i];
    return list[0];
  }

  function itemCard(it) {
    var r = it.installed ? pickRenderer(it) : null;
    var others = (it.renderers || []).map(function (o) {
      return '<a href="' + href(o.url) + '" data-kind="' + esc(it.kind) + '" data-rend="' + esc(o.id) + '">' +
        (r && o.id === r.id ? "● " : "○ ") + esc(o.name) + (o.note ? '<span class="note">' + esc(o.note) + "</span>" : "") + "</a>";
    }).join("");
    return '<div class="card"><div class="row"><div style="flex:1;min-width:200px">' +
      "<h3>" + esc(it.title) + "</h3>" +
      (it.description ? "<p class=\"small\">" + esc(it.description) + "</p>" : "") +
      '<div class="meta">' + esc(it.kind_label) + " · " + gb(it.size_gb) +
      (it.version ? " · " + esc(it.version) : "") + (it.lang && it.lang !== "eng" && it.lang !== "zxx" && it.lang !== "mul" ? " · " + esc(it.lang) : "") +
      (it.use === "personal" ? ' <span class="pill" title="' + esc("Licensed for personal, non-commercial use only: " + (it.license || "see its licence")) + '">Personal-use licence</span>' : "") + "</div></div>" +
      '<div class="btns">' + (r ? '<a class="btn" href="' + href(r.url) + '">Open</a>' : it.installed ?
        '<span class="pill warn" title="The app that opens this is stopped or not installed. An admin can start it under Settings → Services.">Installed — viewer offline</span>' :
        it.held ? '<a class="pill warn" href="#/settings" title="Licensed for personal use only. It downloads once the Badger’s owner accepts that licence in Settings.">Waiting for the owner’s OK</a>' :
        '<span class="pill warn">Not downloaded yet</span>') +
      (it.renderers && it.renderers.length > 1 ? '<details class="chooser"><summary class="btn ghost" aria-label="Open with…">Open with ▾</summary><div class="opts">' + others +
        '<label class="small" style="font-weight:400"><input type="checkbox" class="remember" data-kind="' + esc(it.kind) + '"> Remember on this device for all ' + esc(it.kind_label) + "</label></div></details>" : "") +
      "</div></div></div>";
  }

  // Clicking an "Open with" option remembers the choice if the box is ticked.
  document.addEventListener("click", function (e) {
    var a = e.target.closest ? e.target.closest("a[data-rend]") : null;
    if (!a) return;
    var box = a.parentNode.querySelector(".remember");
    if (box && box.checked) lsSet("rend:" + a.getAttribute("data-kind"), a.getAttribute("data-rend"));
  });

  // ── data ──
  function loadLib(force) {
    if (state.lib && !force) return Promise.resolve(state.lib);
    return api("/api/library").then(function (j) {
      // Never cache an error reply: the next view would try to render it.
      if (j._status !== 200 || !j.categories) throw new Error(j.error || "the Badger answered with an error");
      state.lib = j; return j;
    });
  }
  function libFailed(e) {
    h('<div class="card"><h2>Couldn’t load the library</h2><p class="muted">' + esc((e && e.message) || "No answer from the Badger.") +
      '</p><p class="small">Check that you are on the Badger’s Wi-Fi. If it just started, give it a minute.</p>' +
      '<div class="btns"><button class="btn" id="retryLib">Try again</button></div></div>');
    var b = document.getElementById("retryLib");
    if (b) b.onclick = function () { state.lib = null; route(); };
  }

  function refreshStatus() {
    api("/api/status").then(function (s) {
      state.status = s;
      if (window.NuTime && s.clock) { NuTime.config.tz = s.clock.tz; NuTime.config.reference = s.clock.reference; NuTime.config.display = s.clock.display || "local"; }
      var bits = ['<a href="#" id="clockbit" title="Tap to switch Local · UTC · Nu:Time™">🕒 <b id="clocktxt"></b></a>'];
      var p = s.power;
      if (p && p.percent != null) bits.push((p.charging ? "⚡" : "🔋") + " <b>" + Math.round(num(p.percent)) + "%</b>" +
        (p.runtime_min ? " · " + Math.round(num(p.runtime_min) / 60 * 10) / 10 + " h" : ""));
      if (p && p.input_w != null && p.input_v > 1) bits.push("☀ <b>" + Math.round(num(p.input_w)) + " W</b> in");
      if (s.clients != null) bits.push("📶 <b>" + num(s.clients) + "</b> connected");
      if (s.storage) bits.push("💾 " + esc(human(num(s.storage.free))) + " free");
      if (s.cpu_temp_c != null) bits.push("🌡 " + Math.round(num(s.cpu_temp_c)) + "°C");
      if (s.build && s.build.phase && s.build.phase !== "done") bits.push('<a href="#/status">⏳ building: ' + esc(s.build.phase) + " " + num(s.build.pct) + "%</a>");
      else if (s.updates && s.updates.counts && (s.updates.counts.update || s.updates.counts.install))
        bits.push('<a href="#/status">↑ ' + (num(s.updates.counts.update) + num(s.updates.counts.install)) + " updates</a>");
      var sd = s.sdr;
      if (sd && sd.weather_radio === "paused")
        bits.push('<a href="#/aircraft" class="warnpill">📻 Weather alerts paused' + (sd.by ? " (" + esc(sd.by) + ")" : "") +
          (sd.until ? " until " + esc(tfmt(sd.until * 1000, { short: true })) : "") + "</a>");
      if (sd && (sd.sdr2 === "missing" || sd.sdr2 === "failed")) bits.push('<a href="#/aircraft">SDR 2 not working</a>');
      if (s.posture && s.posture.label)            // the security posture (family, admin, panel only)
        bits.push('<a href="#/posture"' + (s.posture.level === "lockdown" ? ' class="warnpill"' : "") + ">🛡 " + esc(s.posture.label) + "</a>");
      bits.push('<a href="#/status">status</a>');
      document.getElementById("statusbar").innerHTML = bits.join("<span aria-hidden=true>·</span>");
      var al = s.alerts || [];
      document.getElementById("alerts").innerHTML = al.map(function (a) {
        return '<p class="alert" role="alert">⚠ ' + esc(a.event || "Weather alert") + " — " + esc(a.areas || "") +
          "<small>" + esc(a.text || "") + (a.expires_ts ? " · until " + esc(tfmt(a.expires_ts, { short: true })) : a.expires ? " · until " + esc(a.expires) : "") + " · NOAA Weather Radio</small></p>";
      }).join("");
      if (featIds.sensors) api("/api/sensors/alerts").then(function (r) {     // flood / freeze / radiation from the sensor station
        var box = document.getElementById("alerts"); if (!box || !r || !r.alerts) return;
        box.insertAdjacentHTML("beforeend", r.alerts.map(function (a) {
          return '<p class="alert" role="alert"><a href="#/sensors" style="color:#fff">⚠ ' + esc(a.text) + "</a><small>Badger sensors" +
            (a.since ? " · since " + esc(tfmt(a.since * 1000, { short: true })) : "") + (a.stale ? " · reading is old" : "") + "</small></p>";
        }).join(""));
      }).catch(function () {});
    }).catch(function () {
      document.getElementById("statusbar").innerHTML = "<b>Badger not reachable</b> — are you on the Badger’s Wi-Fi?";
    });
  }

  // ── personal-use (non-commercial) content: a Badger built for sale offers it to its owner ──
  var PERSONAL_INTRO = "Some free courses and repair guides (iFixit, Khan Academy, LibreTexts, TED, …) are licensed for " +
    "personal, non-commercial use only. They aren’t included on the Badger as sold.";
  function loadPersonal() {
    return api("/api/content/personal").then(function (pc) {
      if (pc._status !== 200 || !pc.items) throw new Error(pc.error || "no answer");
      state.personal = pc; return pc;
    });
  }
  function personalSig(pc) { return (pc.pending || []).join(","); }
  function personalHidden(pc) { return lsGet("personalHide", "") === personalSig(pc); }
  // The offer: every pending item with its size and licence; the admin can accept or put it off.
  function personalOffer(pc, firstRun) {
    var pend = {}; (pc.pending || []).forEach(function (id) { pend[id] = true; });
    var rows = pc.items.filter(function (i) { return pend[i.id]; });
    var admin = !!pc.admin && state.admin;
    var out = (firstRun ? '<p class="small muted">Setup step</p>' : "") +
      "<p>" + esc(PERSONAL_INTRO) + " <b>Download them now for your own personal use?</b></p>" +
      '<table class="files">' + rows.map(function (i) {
        return "<tr><td>" + (admin ? '<label style="font-weight:400"><input type="checkbox" class="pcPick" data-pid="' + esc(i.id) + '" checked> ' : "") +
          esc(i.title) + (admin ? "</label>" : "") + '<div class="small muted">' + esc(i.license) +
          (i.manual ? " · you copy this one in yourself (see the manual)" : "") + '</div></td><td class="sz">' + (i.manual ? "—" : esc(gb(num(i.size_gb)))) + "</td></tr>";
      }).join("") + "</table>" +
      '<p class="small muted">About ' + esc(gb(num(pc.pending_gb))) + " to download in all. Items that don’t fit in the free space are skipped and listed on the Status page. " +
      "With no internet right now, they download the next time the Badger is online. This is a summary, not legal advice: each item’s own licence applies.</p>";
    if (!admin) return out + '<p class="small">The Badger’s owner can accept this under <a href="#/settings">Settings → Badger administration</a>.</p>';
    return out + '<label style="font-weight:400"><input type="checkbox" id="pcAgree"> I’ll use these for personal, non-commercial purposes only.</label>' +
      '<label for="pcName">Your name (optional, kept with the record of this choice)</label><input type="text" id="pcName" maxlength="64" autocomplete="name">' +
      '<div class="btns" style="margin-top:10px"><button class="btn" id="pcAccept">Accept &amp; download</button>' +
      (firstRun || !pc.later_at ? '<button class="btn ghost" id="pcLater">Not now</button>' : "") + '</div><p class="small" id="pcMsg" role="status"></p>';
  }
  function wirePersonal(box, after) {
    var msg = box.querySelector("#pcMsg");
    function say(t) { if (msg) msg.textContent = t; }
    function sent(r) {
      if (r._status === 401) { state.admin = false; sessionSet("isAdmin", null); }
      say(r.message || r.error || (r.ok ? "Done." : "Failed"));
      if (r.ok) { state.lib = null; if (r.personal) state.personal = r.personal; if (after) after(r); }
    }
    var acc = box.querySelector("#pcAccept"), later = box.querySelector("#pcLater"), rm = box.querySelector("#pcRemove");
    if (acc) acc.onclick = function () {
      var ids = []; box.querySelectorAll(".pcPick").forEach(function (c) { if (c.checked) ids.push(c.getAttribute("data-pid")); });
      if (!ids.length) { say("Tick at least one item, or choose Not now."); return; }
      if (!box.querySelector("#pcAgree").checked) { say("Tick the box to agree to personal, non-commercial use first."); return; }
      say("Saving…");
      api("/api/admin/personal", { body: { action: "accept", agree: true, items: ids, name: box.querySelector("#pcName").value } }).then(sent);
    };
    if (later) later.onclick = function () { say("Saving…"); api("/api/admin/personal", { body: { action: "later" } }).then(sent); };
    if (rm) rm.onclick = function () {
      if (!confirm("Delete the personal-use courses and guides from this Badger? You can download them again later.")) return;
      say("Removing…"); api("/api/admin/personal", { body: { action: "remove" } }).then(sent);
    };
  }
  // A notice for the home and Status pages: the same offer, folded away, and hideable on this device.
  function personalNotice(slotId) {
    loadPersonal().then(function (pc) {
      var slot = document.getElementById(slotId);
      if (!slot || !pc.offer || personalHidden(pc)) return;
      slot.innerHTML = '<div class="card warnbox"><b>' + (pc.offer === "first-run" ? "Finish setting up this Badger: " : "") +
        "Personal-use courses and repair guides are available</b> (" + num((pc.pending || []).length) + " items, about " + esc(gb(num(pc.pending_gb))) + ")." +
        '<details><summary class="small">See the list</summary>' + personalOffer(pc, false) + "</details>" +
        '<div class="btns"><a class="btn ghost" href="#/settings">Review in Settings</a><button class="btn ghost" id="pcHide">Hide this notice</button></div></div>';
      wirePersonal(slot, function (r) { personalNotice(slotId); if (r.message) alert(r.message); });
      document.getElementById("pcHide").onclick = function () { lsSet("personalHide", personalSig(pc)); slot.innerHTML = ""; };
    }).catch(function () {});
  }
  // Settings: the full picture — the offer, or what was accepted (and a way to remove it).
  function personalCard(firstLogin, note) {
    var el = document.getElementById("personalCard");
    if (!el) return;
    loadPersonal().then(function (pc) {
      var admin = !!pc.admin && state.admin, acc = pc.accepted, out = "<h2>Personal-use content</h2>";
      if (pc.offer) out += personalOffer(pc, pc.offer === "first-run");
      else if (pc.provisioning === "owner" && !pc.withdrawn_at) out += '<p class="small muted">This Badger was built by its owner: courses and guides licensed for personal, non-commercial use are included with everything else. Don’t sell it with them on it.</p>';
      else out += '<p class="small muted">Nothing is waiting. ' + esc(PERSONAL_INTRO) + "</p>";
      if (acc && acc.items && acc.items.length) out += '<p class="small">Accepted for personal use' + (acc.by ? " by <b>" + esc(acc.by) + "</b>" : "") +
        (acc.at ? " on " + esc(tfmt(acc.at, { date: true, short: true })) : "") + ": " + num(acc.items.length) + " items (" + esc((acc.licences || []).join(", ")) + ").</p>";
      if (admin && (pc.installed || []).length) out += '<div class="btns"><button class="btn ghost" id="pcRemove">Remove personal-use content</button></div>' + (pc.offer ? "" : '<p class="small" id="pcMsg" role="status"></p>');
      if (pc.withdrawn_at) out += '<p class="small muted">Removed on ' + esc(tfmt(pc.withdrawn_at, { date: true, short: true })) + ".</p>";
      if (note) out += '<p class="small"><b>' + esc(note) + "</b></p>";
      el.innerHTML = out;
      wirePersonal(el, function (r) { personalCard(false, r.message); });
      if (firstLogin && pc.offer === "first-run" && admin && el.scrollIntoView) el.scrollIntoView();
    }).catch(function () { el.innerHTML = '<p class="small muted">Couldn’t check personal-use content.</p>'; });
  }

  // ── views ──
  function home() {
    loadLib().then(function (lib) {
      var cats = lib.categories.map(function (c) {
        var href = c.id === "tools" ? "#/tools" : c.id === "ai" ? "#/ask" : c.id === "files" ? "#/files" : "#/c/" + c.id;
        var count = c.id === "tools" ? lib.tools.length + " tools" : c.count ? num(c.count) + (c.count === 1 ? " collection" : " collections") : "";
        return '<a class="tile" href="' + href + '"><span class="ico" aria-hidden="true">' + (ICONS[c.id] || "•") +
          "</span><b>" + esc(c.title) + '</b><span class="muted">' + esc(c.blurb) + '</span><span class="count">' + count + "</span></a>";
      }).join("");
      h('<div class="quick">' +
        '<a href="#/ask">💬 Ask the library</a>' +
        '<a class="alt" href="#/c/medical">⛑️ First aid & medical</a>' +
        '<a class="alt" href="/maps/">🗺️ Open map</a>' +
        '<a class="alt" href="#/board">📋 Check-in board</a><span id="aidLink"></span></div><div id="personalBanner"></div>' +
        '<div class="grid">' + cats + "</div>" + featureTiles() +
        '<p class="small muted section">On a phone, iPad or laptop? <a href="#/devices">Add the Badger to your home screen</a> — nothing to install, everything stays on the Badger.</p>');
      personalNotice("personalBanner");
      if (featIds.aidstation) api("/api/aidstation/status").then(function (st) {   // neighbourhood aid station open?
        var el = document.getElementById("aidLink");
        if (el && st && st.set && st.open && !st.stale) el.innerHTML = '<a class="alt" href="#/aid">⛑️ Aid station open</a>';
      }).catch(function () {});
    }).catch(libFailed);
  }

  function category(id) {
    loadLib().then(function (lib) {
      var cat = lib.categories.filter(function (c) { return c.id === id; })[0];
      var items = lib.items.filter(function (i) { return i.category === id; });
      items.sort(function (a, b) { return (b.installed - a.installed) || (a.priority - b.priority) || a.title.localeCompare(b.title); });
      h('<div class="crumbs"><a href="#/">Home</a> › ' + esc(cat ? cat.title : id) + "</div>" +
        "<h1>" + (ICONS[id] || "") + " " + esc(cat ? cat.title : id) + "</h1>" +
        (id === "medical" ? '<div class="warnbox">Reference only. In an emergency, stop bleeding, keep the airway open, and get trained help if there is any way to reach it.</div>' : "") +
        (id === "comms" ? '<p><a class="btn ghost" href="/tools/radio.html">📻 Frequency reference</a> <a class="btn ghost" href="/tools/morse.html">Morse code</a> <a class="btn ghost" href="#/board">Mesh / check-in board</a></p>' : "") +
        '<div class="list">' + (items.length ? items.map(itemCard).join("") : '<p class="muted">Nothing here yet.</p>') + "</div>");
    }).catch(libFailed);
  }

  function itemView(id, path) {
    api("/api/files?item=" + encodeURIComponent(id) + "&path=" + encodeURIComponent(path || "")).then(function (d) {
      if (d._status !== 200) return h('<p class="muted">Not found or not downloaded yet.</p>');
      var parts = (path || "").split("/").filter(Boolean), acc = "";
      var crumbs = '<a href="#/">Home</a> › <a href="#/item/' + esc(id) + '">' + esc(d.title) + "</a>" +
        parts.map(function (p) { acc += (acc ? "/" : "") + p; return ' › <a href="#/item/' + esc(id) + "?path=" + encodeURIComponent(acc) + '">' + esc(p) + "</a>"; }).join("");
      var rows = d.entries.map(function (e) {
        if (e.dir) return '<tr><td>📁 <a href="#/item/' + esc(id) + "?path=" + encodeURIComponent(e.path) + '">' + esc(e.name) + "</a></td><td></td><td></td></tr>";
        var mine = lsGet("rend:" + e.kind, e.default_renderer), r = e.renderers[0];
        e.renderers.forEach(function (x) { if (x.id === mine) r = x; });
        return "<tr><td>" + esc(e.name) + '</td><td class="sz">' + esc(human(num(e.size))) + '</td><td style="text-align:right"><a class="btn ghost" href="' + href(r.url) + '">Open</a></td></tr>';
      }).join("");
      h('<div class="crumbs">' + crumbs + "</div><h1>" + esc(d.title) + '</h1><table class="files">' + (rows || '<tr><td class="muted">Empty</td></tr>') + "</table>");
    });
  }

  function searchView(q) {
    document.getElementById("q").value = q;
    h('<h1>Results for “' + esc(q) + '”</h1><p class="muted">Searching every collection…</p>');
    api("/api/search?q=" + encodeURIComponent(q)).then(function (r) {
      var out = "<h1>Results for “" + esc(q) + "”</h1>";
      out += '<p><a class="btn ghost" href="#/ask?q=' + encodeURIComponent(q) + '">💬 Ask the library instead</a></p>';
      if (r.tools.length) out += '<h2 class="section">Tools</h2>' + r.tools.map(function (t) { return '<div class="result"><a href="' + href(t.url) + '">' + esc(t.title) + "</a></div>"; }).join("");
      if (r.collections && r.collections.length) out += '<h2 class="section">Collections</h2>' + r.collections.map(function (c) { return '<div class="result"><a href="#/c/' + esc(c.category) + '">' + esc(c.title) + "</a></div>"; }).join("");
      out += '<h2 class="section">Articles</h2>' + (r.articles.length ? r.articles.map(function (a) {
        return '<div class="result"><a href="' + href(a.url) + '">' + esc(a.title) + '</a><div class="src">' + esc(a.source) + "</div><div class=\"small\">" + esc(a.snippet) + "</div></div>";
      }).join("") : '<p class="muted">No articles matched. Try fewer or simpler words.</p>');
      if (r.files.length) out += '<h2 class="section">Documents, books & videos</h2>' + r.files.map(function (f) { return '<div class="result"><a href="' + href(f.url) + '">' + esc(f.title) + "</a></div>"; }).join("");
      h(out);
    });
  }

  function askView(q) {
    h('<h1>💬 Ask the library</h1>' +
      '<div class="warnbox">Answers are written by a small offline AI from articles on this Badger, with numbered sources. It can still be wrong — open the sources before acting, especially for medical questions.</div>' +
      '<form id="askForm"><textarea id="askQ" placeholder="e.g. How do I purify creek water without a filter?"></textarea>' +
      '<div class="btns" style="margin-top:8px"><button class="btn" id="askBtn">Ask</button><span class="small muted" id="askState"></span></div></form>' +
      '<h2 class="section">Answer</h2><div class="answer" id="ans"></div><h2 class="section">Sources</h2><div id="srcs" class="small muted">—</div>');
    var form = document.getElementById("askForm"), ta = document.getElementById("askQ");
    if (q) ta.value = q;
    form.onsubmit = function (e) {
      e.preventDefault();
      var question = ta.value.trim(); if (!question) return;
      var ans = document.getElementById("ans"), srcs = document.getElementById("srcs"), st = document.getElementById("askState"), btn = document.getElementById("askBtn");
      ans.textContent = ""; srcs.textContent = "Searching the library…"; st.textContent = "Thinking — about a minute on battery power."; btn.disabled = true;
      streamAsk(question, function (ev, data) {
        if (ev === "sources") srcs.innerHTML = data.length ? data.map(function (s) {
          return '<div class="result">[' + num(s.n) + '] <a href="' + href(s.url) + '">' + esc(s.title) + '</a> <span class="src">' + esc(s.source) + "</span></div>"; }).join("") : "No matching articles.";
        else if (ev === "token") ans.textContent += data;
        else if (ev === "done" || ev === "error") { st.textContent = ev === "error" ? "Connection lost." : ""; btn.disabled = false; }
      });
    };
    if (q) form.onsubmit({ preventDefault: function () {} });
  }

  // fetch + manual SSE parse (EventSource can't POST)
  function streamAsk(q, on) {
    fetch("/api/ask", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ q: q }) })
      .then(function (r) {
        if (!r.body || !r.body.getReader) return r.text().then(function (t) { parseSSE(t, on); on("done", {}); });
        var reader = r.body.getReader(), dec = new TextDecoder(), buf = "";
        function pump() {
          return reader.read().then(function (res) {
            if (res.done) { on("done", {}); return; }
            buf += dec.decode(res.value, { stream: true });
            var idx;
            while ((idx = buf.indexOf("\n\n")) >= 0) { parseSSE(buf.slice(0, idx), on); buf = buf.slice(idx + 2); }
            return pump();
          });
        }
        return pump();
      }).catch(function () { on("error", {}); });
  }
  function parseSSE(block, on) {
    block.split("\n\n").forEach(function (chunk) {
      var ev = "message", data = "";
      chunk.split("\n").forEach(function (l) { if (l.indexOf("event:") === 0) ev = l.slice(6).trim(); else if (l.indexOf("data:") === 0) data += l.slice(5).trim(); });
      if (data) { try { on(ev, JSON.parse(data)); } catch (e) {} }
    });
  }

  function boardView(which, code) {
    which = which === "family" ? "family" : "community";
    var tabs = '<div class="btns" role="tablist" style="margin-bottom:10px">' +
      '<a class="btn' + (which === "community" ? "" : " ghost") + '" role="tab" aria-selected="' + (which === "community") + '" href="#/board">Community</a>' +
      '<a class="btn' + (which === "family" ? "" : " ghost") + '" role="tab" aria-selected="' + (which === "family") + '" href="#/board/family">Family</a></div>';
    var intro = which === "family"
      ? '<p class="muted small">Only family members can read or post here. Posts go out under your member name. “Send over mesh” uses the family’s private, encrypted radio channel.</p>'
      : '<p class="muted small">Open to everyone on this Badger’s Wi-Fi: neighbors, visitors, anyone nearby. Don’t post anything private here. “Send over mesh” reaches any Meshtastic radio in range.</p>';
    h('<h1>📋 Check-in board</h1>' + tabs + intro + '<div id="bwrap"></div><div id="msgs" class="section"></div>');
    var wrap = document.getElementById("bwrap");

    function joinForm(msg) {
      wrap.innerHTML = '<form id="jf" class="card"><h2>Join the family board</h2>' +
        '<p class="small muted">Ask the Badger’s owner for an invite code (Settings → Badger administration → Family members). Each code works once, on one device, for 24 hours.</p>' +
        '<label for="jc">Invite code</label><input type="text" id="jc" autocomplete="off" autocapitalize="characters" placeholder="K7QX-M2PD-9Z" value="' + esc(code || "") + '">' +
        '<label for="jn">Your name</label><input type="text" id="jn" maxlength="32" autocomplete="name" value="' + esc(lsGet("boardName", "")) + '">' +
        '<label for="jd">This device</label><input type="text" id="jd" maxlength="40" placeholder="e.g. Ana’s phone">' +
        '<div class="btns" style="margin-top:8px"><button class="btn">Join</button><span id="js" class="small muted">' + esc(msg || "") + '</span></div></form>';
      document.getElementById("msgs").innerHTML = "";
      document.getElementById("jf").onsubmit = function (e) {
        e.preventDefault();
        api("/api/members/join", { body: { code: document.getElementById("jc").value, name: document.getElementById("jn").value,
          device: document.getElementById("jd").value } }).then(function (r) {
          if (r.ok && r.pending) { lsSet("boardName", r.member.name); wrap.innerHTML = '<div class="card"><p>' + esc(r.message || "This device is waiting for approval.") + "</p></div>"; return; }
          if (r.ok) { lsSet("isMember", "1"); lsSet("boardName", r.member.name); location.hash = "#/board/family"; route(); }
          else document.getElementById("js").textContent = r.error || "That didn’t work.";
        });
      };
    }

    function postForm(me, mesh, needsOk) {
      var nameField = which === "family" && me
        ? '<p class="small">Posting as <b>' + esc(me.name) + "</b></p>"
        : '<label for="bn">Your name</label><input type="text" id="bn" maxlength="32" value="' + esc(me ? me.name : lsGet("boardName", "")) + '">';
      wrap.innerHTML = '<form id="bf" class="card">' + nameField +
        '<label for="bt">Message</label><textarea id="bt" maxlength="2000" placeholder="' + (which === "family" ? "e.g. Ana: at the school gym with the kids, all OK." : "e.g. Water available at the church on Main St until 6 pm.") + '"></textarea>' +
        '<div class="btns" style="margin-top:8px"><button class="btn">Post</button>' +
        (mesh ? '<label style="margin:0;font-weight:400"><input type="checkbox" id="bm"> ' + (needsOk ? "Ask to send over mesh" : "Send over mesh") + "</label>" : "") +
        (mesh && needsOk ? '<span class="small muted">(a family member decides — the Badger’s radio is the household’s)</span>' : "") +
        (mesh && me ? '<a class="small" href="/maps/?mesh=1">Radios on the map</a>' : "") +
        '<span id="bs" class="small muted"></span></div></form>';
      document.getElementById("bf").onsubmit = function (e) {
        e.preventDefault();
        var nameEl = document.getElementById("bn"), name = nameEl ? nameEl.value : "", bm = document.getElementById("bm");
        if (nameEl) lsSet("boardName", name);
        api("/api/board", { body: { name: name, text: document.getElementById("bt").value, mesh: !!(bm && bm.checked), board: which } }).then(function (r) {
          document.getElementById("bs").textContent = r.message;
          if (r.ok) { document.getElementById("bt").value = ""; load(); }
        });
      };
    }

    function load() {
      api("/api/board?board=" + which).then(function (d) {
        if (d._status === 503) { wrap.innerHTML = '<p class="card">' + esc(d.error || "The family list can’t be read right now. Try again later.") + "</p>"; return; }
        if (d._status === 401) { if (d.key === "unknown") { lsDel("isMember"); return joinForm("This device is no longer a family member. Ask for a new invite."); }
          if (d.key === "pending") { wrap.innerHTML = '<div class="card"><p>' + esc(d.error) + "</p></div>"; document.getElementById("msgs").innerHTML = ""; return; }
          return joinForm(); }
        if (!document.getElementById("bf")) postForm(d.member, d.mesh && d.mesh_visitors !== false, d.mesh_needs_ok);
        pendingBox(d.mesh_pending);
        var sealNote = d.sealed && d.sealed.locked
          ? '<p class="card warnbox small">🔒 Family history is locked — the owner can unlock it (Settings → Sealed records).' +
            (d.messages.length ? " Below: what was posted since it locked" + (d.sealed.recent_hours ? " (the last " + num(d.sealed.recent_hours) + " hours at most)" : "") + "." : "") +
            " New posts are saved sealed.</p>" : "";
        document.getElementById("msgs").innerHTML = sealNote + d.messages.slice().reverse().map(function (m) {
          // ✓ only for posts a family member made here; the Badger's own notes get a neutral mark;
          // radio posts are never "verified" (a radio's ID can be faked on the air).
          var badge = m.system ? ' <span class="pill" title="An automatic note from the Badger">Badger</span>' :
            m.removed_member ? ' <span class="pill">removed member</span>' : (m.verified ? ' <span class="pill ok" title="Family member">✓</span>' : "");
          var via = m.via === "mesh-in" ? (which === "family" ? " · via the family radio channel 📡" : " · via mesh 📡 (anyone with a radio; name not checked)") :
            m.via === "mesh-out" ? " · sent to mesh" : m.mesh === "pending" ? " · waiting for a family member to send it over the radio" :
            m.mesh === "declined" ? " · not sent over the radio" : "";
          return '<div class="msg"><span class="who">' + esc(m.name) + badge + '</span><span class="when">' + esc(tfmt(m.at, { date: true, short: true })) +
            esc(via) + "</span><div>" + esc(m.text) + "</div></div>";
        }).join("") || (sealNote ? "" : '<p class="muted">No messages yet.</p>');
      });
    }
    // Visitors' "send over mesh" requests, for family members and the admin: send or keep on the board.
    function pendingBox(list) {
      var box = document.getElementById("mpend");
      if (!box) { wrap.insertAdjacentHTML("afterend", '<div id="mpend"></div>'); box = document.getElementById("mpend"); }
      if (!list || !list.length) { box.innerHTML = ""; return; }
      box.innerHTML = '<div class="card warnbox"><h2>Waiting to go out over the radio</h2><p class="small">Visitors asked for these to be sent from the Badger’s radio. ' +
        "Whatever you send goes out from this household’s transmitter: only send what you’d put your own name to.</p>" +
        list.map(function (p) {
          return '<div class="msg"><span class="who">' + esc(p.name) + '</span><span class="when">' + esc(tfmt(Number(p.at) * 1000, { date: true, short: true })) +
            "</span><div>" + esc(p.text) + '</div><div class="btns"><button class="btn ghost" data-mp="' + esc(p.id) + '" data-send="1">Send over radio</button>' +
            '<button class="btn ghost" data-mp="' + esc(p.id) + '" data-send="0">Don’t send</button></div></div>';
        }).join("") + '<p class="small" id="mpmsg" role="status"></p></div>';
      box.querySelectorAll("[data-mp]").forEach(function (b) {
        b.onclick = function () {
          api("/api/board/mesh", { body: { id: b.getAttribute("data-mp"), send: b.getAttribute("data-send") === "1" } }).then(function (r) {
            var m = document.getElementById("mpmsg"); if (m) m.textContent = r.message || r.error || "";
            load();
          });
        };
      });
    }
    load();
  }

  function toolsView() {
    loadLib().then(function (lib) {
      h('<div class="crumbs"><a href="#/">Home</a> › Tools</div><h1>🧰 Tools</h1><div class="grid">' + lib.tools.map(function (t) {
        return '<a class="tile" href="' + href(t.url) + '"><b>' + esc(t.title) + "</b></a>"; }).join("") + "</div>" +
        '<h2 class="section">Apps & downloads</h2><div class="list">' + lib.items.filter(function (i) { return i.category === "tools"; }).map(itemCard).join("") + "</div>");
    }).catch(libFailed);
  }

  // USB sticks are the family's own files: members and the admin only (a visitor sees an explanation).
  function filesView(drive, path) {
    var fam = lsGet("isMember", "") === "1" || state.admin;
    if (drive) return usbBrowse(drive, path || "");
    api("/api/usb").then(function (list) {
      list = list && list.length ? list : [];
      h('<h1>💾 USB & files</h1><p class="muted">Plug a USB stick into the front of the Badger. Family members (and the admin) can open its files here.</p>' +
        (list.length ? '<div class="list">' + list.map(function (u) {
          return '<div class="card"><div class="row"><div><h3>' + esc(u.name) + '</h3><div class="meta">' + esc(human(num(u.free))) + " free of " + esc(human(num(u.total))) +
            (u.bundle ? ' · <span class="pill ok">contains an update bundle</span>' : "") + '</div></div><div class="btns"><a class="btn" href="#/files?d=' + encodeURIComponent(u.drive || u.name) + '">Browse</a></div></div></div>';
        }).join("") + "</div>" : fam ? '<p class="card">No USB drive detected.</p>' :
          '<p class="card">USB drives are for the family whose Badger this is. Family members: join under <a href="#/board/family">Check-in board → Family</a>.</p>'));
    });
  }
  function usbBrowse(drive, path) {
    api("/api/usb/ls?drive=" + encodeURIComponent(drive) + "&path=" + encodeURIComponent(path)).then(function (d) {
      if (d._status !== 200) return h('<p class="muted">' + esc(d.error || "Not found.") + ' <a href="#/files">Back</a></p>');
      var parts = (d.path || "").split("/").filter(Boolean), acc = "", base = "#/files?d=" + encodeURIComponent(drive);
      var crumbs = '<a href="#/files">USB & files</a> › <a href="' + base + '">' + esc(drive) + "</a>" +
        parts.map(function (p) { acc += (acc ? "/" : "") + p; return ' › <a href="' + base + "&p=" + encodeURIComponent(acc) + '">' + esc(p) + "</a>"; }).join("");
      var rows = d.entries.map(function (e) {
        if (e.dir) return '<tr><td>📁 <a href="' + base + "&p=" + encodeURIComponent(e.path) + '">' + esc(e.name) + "</a></td><td></td><td></td></tr>";
        return "<tr><td>" + esc(e.name) + '</td><td class="sz">' + esc(human(num(e.size))) + '</td><td style="text-align:right"><a class="btn ghost" href="' + href(e.url) + '">Open</a></td></tr>';
      }).join("");
      h('<div class="crumbs">' + crumbs + '</div><h1>💾 ' + esc(drive) + '</h1><table class="files">' + (rows || '<tr><td class="muted">Empty</td></tr>') + "</table>");
    });
  }

  function devicesView() {
    var host = location.host;
    h('<h1>📱 Phones, iPads & laptops</h1>' +
      '<p>Everything runs on the Badger. Your device only needs a web browser and the <b>' + esc((state.status && state.status.ssid) || "Badger’s") + '</b> Wi-Fi. Nothing is stored on it unless you choose to.</p>' +
      '<div class="card"><h2>iPad & iPhone</h2><ol><li>Open <b>http://' + esc(host) + '</b> in Safari.</li><li>Tap Share → <b>Add to Home Screen</b>.</li><li>The launcher now opens like an app, full screen.</li></ol>' +
      '<p class="small">Want the tools (Morse, phrasebook, water, radio, converter) to work even out of range? Install the <a href="/ca.crt">Badger certificate</a> (Settings → General → VPN & Device Management, then About → Certificate Trust Settings), and use <b>https://' + esc(location.hostname) + '</b> instead. The app then caches them.</p>' +
      '<p class="small muted">Leaving the Badger’s range? Download a few ZIMs from any collection’s “Open with → Download” into the free <b>Kiwix</b> app, and a map region into <b>Organic Maps</b>.</p></div>' +
      '<div class="card"><h2>Android</h2><ol><li>Open <b>http://' + esc(host) + '</b> in Chrome → menu → <b>Install app</b>.</li><li>Optional: install Kiwix, Organic Maps, OsmAnd, Trail Sense and Meshtastic straight from the Badger under <a href="#/tools">Tools → Apps</a>.</li></ol></div>' +
      '<div class="card"><h2>Laptop</h2><p>Use any browser at <b>http://' + esc(host) + '</b>. Chrome and Edge can install it as an app. For a laptop that travels without the Badger, see the <b>go-bag edition</b> in the manual (keeps a fixed-size subset, e.g. 64 GB, synced from the Badger).</p></div>');
  }

  function statusView() {
    Promise.all([api("/api/status"), api("/api/updates")]).then(function (r) {
      var s = r[0], u = r[1], b = u.build, rep = u.report;
      var svc = Object.keys(s.services || {}).map(function (k) { return '<span class="pill ' + (s.services[k] ? "ok" : "bad") + '">' + esc(k) + (s.services[k] ? " ✓" : " ✗") + "</span>"; }).join(" ");
      var p = s.power || {};
      var out = '<h1>Status</h1><div id="personalStatus"></div>';
      if (b && b.phase && b.phase !== "done") out += '<div class="card"><h2>⏳ Building this Badger</h2><p>' + esc(b.message || b.phase) + '</p><div class="bar"><i style="width:' + Math.max(0, Math.min(100, num(b.pct))) + '%"></i></div><p class="small muted">' + num(b.done) + " of " + esc(b.total || "?") + " items · " + esc(b.current || "") + "</p></div>";
      out += '<div class="card"><dl class="kv">' +
        "<dt>Battery</dt><dd>" + (p.percent != null ? Math.round(num(p.percent)) + "% " + (p.charging ? "(charging" + (p.input_v ? ", " + num(p.input_v).toFixed(1) + " V in" : "") + ")" : "(on battery)") + (p.runtime_min ? " · ~" + (num(p.runtime_min) / 60).toFixed(1) + " h left" : "") : "no battery data") + "</dd>" +
        "<dt>Power mode</dt><dd>" + esc(p.mode || "—") + "</dd>" +
        (p.input_v != null ? "<dt>Solar / DC in</dt><dd>" + num(p.input_v).toFixed(1) + " V · " + num(p.input_a).toFixed(2) + " A · <b>" + num(p.input_w).toFixed(1) + " W</b></dd>" : "") +
        "<dt>Wi-Fi</dt><dd>" + esc(s.ssid) + " · " + (s.clients != null ? num(s.clients) + " connected" : "?") + "</dd>" +
        "<dt>Internet for updates</dt><dd>" + (s.uplink ? esc(s.uplink.type === "wifi" ? "Wi-Fi “" + s.uplink.connection + "”" : "Ethernet") + " (content keeps serving while it downloads)" : "none") + "</dd>" +
        "<dt>Case fan</dt><dd>" + (p.case_fan == null ? "—" : p.case_fan ? "on" : "off") + "</dd>" +
        "<dt>Storage</dt><dd>" + (s.storage ? esc(human(num(s.storage.used))) + " used · " + esc(human(num(s.storage.free))) + " free" : "?") + "</dd>" +
        "<dt>CPU</dt><dd>" + (s.cpu_temp_c != null ? Math.round(num(s.cpu_temp_c)) + "°C · " : "") + "load " + esc((s.load || []).map(num).join(" / ")) + "</dd>" +
        (s.version ? "<dt>Software</dt><dd>Honey Badger v" + esc(s.version) + "</dd>" : "") +
        "<dt>Uptime</dt><dd>" + (s.uptime_s ? (num(s.uptime_s) / 3600).toFixed(1) + " h" : "?") + "</dd>" +
        "<dt>Time</dt><dd><b>" + esc(tfmt(Date.now(), { date: true })) + "</b><br><span class=\"small muted\">" +
          ["local", "utc", "nutime"].filter(function (m) { return !window.NuTime || m !== NuTime.pref(); }).map(function (m) { return esc(tfmt(Date.now(), { mode: m })); }).join(" · ") +
          " — tap the clock in the status bar to switch</span></dd>" +
        (s.posture && s.posture.label ? '<dt>Security posture</dt><dd><a href="#/posture">' + esc(s.posture.label) + "</a></dd>" : "") +
        "<dt>Services</dt><dd>" + svc + "</dd></dl></div>";
      out += '<h2 class="section">Updates</h2>';
      if (rep) {
        var c = rep.counts || {};
        out += '<div class="card"><p>Last checked <b title="' + esc(tfmt(rep.generated, { date: true })) + '">' + esc(ago(rep.generated)) + "</b> (" + esc(tfmt(rep.generated, { date: true, short: true })) + "): " + num(c["up-to-date"]) + " current, " + (num(c.update) + num(c.install)) + " to fetch (~" + esc(rep.download_human) + "), " + num(c.offline) + " unreachable.</p>" +
          '<table class="files">' + (rep.items || []).filter(function (i) { return i.status === "update" || i.status === "install" || i.status === "error"; }).map(function (i) {
            return "<tr><td>" + esc(i.title) + '<div class="small muted">' + esc(i.local_version || "new") + " → " + esc(i.remote_version || "?") + " · " + esc(i.method) + (i.note ? " · " + esc(i.note) : "") + '</div></td><td class="sz">' + esc(i.download_human) + "</td></tr>";
          }).join("") + "</table></div>";
      } else out += '<p class="muted">No update check yet. Connect the Badger to the internet (Ethernet, phone tethering, or another Wi-Fi) and run a check from Settings.</p>';
      if (u.history && u.history.length) out += '<h2 class="section">Recent changes</h2><div class="card small">' + u.history.slice().reverse().map(function (x) { return "<div>" + esc(tfmt(x.at, { date: true, short: true })) + " · " + esc(x.event) + " <b>" + esc(x.id) + "</b> " + esc(x.version || "") + "</div>"; }).join("") + "</div>";
      h(out);
      personalNotice("personalStatus");
    });
  }

  var SOURCE_URL = "https://github.com/chad-maglaque/honey-badger";
  // Sealed records (docs/SECURITY.md): the owner passphrase that unlocks the family's sealed data after a restart.
  function sealBox(note) {
    var box = document.getElementById("sealBox"); if (!box) return;
    api("/api/sealed").then(function (st) {
      if (st._status !== 200) { box.innerHTML = ""; return; }
      var secureMsg = st.secure ? "" : '<p class="small warnbox">This page isn’t encrypted (http://), so the passphrase isn’t typed here. Open <b>https://' +
        esc(location.hostname) + "</b>, or unlock at the Badger itself.</p>";
      var head = '<h3 class="section">🔒 Sealed records</h3>';
      var out;
      if (!st.available) {
        out = head + '<p class="small warnbox">Sealing needs the openssl program, which isn’t working on this Badger.</p>';
      } else if (!st.setup) {
        out = head + '<div class="card warnbox"><p class="small"><b>Not protected yet.</b> Supplies, the aid-station log, family board history, family names and contact details are stored scrambled, but the key sits next to them until you set an owner passphrase. ' +
          "With one, a Badger that is taken or stolen shows nothing of them. After every restart the Badger keeps working (alerts, library, maps, radio, new entries), and the owner unlocks the family’s records with the passphrase.</p>" +
          '<p class="small">Use 16+ characters or four or more words — not the admin PIN. You’ll get a recovery key to write down, in case the passphrase is forgotten.</p>' + secureMsg +
          (st.secure ? '<form id="sealSetup"><label for="sp1">Owner passphrase</label><input type="password" id="sp1" autocomplete="new-password">' +
            '<label for="sp2">Type it again</label><input type="password" id="sp2" autocomplete="new-password">' +
            '<div class="btns"><button class="btn">Set up sealed records</button></div></form>' : "") + "</div>";
      } else if (st.unlocked) {
        out = head + '<p class="small"><span class="pill ok">Unlocked</span> The family’s sealed records can be read until the Badger restarts or someone locks them' +
          (st.idle_minutes ? " (or after " + num(st.idle_minutes) + " minutes without use)" : "") + ".</p>" +
          (st.lockdown ? '<p class="small warnbox">The Badger is in lockdown and you chose to unlock anyway. Lock again when you’re done.</p>' : "") +
          (st.recovery_ack === false ? '<p class="small warnbox">Have you written down the recovery key? Without it, a forgotten passphrase means the sealed records are gone. <button class="btn ghost" id="sealAck">Yes, it’s written down</button></p>' : "") +
          '<div class="btns"><button class="btn" id="sealLock">Lock now</button></div>' + secureMsg +
          (st.secure ? '<details><summary class="small">Change the owner passphrase · new recovery key</summary>' +
            '<label for="sco">Current passphrase</label><input type="password" id="sco" autocomplete="current-password">' +
            '<label for="scn">New passphrase</label><input type="password" id="scn" autocomplete="new-password">' +
            '<div class="btns"><button class="btn ghost" id="sealChange">Change passphrase</button><button class="btn ghost" id="sealNewRk">Make a new recovery key</button></div></details>' : "");
      } else {
        out = head + '<p class="small"><span class="pill warn">Locked</span> ' + esc({ idle: "Locked after a while without use.", lockdown: "Locked because the Badger is in lockdown.",
          locked: "Someone locked it." }[st.locked_reason] || "Locked since the Badger started.") +
          " Everything else keeps working; new entries are saved sealed. Family board history, supplies, the aid-station log, names and contact details show once you unlock.</p>" + secureMsg +
          (st.wait ? '<p class="small warnbox">Too many wrong tries. Wait ' + Math.ceil(num(st.wait) / 60) + " minute(s).</p>" : "") +
          (st.secure ? '<form id="sealUnlock"><label for="sus">Owner passphrase (or the recovery key)</label><input type="password" id="sus" autocomplete="current-password">' +
            (st.lockdown ? '<label style="font-weight:400"><input type="checkbox" id="sld"> The Badger is in lockdown. Unlock anyway — I’m sure nobody unwanted can reach it.</label>' : "") +
            '<div class="btns"><button class="btn">Unlock</button></div></form>' +
            '<details><summary class="small">Forgot the passphrase?</summary><label for="srk">Recovery key</label><input type="text" id="srk" autocomplete="off" class="mono">' +
            '<label for="srn">New owner passphrase</label><input type="password" id="srn" autocomplete="new-password"><div class="btns"><button class="btn ghost" id="sealRecover">Set new passphrase</button></div></details>' : "");
      }
      box.innerHTML = out + '<div id="sealOut" class="small" role="status">' + esc(note || "") + "</div>";
      function say(t) { var o = document.getElementById("sealOut"); if (o) o.textContent = t; }
      function after(r, ok) { if (r.ok) sealBox(ok || ""); else say(r.error || "That didn’t work."); }
      function showKey(rk) {
        box.innerHTML = head + '<div class="card warnbox"><h3>Write down your recovery key</h3><p class="small">It unlocks the sealed records if the owner passphrase is forgotten. It is shown only now. Keep it somewhere safe, away from the Badger.</p>' +
          '<p class="mono" style="font-size:1.1rem;word-break:break-all">' + esc(rk) + '</p><div class="btns"><button class="btn ghost" id="sealPrint">Print</button><button class="btn" id="sealKeyDone">I’ve written it down</button></div></div>';
        document.getElementById("sealPrint").onclick = function () { window.print(); };
        document.getElementById("sealKeyDone").onclick = function () { api("/api/sealed", { body: { op: "ack_recovery" } }).then(function () { sealBox(); }); };
      }
      var f = document.getElementById("sealSetup");
      if (f) f.onsubmit = function (e) {
        e.preventDefault();
        var a = document.getElementById("sp1").value;
        if (a !== document.getElementById("sp2").value) return say("The two passphrases don’t match.");
        say("Setting up… this can take a minute.");
        api("/api/sealed", { body: { op: "setup", passphrase: a } }).then(function (r) { if (r.ok) showKey(r.recovery_key); else say(r.error || "That didn’t work."); });
      };
      f = document.getElementById("sealUnlock");
      if (f) f.onsubmit = function (e) {
        e.preventDefault();
        var ld = document.getElementById("sld");
        say("Unlocking…");
        api("/api/sealed", { body: { op: "unlock", secret: document.getElementById("sus").value, confirm_lockdown: !!(ld && ld.checked) } })
          .then(function (r) { after(r, r.via === "recovery" ? "Unlocked with the recovery key. Consider setting a new passphrase." : "Unlocked."); });
      };
      var b = document.getElementById("sealLock");
      if (b) b.onclick = function () { api("/api/sealed", { body: { op: "lock" } }).then(function (r) { after(r, "Locked."); }); };
      b = document.getElementById("sealAck");
      if (b) b.onclick = function () { api("/api/sealed", { body: { op: "ack_recovery" } }).then(function (r) { after(r); }); };
      b = document.getElementById("sealChange");
      if (b) b.onclick = function () {
        api("/api/sealed", { body: { op: "change", old: document.getElementById("sco").value, "new": document.getElementById("scn").value } })
          .then(function (r) { after(r, "Passphrase changed."); });
      };
      b = document.getElementById("sealNewRk");
      if (b) b.onclick = function () {
        if (!confirm("Make a new recovery key? The old one stops working.")) return;
        api("/api/sealed", { body: { op: "new_recovery", passphrase: document.getElementById("sco").value } })
          .then(function (r) { if (r.ok) showKey(r.recovery_key); else say(r.error || "Enter the current passphrase first."); });
      };
      b = document.getElementById("sealRecover");
      if (b) b.onclick = function () {
        var ld = document.getElementById("sld");
        api("/api/sealed", { body: { op: "recover", recovery_key: document.getElementById("srk").value, "new": document.getElementById("srn").value,
          confirm_lockdown: !!(ld && ld.checked) } }).then(function (r) { after(r, "New passphrase set; unlocked."); });
      };
    });
  }

  function settingsView() {
    var theme = lsGet("theme", "auto"), text = lsGet("text", "normal");
    var out = "<h1>Settings</h1>" +
      '<div class="card"><h2>This device</h2>' +
      '<label for="th">Appearance</label><select id="th">' + ["auto", "light", "dark", "red"].map(function (t) {
        return '<option value="' + t + '"' + (t === theme ? " selected" : "") + ">" + { auto: "Match system", light: "Light", dark: "Dark", red: "Night vision (red)" }[t] + "</option>"; }).join("") + "</select>" +
      '<label for="tf">Show times as</label><select id="tf"><option value="local">Local time</option><option value="utc">UTC</option><option value="nutime">Nu:Time™ (GG:BB:TT)</option></select>' +
      '<label for="tx">Text size</label><select id="tx"><option value="normal"' + (text === "normal" ? " selected" : "") + '>Normal</option><option value="large"' + (text === "large" ? " selected" : "") + ">Large</option></select>" +
      '<h3 class="section">Open content with…</h3><div id="myRend" class="small muted">Loading…</div>' +
      '<h3 class="section">Family</h3><div id="meBox" class="small muted">Checking…</div></div>' +
      '<div class="card section" id="personalCard"></div>' +
      '<div class="card section"><h2>Badger administration</h2><div id="adm"></div></div>' +
      // AGPL-3.0 §13: people using this Badger over the network can get its source. If you change the code,
      // point SOURCE_URL at the source of your version.
      '<p class="small muted section">Honey Badger is free software under the AGPL-3.0. <a href="' + href(SOURCE_URL) + '">Source code and licences</a>.</p>';
    h(out);
    document.getElementById("th").onchange = function () { lsSet("theme", this.value); applyPrefs(); };
    document.getElementById("tx").onchange = function () { lsSet("text", this.value); applyPrefs(); };
    if (window.NuTime) { document.getElementById("tf").value = NuTime.pref(); document.getElementById("tf").onchange = function () { NuTime.setPref(this.value); tickClock(); }; }
    refreshWho().then(function (r) {
      var box = document.getElementById("meBox");
      if (!box) return;
      if (r._status === 503) { box.textContent = r.error || "The family list can’t be read right now."; return; }
      if (r.key === "pending" && r.pending) { box.innerHTML = "This device is waiting for <b>" + esc(r.pending.name) + "</b> to approve it on one of their other devices (or at the Badger’s own screen: knob → Approve device)."; return; }
      if (!r.member) { box.innerHTML = 'This device isn’t a family member. <a href="#/board/family">Join with an invite code</a>.'; return; }
      var pr = r.member.pending_radios || [];
      box.innerHTML = "This device belongs to <b>" + esc(r.member.name) + '</b>. <button class="btn ghost" id="leave">Forget this device</button>' +
        '<p class="small"><a href="#/posture">Security posture</a> · <a href="#/circles">Trusted circle</a></p>' +
        '<h4 class="section">Family radio link</h4><p class="small">The link (with the channel key) for your Meshtastic radio. Shown only on family members’ own devices, over https, and on the Badger’s screen.</p>' +
        '<div class="btns"><button class="btn ghost" id="myLink">Show family radio link</button></div><div id="myLinkOut"></div>' +
        '<h4 class="section">My radios</h4><p class="small">Your radio’s posts on the family channel carry your name. To link one, enter its ID; then send the code it gives you from that radio on the family channel.</p>' +
        '<p class="small">' + ((r.member.radios || []).map(function (x) { return '<span class="mono">' + esc(x) + '</span> <button class="btn ghost" data-myunradio="' + esc(x) + '">×</button>'; }).join(" ") || "No radio linked yet.") +
        (pr.length ? "<br>Waiting for a message from: " + pr.map(function (x) { return '<span class="mono">' + esc(x.radio) + "</span>"; }).join(", ") : "") + "</p>" +
        '<div class="btns"><input type="text" id="myRadio" placeholder="!a1b2c3d4" maxlength="9" style="width:9em"><button class="btn ghost" id="myPair">Link my radio</button></div><div id="myRadioOut" class="small"></div>';
      document.getElementById("leave").onclick = function () {
        if (!confirm("Remove family access from this device? You’ll need a new invite to get it back.")) return;
        api("/api/members/leave", { body: {} }).then(function () { lsDel("isMember"); settingsView(); });
      };
      document.getElementById("myLink").onclick = function () {
        api("/api/members/radio-link").then(function (x) {
          var o = document.getElementById("myLinkOut");
          if (!x.url) { o.innerHTML = '<p class="small">' + esc(x.message || x.error || "Not available.") + "</p>"; return; }
          o.innerHTML = '<p><a class="btn" href="' + href(x.url) + '">Open in Meshtastic</a></p><p class="small mono" style="word-break:break-all">' + esc(x.url) + "</p>" +
            (x.bt_pin ? '<p class="small">The Badger radio’s Bluetooth PIN: <b class="mono">' + esc(x.bt_pin) + "</b></p>" : "") +
            '<p class="small">Keep it in the family: whoever has this link can read and send on the family channel.</p>';
        });
      };
      document.getElementById("myPair").onclick = function () {
        api("/api/members/radio", { body: { op: "pair", radio: document.getElementById("myRadio").value } }).then(function (x) {
          document.getElementById("myRadioOut").innerHTML = x.ok
            ? "From that radio, on the family channel, send: <b class=\"mono\">!pair " + esc(x.code) + "</b> (within an hour). It’s linked when the Badger hears it — reopen this page to check."
            : esc(x.error || "That didn’t work.");
        });
      };
      box.querySelectorAll("[data-myunradio]").forEach(function (b) { b.onclick = function () {
        api("/api/members/radio", { body: { op: "remove", radio: b.getAttribute("data-myunradio") } }).then(function () { settingsView(); });
      }; });
    });
    api("/api/settings").then(function (d) {
      var kinds = d.renderers;
      document.getElementById("myRend").innerHTML = Object.keys(kinds).filter(function (k) { return kinds[k].options.length > 1; }).map(function (k) {
        var cur = lsGet("rend:" + k, ""), dflt = ((d.settings.renderer_defaults || {})[k]) || kinds[k].default;
        return '<label>' + esc(kinds[k].label) + '</label><select data-k="' + esc(k) + '" class="rsel"><option value="">Device default (' + esc(dflt) + ")</option>" +
          kinds[k].options.map(function (o) { return '<option value="' + esc(o.id) + '"' + (o.id === cur ? " selected" : "") + ">" + esc(o.name) + "</option>"; }).join("") + "</select>";
      }).join("");
      Array.prototype.forEach.call(document.querySelectorAll(".rsel"), function (s) {
        s.onchange = function () { if (this.value) lsSet("rend:" + this.getAttribute("data-k"), this.value); else try { localStorage.removeItem("rend:" + this.getAttribute("data-k")); } catch (e) {} };
      });
      adminPanel(kinds, d.settings);
    });
    personalCard(false);
  }

  // ── Emergency: panic wipe, duress PIN, dead-man switch, handover (docs/SECURITY.md → "Panic wipe") ──
  function wipeBox() {
    var box = document.getElementById("wipeBox"); if (!box) return;
    function post(body) { return api("/api/admin/wipe", { body: body }); }
    function say(id, text) { var e = document.getElementById(id); if (e) e.textContent = text; }
    function val(id) { var e = document.getElementById(id); return e ? e.value : ""; }
    post({ op: "status" }).then(function (s) {
      if (!s || !s.ok) { box.innerHTML = ""; return; }
      var dm = s.deadman || {};
      var lockNote = s.secure ? "" : '<p class="small warnbox">This page is not encrypted (http://), so these controls are switched off here. Open <b>https://' + esc(location.hostname) + '</b>, or use the Badger’s own screen (knob → System → Panic wipe / Hand over / sell).</p>';
      var dis = s.secure ? "" : " disabled";
      box.innerHTML =
        '<h3 class="section">Emergency: panic wipe</h3>' +
        '<p class="small muted">Erases the family’s data on this Badger in seconds: members, both boards, supplies and where they are kept, the aid log, skills, missing people, the document vault, radio and device keys, saved Wi-Fi passwords and logs. The library, maps and AI stay, so the Badger looks like a plain offline library. The radio gets new random keys (if its bridge is running — otherwise unplug it). The Wi-Fi gets a new password, shown only on the Badger’s screen. It can’t be undone.</p>' + lockNote +
        '<label for="wipeWord">Type WIPE to confirm</label><div class="btns"><input type="text" id="wipeWord" autocomplete="off" autocapitalize="characters"' + dis + '><button class="btn danger" id="wipeGo"' + dis + '>Wipe family data now</button></div><p class="small" id="wipeMsg" role="status"></p>' +
        '<h3 class="section">If someone forces you to unlock the Badger</h3>' +
        '<p class="small muted"><b>People come first.</b> If the person forcing you can check what you say — they know the family, have seen this Badger before, or know what is on it — give the real PIN or passphrase. Use a duress secret only when they can’t tell the difference. Agree as a family: whoever is forced gives whatever keeps people safest, and nobody blames them afterwards.</p>' +
        '<p class="small muted">A duress secret works exactly like the real one — same screens, same timing — but every screen and every family phone then shows a <b>made-up household</b> (below): a few ordinary supplies, everyday board posts, made-up first names. Nothing on the screen says so.</p>' +
        '<h4 class="section">What happens behind the made-up household</h4>' +
        '<label style="font-weight:400"><input type="radio" name="durAct" value="lock"' + (s.duress_action !== "wipe" ? " checked" : "") + dis + '> <b>Lock (recommended)</b> — nothing is erased. The family’s records and the vault lock so that the passphrases alone no longer open them: only the <b>recovery keys</b> do. When everyone is safe, type the sealed-records recovery key (Sealed records → Lock now → Unlock, or the Badger’s knob: System → Recovery key) and everything is back. No restart, no Wi-Fi change.</label>' +
        '<label style="font-weight:400"><input type="radio" name="durAct" value="wipe"' + (s.duress_action === "wipe" ? " checked" : "") + dis + '> <b>Wipe</b> — the panic wipe runs quietly (the noticeable steps wait 10 minutes) and the made-up household stays in place of the family’s data. <b>There is nothing to get back afterwards.</b></label>' +
        (s.duress_action !== "wipe" && !s.sealed_setup ? '<p class="small warnbox">Set up sealed records first (above): without the owner passphrase and its recovery key the lock can only last until the Badger restarts.</p>' : "") +
        '<div class="btns"><input type="password" id="actPin" placeholder="Admin PIN" autocomplete="current-password"' + dis + '><button class="btn ghost" id="actSet"' + dis + '>Save choice</button></div><p class="small" id="actMsg" role="status"></p>' +
        '<h4 class="section">Duress PIN</h4><p class="small muted">A second admin PIN. It must differ from the admin PIN. Status: <b>' + (s.duress_set ? "set" : "not set") + '</b>.</p>' +
        '<div class="btns"><input type="password" id="durPin" placeholder="Admin PIN" autocomplete="current-password"' + dis + '><input type="password" id="durNew" placeholder="New duress PIN" autocomplete="new-password"' + dis + '><button class="btn ghost" id="durSet"' + dis + '>Set duress PIN</button>' + (s.duress_set ? '<button class="btn ghost" id="durClear"' + dis + '>Remove it</button>' : "") + '</div><p class="small" id="durMsg" role="status"></p>' +
        '<h4 class="section">Duress passphrase (sealed records)</h4><p class="small muted">Typed where the owner passphrase is asked, it “unlocks” — into the made-up household. As strong as a passphrase, and different from it. Status: <b>' + (s.duress_phrase ? "set" : "not set") + '</b>.</p>' +
        (s.sealed_setup ? '<div class="btns"><input type="password" id="dpOwner" placeholder="Owner passphrase" autocomplete="current-password"' + dis + '><input type="password" id="dpNew" placeholder="New duress passphrase" autocomplete="new-password"' + dis + '><button class="btn ghost" id="dpSet"' + dis + '>Set</button>' + (s.duress_phrase ? '<button class="btn ghost" id="dpClear"' + dis + '>Remove it</button>' : "") + '</div><p class="small" id="dpMsg" role="status"></p>' : '<p class="small">Set up sealed records first.</p>') +
        '<h4 class="section">Duress code (front panel)</h4><p class="small muted">' + (s.panel_code ? "Set it at the Badger: knob → System → Panel lock → Duress code. It opens the panel like the panel code." : "First set a panel code (above), then a duress code at the Badger: knob → System → Panel lock → Duress code.") + '</p>' +
        '<h4 class="section">The made-up household</h4><p class="small muted">Every Badger has one ready, made from ordinary templates with a little randomness, so no two are the same. It shows as many people as your family has. If someone who forces you might know your family’s first names, type believable ones (or your real ones) here.</p>' +
        '<div class="btns"><button class="btn ghost" id="dcShow">Show and edit it</button></div><div id="dcBox"></div>' +
        '<h4 class="section">Dead-man switch (off unless you turn it on)</h4>' +
        (dm.armed ? '<div class="card ' + (dm.warning ? "warnbox" : "") + '"><p><b>ON:</b> if nobody checks in for ' + esc(String(dm.days)) + ' days of the Badger running, it wipes the family data by itself. ' +
          'A check-in is any family member opening the Badger app on their device, or an admin login. Wipe in about <b>' + esc(String(Math.round(num(dm.left_h)))) + ' hours</b> unless someone checks in.</p><div class="btns"><button class="btn" id="dmOff">Turn the dead-man switch off</button></div></div>'
          : '<p class="small warnbox"><b>Think hard before turning this on.</b> If everyone is away, sick or cut off from the Badger for that long, all family data is erased — there is no undo and no warning beyond this page and the Badger’s screen. Only time the Badger is running counts (a switched-off Badger never counts down; a wrong clock can’t trigger it).</p>' +
            '<div class="btns"><input type="number" id="dmDays" min="3" max="365" value="14" style="width:6em"' + dis + '><span class="small">days</span><input type="password" id="dmPin" placeholder="Admin PIN" autocomplete="current-password"' + dis + '><button class="btn ghost" id="dmArm"' + dis + '>Set up…</button></div><div id="dmStep2"></div>') +
        '<p class="small" id="dmMsg" role="status"></p>' +
        '<h4 class="section">Hand over or sell this Badger</h4><p class="small muted">Everything the panic wipe does, plus factory settings: a new Wi-Fi name and password (on the Badger’s screen), no admin PIN (the next owner sets one at the Badger: <span class="mono">sudo prepperctl set-pin</span>), no duress PIN, dead-man switch off, no trusted Badgers. On a Badger built for sale, personal-use content (non-commercial licences) is removed and the next owner is asked again.</p>' +
        '<div class="btns"><input type="text" id="hoWord" placeholder="Type HANDOVER" autocomplete="off" autocapitalize="characters"' + dis + '><input type="password" id="hoPin" placeholder="Admin PIN" autocomplete="current-password"' + dis + '></div>' +
        '<label class="small"><input type="checkbox" id="hoSale"' + dis + '> I’m selling it: also remove personal-use content</label><div class="btns"><button class="btn danger" id="hoGo"' + dis + '>Reset for the next owner</button></div><p class="small" id="hoMsg" role="status"></p>';
      var g = document.getElementById("wipeGo");
      if (g) g.onclick = function () {
        if (val("wipeWord") !== "WIPE") { say("wipeMsg", "Type WIPE in capital letters first."); return; }
        if (!confirm("Erase all family data on this Badger now? This can't be undone.")) return;
        say("wipeMsg", "Starting…");
        post({ op: "panic", confirm: "WIPE" }).then(function (r) { say("wipeMsg", r.ok ? r.message : (r.error || "Failed")); });
      };
      var ds = document.getElementById("durSet");
      if (ds) ds.onclick = function () {
        post({ op: "duress_set", pin: val("durPin"), duress: val("durNew") }).then(function (r) {
          document.getElementById("durPin").value = ""; document.getElementById("durNew").value = "";
          say("durMsg", r.ok ? "Duress PIN set. Practise saying it calmly; write it nowhere near the admin PIN." : (r.error || "Failed"));
        });
      };
      var dc = document.getElementById("durClear");
      if (dc) dc.onclick = function () {
        post({ op: "duress_clear", pin: val("durPin") }).then(function (r) { say("durMsg", r.ok ? "Duress PIN removed." : (r.error || "Failed")); if (r.ok) wipeBox(); });
      };
      var da = document.getElementById("dmArm");
      if (da) da.onclick = function () {
        post({ op: "deadman_arm", pin: val("dmPin"), days: parseInt(val("dmDays"), 10) }).then(function (r) {
          document.getElementById("dmPin").value = "";
          if (!r.ok) { say("dmMsg", r.error || "Failed"); return; }
          document.getElementById("dmStep2").innerHTML = '<div class="card warnbox"><p>Last step. To turn the dead-man switch ON, type this code within 10 minutes: <b class="mono">' + esc(r.code) + '</b></p><div class="btns"><input type="text" id="dmCode" autocomplete="off" autocapitalize="characters"><button class="btn danger" id="dmConfirm">Turn it on</button></div></div>';
          document.getElementById("dmConfirm").onclick = function () {
            post({ op: "deadman_confirm", code: val("dmCode") }).then(function (x) { say("dmMsg", x.ok ? "The dead-man switch is ON." : (x.error || x.output || "Failed")); if (x.ok) wipeBox(); });
          };
        });
      };
      var dof = document.getElementById("dmOff");
      if (dof) dof.onclick = function () { post({ op: "deadman_off" }).then(function (r) { say("dmMsg", r.ok ? "The dead-man switch is off." : (r.error || "Failed")); if (r.ok) wipeBox(); }); };
      var as = document.getElementById("actSet");
      if (as) as.onclick = function () {
        var pick = document.querySelector('input[name="durAct"]:checked');
        post({ op: "action", pin: val("actPin"), action: pick ? pick.value : "lock" }).then(function (r) {
          document.getElementById("actPin").value = "";
          say("actMsg", r.ok ? (r.duress_action === "wipe" ? "Saved: a duress secret wipes the family data." : "Saved: a duress secret locks the family data (the recovery key brings it back).") : (r.error || "Failed"));
        });
      };
      var dps = document.getElementById("dpSet");
      if (dps) dps.onclick = function () {
        post({ op: "phrase_set", passphrase: val("dpOwner"), duress: val("dpNew") }).then(function (r) {
          document.getElementById("dpOwner").value = ""; document.getElementById("dpNew").value = "";
          say("dpMsg", r.ok ? "Duress passphrase set." : (r.error || "Failed"));
        });
      };
      var dpc = document.getElementById("dpClear");
      if (dpc) dpc.onclick = function () {
        post({ op: "phrase_clear", passphrase: val("dpOwner") }).then(function (r) { say("dpMsg", r.ok ? "Duress passphrase removed." : (r.error || "Failed")); if (r.ok) wipeBox(); });
      };
      var dcs = document.getElementById("dcShow");
      if (dcs) dcs.onclick = function () { post({ op: "decoy_get" }).then(decoyEditor); };
      var ho = document.getElementById("hoGo");
      if (ho) ho.onclick = function () {
        if (val("hoWord") !== "HANDOVER") { say("hoMsg", "Type HANDOVER in capital letters first."); return; }
        if (!confirm("Erase all family data AND reset this Badger to factory settings? This can't be undone.")) return;
        post({ op: "handover", confirm: "HANDOVER", pin: val("hoPin"), sale: document.getElementById("hoSale").checked }).then(function (r) {
          document.getElementById("hoPin").value = ""; say("hoMsg", r.ok ? r.message : (r.error || "Failed"));
        });
      };
    });
  }

  // The made-up household a duress session sees: preview and edit (names, supplies, board posts), or regenerate.
  function decoyEditor(r) {
    var box = document.getElementById("dcBox"); if (!box) return;
    if (!r || !r.ok) { box.innerHTML = '<p class="small">' + esc((r && r.error) || "Couldn’t load it.") + "</p>"; return; }
    var p = r.profile, pv = r.preview || {}, cats = r.categories || [];
    function row(it) {
      it = it || { name: "", qty: 1, unit: "", category: "food", location: "", expires_in_days: "" };
      return '<tr><td><input class="dcN" value="' + esc(it.name) + '" maxlength="60"></td><td><input class="dcQ" type="number" min="0" step="any" style="width:5em" value="' + esc(String(it.qty)) + '"></td>' +
        '<td><input class="dcU" style="width:6em" value="' + esc(it.unit) + '" maxlength="16"></td><td><select class="dcC">' + cats.map(function (c) { return '<option' + (c === it.category ? " selected" : "") + ">" + esc(c) + "</option>"; }).join("") + "</select></td>" +
        '<td><input class="dcL" style="width:8em" value="' + esc(it.location) + '" maxlength="60"></td><td><input class="dcE" type="number" min="-30" max="3650" style="width:5em" value="' + esc(it.expires_in_days === null || it.expires_in_days === undefined ? "" : String(it.expires_in_days)) + '"></td>' +
        '<td><button class="btn ghost dcDel" type="button">✕</button></td></tr>';
    }
    box.innerHTML = '<div class="card"><p class="small">What a duress session shows now: <b>' + esc(String((pv.members || []).length)) + "</b> " + ((pv.members || []).length === 1 ? "person" : "people") + " (" + esc((pv.members || []).join(", ") || "none yet") + "), <b>" + esc(String(pv.supplies)) + "</b> supplies, <b>" + esc(String(pv.posts)) + "</b> board posts, the vault: " + esc(pv.vault || "") + ", posture: " + esc(pv.posture || "") + ".</p>" +
      '<label for="dcNames">First names (one per line; the first ones are used, as many as your family has)</label><textarea id="dcNames" rows="4">' + esc((p.names || []).join("\n")) + "</textarea>" +
      '<p class="small"><b>Supplies</b> — ordinary amounts and ordinary places (no hiding places). “Expires in” is days from the day it is shown.</p><div style="overflow-x:auto"><table class="small"><thead><tr><th>Item</th><th>Qty</th><th>Unit</th><th>Kind</th><th>Where</th><th>Expires in</th><th></th></tr></thead><tbody id="dcRows">' + (p.supplies || []).map(row).join("") + "</tbody></table></div>" +
      '<div class="btns"><button class="btn ghost" id="dcAdd" type="button">Add an item</button></div>' +
      '<label for="dcPosts">Family board posts (one per line; spread over the last days, under the made-up names). Everyday things only: no places, no times when the house is empty.</label><textarea id="dcPosts" rows="6">' + esc((p.board || []).map(function (b) { return b.text; }).join("\n")) + "</textarea>" +
      '<div class="btns"><button class="btn" id="dcSave" type="button">Save</button><button class="btn ghost" id="dcRegen" type="button">Make a new one</button></div><p class="small" id="dcMsg" role="status"></p></div>';
    function wireDel() {
      Array.prototype.forEach.call(box.querySelectorAll(".dcDel"), function (b) { b.onclick = function () { var tr = this.parentNode.parentNode; tr.parentNode.removeChild(tr); }; });
    }
    wireDel();
    document.getElementById("dcAdd").onclick = function () {
      var tb = document.getElementById("dcRows"), t = document.createElement("tbody");
      t.innerHTML = row(null); tb.appendChild(t.firstChild); wireDel();
    };
    function msg(t) { var e = document.getElementById("dcMsg"); if (e) e.textContent = t; }
    document.getElementById("dcSave").onclick = function () {
      var items = [];
      Array.prototype.forEach.call(box.querySelectorAll("#dcRows tr"), function (tr) {
        function v(c) { var e = tr.querySelector(c); return e ? e.value : ""; }
        if (!v(".dcN").replace(/\s/g, "")) return;
        var old = null;
        (p.supplies || []).forEach(function (x) { if (x.name === v(".dcN")) old = x; });
        items.push({ name: v(".dcN"), qty: parseFloat(v(".dcQ")) || 0, unit: v(".dcU"), category: v(".dcC"), location: v(".dcL"),
          expires_in_days: v(".dcE") === "" ? null : parseInt(v(".dcE"), 10), min: old ? old.min : 0,
          kcal: old ? old.kcal : 0, litres: old ? old.litres : 0, hours: old ? old.hours : 0 });
      });
      var prof = { names: document.getElementById("dcNames").value.split("\n"), supplies: items,
        board: document.getElementById("dcPosts").value.split("\n").map(function (t) { return { text: t }; }) };
      api("/api/admin/wipe", { body: { op: "decoy_save", profile: prof } }).then(function (x) { if (x.ok) { decoyEditor(x); msg("Saved."); } else msg(x.error || "Failed"); });
    };
    document.getElementById("dcRegen").onclick = function () {
      if (!confirm("Replace the made-up household with a new random one?")) return;
      api("/api/admin/wipe", { body: { op: "decoy_regen" } }).then(function (x) { if (x.ok) { decoyEditor(x); msg("A new one is ready."); } else msg(x.error || "Failed"); });
    };
  }

  function adminPanel(kinds, settings) {
    var el = document.getElementById("adm");
    if (!state.admin) {
      el.innerHTML = '<form id="lf"><label for="pin">Admin PIN or passphrase</label><input type="password" id="pin" autocomplete="current-password"> <button class="btn">Unlock</button> <span id="lm" class="small muted"></span></form>';
      document.getElementById("lf").onsubmit = function (e) {
        e.preventDefault();
        api("/api/admin/login", { body: { pin: document.getElementById("pin").value } }).then(function (r) {
          if (r.token) { state.admin = true; sessionSet("isAdmin", "1"); state.weakPin = !!r.weak_pin; adminPanel(kinds, settings); personalCard(true); }
          else document.getElementById("lm").textContent = r.error || "Failed";
        });
      };
      return;
    }
    var rd = settings.renderer_defaults || {};
    el.innerHTML =
      '<h3>Power</h3><div class="btns">' + ["endurance", "balanced", "full"].map(function (m) { return '<button class="btn ghost" data-power="' + m + '">' + m[0].toUpperCase() + m.slice(1) + "</button>"; }).join("") + "</div>" +
      '<p class="small muted">Endurance: Wi-Fi + library only, smallest AI model, CPU throttled. Full: everything on.</p>' +
      '<h3 class="section">Updates</h3><div class="btns"><button class="btn ghost" data-upd="check">Check for updates</button><button class="btn" data-upd="apply">Download updates</button></div>' +
      '<label for="upssid">Join a Wi-Fi network for updates (uses the onboard radio; the hotspot stays up)</label><div class="btns"><input type="text" id="upssid" placeholder="Network name" autocomplete="off"><input type="password" id="uppsk" placeholder="Password" autocomplete="new-password"><button class="btn ghost" id="upjoin">Join</button><button class="btn ghost" id="upforget">Forget</button></div>' +
      '<label for="peer">Pull from another Badger on this network</label><div class="btns"><input type="text" id="peer" placeholder="http://10.10.10.11/sync"><button class="btn ghost" id="addPeer">Add</button></div>' +
      '<label for="bund">Import / export a USB update bundle</label><div class="btns"><input type="text" id="bund" placeholder="/media/usb0/prepper-bundle"><button class="btn ghost" data-upd="import">Import</button><button class="btn ghost" data-upd="export">Export</button></div>' +
      '<h3 class="section">Badger-wide time display</h3><div class="btns"><select id="dtf"><option value="local">Local</option><option value="utc">UTC</option><option value="nutime">Nu:Time™</option></select><button class="btn ghost" id="saveTf">Save default</button></div>' +
      '<p class="small muted">Used by phones that haven\'t picked their own, and by the front screen.</p>' +
      '<h3 class="section">Device default renderers</h3>' + Object.keys(kinds).filter(function (k) { return kinds[k].options.length > 1; }).map(function (k) {
        return "<label>" + esc(kinds[k].label) + '</label><select class="dsel" data-k="' + esc(k) + '">' + kinds[k].options.map(function (o) {
          return '<option value="' + esc(o.id) + '"' + ((rd[k] || kinds[k].default) === o.id ? " selected" : "") + ">" + esc(o.name) + "</option>"; }).join("") + "</select>"; }).join("") +
      '<div class="btns" style="margin-top:10px"><button class="btn ghost" id="saveDefaults">Save defaults</button></div>' +
      (state.weakPin ? '<p class="small warnbox">Your admin PIN is short or only digits. Anyone who copies the Badger’s disk could guess it. At the Badger, set a passphrase of 10+ characters (a few words): <span class="mono">sudo prepperctl set-pin</span></p>' : "") +
      '<h3 class="section">Security</h3><div class="btns"><a class="btn ghost" href="#/posture">🛡️ Security posture</a><a class="btn ghost" href="#/reveal">👁️ What this Badger reveals</a><a class="btn ghost" href="#/circles">🤝 Trusted circle</a></div>' +
      '<div id="panelLockBox"></div><div id="sealBox"></div>' +
      '<h3 class="section">Family members</h3><p class="small muted">Members can read and post on the family board and see family radios on the map. Radios on the family’s private channel can use !ai and !status. Removing someone cuts off all their devices at once. A device you add to an existing member only starts working when that member approves it on one of their own devices (or someone approves it at the Badger’s screen), and the family board says so. A radio is linked only after it sends a code on the family channel.</p>' +
      '<div class="btns"><input type="text" id="invName" maxlength="32" placeholder="Name (optional)"><button class="btn" id="invite">Invite a member</button></div><div id="invOut"></div><div id="storeBox"></div><div id="memList" class="list section"></div><div id="invList"></div>' +
      '<h3 class="section">Shared secrets</h3><p class="small muted">If someone you removed knew the Wi-Fi password, the family radio key or the admin PIN, change them. ' +
      'A new Wi-Fi password switches back by itself unless someone rejoins with it and confirms within 10 minutes (even across a restart), and the Badger’s own screen always shows the current password (knob → Join Wi-Fi) and the family radio link (knob → Family radio).</p>' +
      (location.protocol === "http:" ? '<p class="small warnbox">This page is not encrypted (http://). New passwords and radio keys are shown on the Badger’s own screen instead of here — or open <b>https://' + esc(location.hostname) + '</b> (see Phones, iPads &amp; laptops) to see them on this device.</p>' : "") +
      '<p class="small muted">The family radio link holds the channel key, so it is shown only to family members on their own devices (Settings → Family) and on the Badger’s screen — not on this admin page.</p>' +
      '<div id="wifiPending"></div><div class="btns"><button class="btn ghost" id="rotWifi">New Wi-Fi password</button><button class="btn ghost" id="rotMesh">New family radio key</button><button class="btn ghost" id="meshLink">Where is the family radio link?</button></div><div id="secOut"></div>' +
      '<p class="small muted">Changed the admin PIN (at the Badger: <span class="mono">sudo prepperctl set-pin</span>)? End every admin session, this one too:</p><div class="btns"><button class="btn ghost" id="logoutAll">Log out all admin sessions</button></div>' +
      '<h3 class="section">Radio commands</h3><p class="small muted">Who may use each command from a Meshtastic radio. Community: anyone in radio range. Family: only messages sent on the family’s private channel (a radio’s ID alone proves nothing). Changes apply to the next message.</p>' +
      [["q", "!q", "library search"], ["ai", "!ai", "AI solver answers"], ["status", "!status", "battery, power, alerts"], ["help", "!help", "list of commands"]].map(function (c) {
        var cur = ((settings.mesh_commands || {})[c[0]]) || { q: "community", ai: "family", status: "family", help: "family" }[c[0]];
        if (c[0] === "help" && cur === "off") cur = "family";     // !help is never opened to strangers
        return '<label for="mc_' + c[0] + '"><span class="mono">' + c[1] + "</span> — " + c[2] + '</label><select id="mc_' + c[0] + '" class="mcsel" data-k="' + c[0] + '">' +
          ["community", "family", "off", "ignore"].filter(function (l) { return !(c[0] === "help" && l === "off"); }).map(function (l) {
            return '<option value="' + l + '"' + (l === cur ? " selected" : "") + ">" + { community: "Community (anyone)", family: "Family only", off: "Off (family told it is off)", ignore: "Ignore (no reply)" }[l] + "</option>"; }).join("") + "</select>";
      }).join("") + '<div class="btns" style="margin-top:10px"><button class="btn ghost" id="saveMc">Save radio commands</button></div>' +
      '<h3 class="section">Services</h3><div id="svc" class="btns"></div>' +
      '<p class="small"><a href="#/solver">AI models → Problem solver page</a></p>' +
      '<h3 class="section">System</h3><div class="btns"><button class="btn ghost" data-sys="reboot">Restart</button><button class="btn danger" data-sys="shutdown">Shut down</button><button class="btn ghost" id="logout">Lock</button></div>' +
      '<div id="wipeBox"></div>' +
      '<pre id="out" class="small mono" style="white-space:pre-wrap"></pre>';
    var out = document.getElementById("out");
    function done(r) { if (r._status === 401) { state.admin = false; sessionSet("isAdmin", null); } out.textContent = r.output || r.error || (r.ok ? "OK" : "Failed"); }
    el.querySelectorAll("[data-power]").forEach(function (b) { b.onclick = function () { api("/api/admin/power", { body: { mode: b.getAttribute("data-power") } }).then(done); }; });
    el.querySelectorAll("[data-upd]").forEach(function (b) { b.onclick = function () {
      var a = b.getAttribute("data-upd"), body = { action: a };
      if (a === "import") body.src = document.getElementById("bund").value;
      if (a === "export") body.dest = document.getElementById("bund").value;
      out.textContent = "Working…"; api("/api/admin/updates", { body: body }).then(done); }; });
    document.getElementById("upjoin").onclick = function () { out.textContent = "Joining…";
      api("/api/admin/uplink", { body: { ssid: document.getElementById("upssid").value, psk: document.getElementById("uppsk").value } }).then(function (r) { document.getElementById("uppsk").value = ""; done(r); }); };
    document.getElementById("upforget").onclick = function () { api("/api/admin/uplink", { body: { forget: true } }).then(done); };
    document.getElementById("addPeer").onclick = function () { api("/api/admin/updates", { body: { action: "add-peer", peer: document.getElementById("peer").value } }).then(done); };
    document.getElementById("dtf").value = settings.display_time || "local";
    document.getElementById("saveTf").onclick = function () {
      api("/api/admin/settings", { body: { display_time: document.getElementById("dtf").value } }).then(function (r) { out.textContent = r.ok ? "Saved." : (r.error || "Failed"); });
    };
    document.getElementById("saveMc").onclick = function () {
      var m = {}; el.querySelectorAll(".mcsel").forEach(function (s) { m[s.getAttribute("data-k")] = s.value; });
      api("/api/admin/settings", { body: { mesh_commands: m } }).then(function (r) { out.textContent = r.ok ? "Radio commands saved." : (r.error || "Failed"); if (r.ok) settings = r.settings; });
    };
    document.getElementById("saveDefaults").onclick = function () {
      var m = {}; el.querySelectorAll(".dsel").forEach(function (s) { m[s.getAttribute("data-k")] = s.value; });
      api("/api/admin/settings", { body: { renderer_defaults: m } }).then(function (r) { out.textContent = r.ok ? "Saved." : (r.error || "Failed"); state.lib = null; });
    };
    el.querySelectorAll("[data-sys]").forEach(function (b) { b.onclick = function () {
      if (!confirm("Really " + b.getAttribute("data-sys") + " the Badger? Everyone connected will lose access.")) return;
      api("/api/admin/system", { body: { action: b.getAttribute("data-sys") } }).then(done); }; });
    function fmtTs(t) { return t ? tfmt(t * 1000, { date: true, short: true }) : "never"; }
    function showInvite(r, who) {
      var link = location.origin + "/#/join/" + encodeURIComponent(r.code);
      document.getElementById("invOut").innerHTML = r.ok
        ? '<div class="card"><p>Invite' + (who ? " for " + esc(who) : "") + ': <b class="mono" style="font-size:1.4em;letter-spacing:.06em">' + esc(r.code) + '</b></p>' +
          '<p class="small">On their phone, join the Badger’s Wi-Fi and open <span class="mono">' + esc(link) + '</span>, or go to Check-in board → Family and type the code. Works once, until ' + esc(fmtTs(r.expires)) + ".</p>" +
          (r.needs_approval ? '<p class="small warnbox">' + esc(r.message) + "</p>" : "") + "</div>"
        : '<p class="small">' + esc(r.error || "Failed") + "</p>";
    }
    function panelLockBox(on) {
      var box = document.getElementById("panelLockBox"); if (!box) return;
      box.innerHTML = on
        ? '<h3 class="section">Front panel lock</h3><p class="small muted">On: the knob needs the panel code for invites, the family radio key, services, Wi-Fi off, USB import and shutting down. Forgot it? Use the recovery code (knob → code screen → Forgot?), or at the console: <span class="mono">sudo python3 /opt/prepper/software/panel/panel.py --reset-lock</span></p>'
        : '<div class="card warnbox"><h3>Front panel lock is off</h3><p class="small">Anyone who can turn the Badger’s knob can make a family invite, show the family radio key, switch off weather alerts or shut it down. Strongly suggested: set a panel code (digits, entered with the knob). You’ll get a recovery code to write down.</p>' +
          '<div class="btns"><button class="btn" id="armLock">Set up panel code</button></div><div id="armOut" class="small"></div></div>';
      var b = document.getElementById("armLock");
      if (b) b.onclick = function () {
        api("/api/admin/members", { body: { op: "arm_panel_lock" } }).then(function (x) {
          document.getElementById("armOut").textContent = x.ok
            ? "Within 10 minutes, at the Badger: press the knob → System → Panel lock → Set a code. Turn the knob to pick each digit, press to add it, then choose OK. Write down the recovery code it shows."
            : (x.error || "Failed");
        });
      };
    }
    function storeBox(st) {
      var box = document.getElementById("storeBox"); if (!box) return;
      if (!st || st.ok) { box.innerHTML = ""; return; }
      box.innerHTML = '<div class="card warnbox"><p>' + esc(st.error || "The family member list can’t be read.") + "</p>" +
        (st.backup ? '<div class="btns"><button class="btn" id="restoreMembers">Restore the last good copy</button></div>' : '<p class="small">There is no backup copy. Members may need to be invited again.</p>') + "</div>";
      var b = document.getElementById("restoreMembers");
      if (b) b.onclick = function () { api("/api/admin/members", { body: { op: "restore_backup" } }).then(function (x) { out.textContent = x.ok ? "Restored." : (x.error || "Failed"); members(); }); };
    }
    function invites(list) {
      var box = document.getElementById("invList"); if (!box) return;
      if (!list || !list.length) { box.innerHTML = ""; return; }
      box.innerHTML = '<h4 class="section">Open invites</h4><div class="list">' + list.map(function (v) {
        return '<div class="row small">' + (v.member ? "Extra device" + (v.confirm ? " (needs the member’s approval)" : "") : esc(v.name || "No name yet")) + " · " + (v.source === "panel" ? "from the Badger’s screen" : "from here") +
          " · until " + esc(fmtTs(v.expires)) + ' <button class="btn ghost" data-cancel-inv="' + esc(v.id) + '">Cancel</button></div>';
      }).join("") + '</div><div class="btns"><button class="btn ghost" id="cancelAllInv">Cancel all open invites</button></div>';
      box.querySelectorAll("[data-cancel-inv]").forEach(function (b) { b.onclick = function () {
        api("/api/admin/members", { body: { op: "cancel_invite", invite: b.getAttribute("data-cancel-inv") } }).then(function () { members(); }); }; });
      document.getElementById("cancelAllInv").onclick = function () {
        if (!confirm("Cancel every open invite? Codes not used yet stop working.")) return;
        api("/api/admin/members", { body: { op: "cancel_invite", all: true } }).then(function () { members(); });
      };
    }
    function members() {
      api("/api/admin/members", { body: { op: "list" } }).then(function (r) {
        if (r._status === 401) return done(r);
        storeBox(r.store);
        panelLockBox(r.panel_lock);
        invites(r.invites);
        var list = document.getElementById("memList");
        list.innerHTML = (r.members || []).map(function (m) {
          return '<div class="card"><div class="row"><div><h3>' + esc(m.name) + (m.removed ? ' <span class="pill">removed</span>' : "") + '</h3><div class="meta">' +
            (m.removed ? "Removed " + esc(fmtTs(m.removed_at)) : m.devices.map(function (d) { return esc(d.label) + (d.pending ? " (waiting for their approval)" : " (seen " + esc(fmtTs(d.last_seen)) + ")") + ' <button class="btn ghost" data-rev="' + esc(m.id) + '" data-dev="' + esc(d.id) + '">forget</button>'; }).join(" · ") || "no devices yet") +
            (m.radios.length ? "<br>Radios: " + m.radios.map(function (x) { return '<span class="mono">' + esc(x) + '</span> <button class="btn ghost" data-unradio="' + esc(m.id) + '" data-radio="' + esc(x) + '">×</button>'; }).join(" ") : "") +
            ((m.pending_radios || []).length ? "<br>Waiting for !pair from: " + m.pending_radios.map(function (x) { return '<span class="mono">' + esc(x.radio) + '</span> <button class="btn ghost" data-unpair="' + esc(m.id) + '" data-radio="' + esc(x.radio) + '">cancel</button>'; }).join(" ") : "") +
            "</div></div>" + (m.removed ? "" : '<div class="btns"><button class="btn ghost" data-dev-invite="' + esc(m.id) + '" data-name="' + esc(m.name) + '">Add a device</button>' +
            '<input type="text" class="radioIn" data-for="' + esc(m.id) + '" placeholder="!a1b2c3d4" maxlength="9" style="width:9em"><button class="btn ghost" data-addradio="' + esc(m.id) + '">Add radio</button>' +
            '<button class="btn danger" data-remove="' + esc(m.id) + '" data-name="' + esc(m.name) + '">Remove</button></div>') + "</div></div>";
        }).join("") || '<p class="small muted">No members yet. Invite yourself first.</p>';
        function act(body) { api("/api/admin/members", { body: body }).then(function (x) { if (!x.ok) out.textContent = x.error || "Failed"; members(); }); }
        list.querySelectorAll("[data-remove]").forEach(function (b) { b.onclick = function () {
          if (!confirm("Remove " + b.getAttribute("data-name") + "? All their devices and radios lose family access now. If they know the Wi-Fi password, change that too.")) return;
          // Their radio still holds the family channel key: a new key is part of removing someone (recommended).
          var rot = confirm("Also give the family radio channel a NEW KEY? (Recommended — their radio still has the old one.)\n\nOK = new key: every family radio then needs the new link (family members find it in Settings → Family on their phones, or on the Badger’s screen: knob → Family radio).\nCancel = keep the old key.");
          api("/api/admin/members", { body: { op: "remove", id: b.getAttribute("data-remove"), rotate_key: rot } }).then(function (x) {
            if (!x.ok) out.textContent = x.error || "Failed";
            members();
            if (x.rotate) {
              if (!x.rotate.ok) { secOut.innerHTML = '<p class="small warnbox">The member was removed, but the radio key wasn’t changed: ' + esc(x.rotate.error || "") + " Use “New family radio key” below.</p>"; return; }
              pollRotation(x.rotate.id);
            }
          }); }; });
        list.querySelectorAll("[data-unpair]").forEach(function (b) { b.onclick = function () { act({ op: "cancel_radio", id: b.getAttribute("data-unpair"), radio: b.getAttribute("data-radio") }); }; });
        list.querySelectorAll("[data-rev]").forEach(function (b) { b.onclick = function () { act({ op: "revoke_device", id: b.getAttribute("data-rev"), device: b.getAttribute("data-dev") }); }; });
        list.querySelectorAll("[data-unradio]").forEach(function (b) { b.onclick = function () { act({ op: "remove_radio", id: b.getAttribute("data-unradio"), radio: b.getAttribute("data-radio") }); }; });
        list.querySelectorAll("[data-addradio]").forEach(function (b) { b.onclick = function () {
          var inp = list.querySelector('.radioIn[data-for="' + b.getAttribute("data-addradio") + '"]');
          api("/api/admin/members", { body: { op: "add_radio", id: b.getAttribute("data-addradio"), radio: inp ? inp.value : "" } }).then(function (x) {
            out.textContent = x.ok ? (x.message || "Waiting for the radio.") : (x.error || "Failed");
            members();
          }); }; });
        list.querySelectorAll("[data-dev-invite]").forEach(function (b) { b.onclick = function () {
          api("/api/admin/members", { body: { op: "invite", id: b.getAttribute("data-dev-invite") } }).then(function (x) { showInvite(x, b.getAttribute("data-name")); }); }; });
      });
    }
    var secOut = document.getElementById("secOut"), pendTimer = null;
    function wifiPending() {
      api("/api/admin/members", { body: { op: "wifi_status" } }).then(function (r) {
        var box = document.getElementById("wifiPending"); if (!box) return;
        clearTimeout(pendTimer);
        if (r.ok === false) {
          box.innerHTML = '<div class="card warnbox"><p>' + esc(r.error || "Couldn’t check the Wi-Fi password state.") + '</p><div class="btns"><button class="btn ghost" id="keepWifi">Keep new password</button><button class="btn ghost" id="undoWifi">Switch back now</button></div></div>';
        } else if (!r.pending) { box.innerHTML = ""; return; }
        else {
          var m = Math.ceil(num(r.seconds_left) / 60);
          box.innerHTML = '<div class="card" style="border-color:var(--accent)"><p><b>A new Wi-Fi password is waiting to be confirmed.</b> ' +
            (r.applied === false ? "The hotspot switches to it in a few seconds; then rejoin with it and come back here." :
              "If this device is connected with the new password, keep it. Otherwise the Badger switches back by itself in about " + m + " min.") + "</p>" +
            (r.damaged ? '<p class="small">The saved old password is damaged, so switching back isn’t possible: keep the new one.</p>' : "") +
            '<div class="btns">' + (r.applied === false ? "" : '<button class="btn" id="keepWifi">Keep new password</button>') + '<button class="btn ghost" id="undoWifi">Switch back now</button></div></div>';
        }
        var kw = document.getElementById("keepWifi");
        if (kw) kw.onclick = function () { api("/api/admin/members", { body: { op: "confirm_wifi" } }).then(function (x) { secOut.innerHTML = '<p class="small">' + esc(x.message || x.error || "") + "</p>"; wifiPending(); }); };
        document.getElementById("undoWifi").onclick = function () { api("/api/admin/members", { body: { op: "revert_wifi" } }).then(function (x) { secOut.innerHTML = '<p class="small">' + esc(x.message || x.error || "") + " Rejoin with the old password.</p>"; wifiPending(); }); };
        pendTimer = setTimeout(wifiPending, r.applied === false ? 5000 : 30000);   // (cleared above)
      });
    }
    document.getElementById("rotWifi").onclick = function () {
      if (!confirm("Make a new Wi-Fi password? In about 10 seconds everyone — this device too — is disconnected and must rejoin with the new password. Then come back here and tap “Keep new password” within 10 minutes, or it switches back.")) return;
      api("/api/admin/members", { body: { op: "rotate_wifi" } }).then(function (r) {
        secOut.innerHTML = !r.ok ? '<p class="small">' + esc(r.error || "Failed") + "</p>" :
          '<div class="card">' + (r.passphrase ? '<p>New Wi-Fi password: <b class="mono" style="font-size:1.4em;letter-spacing:.06em">' + esc(r.passphrase) + "</b></p>" :
            "<p>" + esc(r.message || "A new Wi-Fi password is made. Read it on the Badger’s own screen: knob → Join Wi-Fi.") + "</p>") +
          (r.warnings && r.warnings.length ? '<p class="small warnbox">' + esc(r.warnings.join(" ")) + "</p>" : "") +
          '<p class="small">Write it down. The hotspot switches in about ' + num(r.applies_in_s) + ' seconds. Rejoin with it (the Badger’s screen shows it: knob → Join Wi-Fi), reopen this page and tap <b>Keep new password</b> within ' + Math.round(num(r.confirm_within_s) / 60) + " minutes.</p></div>";
        wifiPending();
      });
    };
    function showLink(url, fresh, x) {
      x = x || {};
      if (!url) {                                        // not over plain http: on the Badger's screen instead
        secOut.innerHTML = '<div class="card"><p>' + (fresh ? "The family channel has a new key. " : "") + esc(x.message || x.warning || "Show the link on the Badger’s own screen: knob → Family radio → Channel link.") + "</p></div>";
        return;
      }
      secOut.innerHTML = '<div class="card"><p>' + (fresh ? "The family channel has a new key. " : "") + "Family radio channel link:</p>" +
        '<p><a class="btn" href="' + href(url) + '">Open in Meshtastic</a></p><p class="small mono" style="word-break:break-all">' + esc(url) + "</p>" +
        (x.bt_pin ? '<p class="small">The radio’s Bluetooth PIN: <b class="mono">' + esc(x.bt_pin) + "</b></p>" : "") +
        '<p class="small">Open it on your own phone to update your radio (the Meshtastic app asks to accept the channels). Then, in the Meshtastic app, share the channel QR code with the other family radios. Radios that don’t get it can no longer read family messages.</p></div>';
    }
    function pollRotation(rid) {
      secOut.innerHTML = '<p class="small">Asking the radio for a new family key…</p>';
      var tries = 0;
      (function poll() {
        api("/api/admin/members", { body: { op: "rotate_mesh_status", rid: rid } }).then(function (x) {
          if (x.pending && ++tries < 45) return setTimeout(poll, 2000);
          if (x.pending || x.cancelled) {
            // take the request back, so a bridge that starts later never acts on it
            if (x.pending) api("/api/admin/members", { body: { op: "rotate_mesh_cancel", rid: rid } });
            secOut.innerHTML = '<p class="small">The radio bridge didn’t answer. Is the mesh radio plugged in and the Mesh service on? Nothing was changed.</p>'; return;
          }
          if (!x.ok) { secOut.innerHTML = '<p class="small">The key wasn’t changed: ' + esc(x.error || "unknown error") + "</p>"; return; }
          showLink(x.url, true, x);
        });
      })();
    }
    document.getElementById("rotMesh").onclick = function () {
      if (!confirm("Give the family radio channel a new key? Family radios stop hearing the family channel until they get the new link (family members see it in Settings → Family on their phones; the Badger’s screen shows it: knob → Family radio).")) return;
      secOut.innerHTML = '<p class="small">Asking the radio…</p>';
      api("/api/admin/members", { body: { op: "rotate_mesh" } }).then(function (r) {
        if (!r.ok) { secOut.innerHTML = '<p class="small">' + esc(r.error || "Failed") + "</p>"; return; }
        pollRotation(r.id);
      });
    };
    document.getElementById("meshLink").onclick = function () {
      api("/api/admin/members", { body: { op: "mesh_link" } }).then(function (r) {
        if (r.ok) showLink(r.url, false, r); else secOut.innerHTML = '<p class="small">' + esc(r.error || "Not available") + "</p>";
      });
    };
    wifiPending();
    sealBox();
    document.getElementById("invite").onclick = function () {
      api("/api/admin/members", { body: { op: "invite", name: document.getElementById("invName").value } }).then(function (r) { showInvite(r, document.getElementById("invName").value); members(); });
    };
    members();
    wipeBox();
    document.getElementById("logoutAll").onclick = function () {
      if (!confirm("End every admin session on every device, this one too?")) return;
      api("/api/admin/logout", { body: { all: true } }).then(function (r) {
        out.textContent = r.ok ? "All admin sessions ended." : (r.error || "Failed");
        if (r.ok) { state.admin = false; sessionSet("isAdmin", null); adminPanel(kinds, settings); }
      });
    };
    document.getElementById("logout").onclick = function () {
      // Revoke it on the Badger too: a copied token must stop working when you lock.
      api("/api/admin/logout", { body: {} }).catch(function () {}).then(function () {
        state.admin = false; sessionSet("isAdmin", null); adminPanel(kinds, settings);
      });
    };
    api("/api/status").then(function (s) {
      document.getElementById("svc").innerHTML = Object.keys(s.services || {}).map(function (k) {
        var on = s.services[k];
        return '<button class="btn ghost" data-svc="' + esc(k) + '" data-act="' + (on ? "stop" : "start") + '">' + esc(k) + (on ? " — stop" : " — start") + "</button>"; }).join("");
      el.querySelectorAll("[data-svc]").forEach(function (b) { b.onclick = function () {
        api("/api/admin/service", { body: { name: b.getAttribute("data-svc"), action: b.getAttribute("data-act") } }).then(function (r) { done(r); state.lib = null; }); }; });
    });
  }

  // ── feature plugins (software/portal/features/*.py + static/features/*.js) ──
  // A feature script calls Badger.register("/supplies", function (ctx) { Badger.h("…"); }) — the path is a
  // string (exact, or ending in "/*" for a prefix) or a RegExp; ctx = {path, qs, match}.
  var featRoutes = [], featList = [], featIds = {}, featuresLoaded = false;
  var GROUPS = { family: "Family", community: "Neighbourhood", radio: "Radio & sky", tools: "More" };
  function featureTiles() {
    if (!featList.length) return "";
    var by = {};
    featList.forEach(function (f) { if (f.route) (by[f.group] = by[f.group] || []).push(f); });
    return Object.keys(GROUPS).filter(function (g) { return by[g]; }).map(function (g) {
      return '<h2 class="section">' + esc(GROUPS[g]) + '</h2><div class="grid">' + by[g].map(function (f) {
        return '<a class="tile" href="#' + esc(f.route) + '"><span class="ico" aria-hidden="true">' + esc(f.icon) + "</span><b>" +
          esc(f.title) + '</b><span class="muted">' + esc(f.description) + "</span>" +
          (f.access === "member" ? '<span class="count">Family only</span>' : "") + "</a>";
      }).join("") + "</div>";
    }).join("");
  }
  window.Badger = {
    register: function (p, fn) {
      var re = p instanceof RegExp ? p : new RegExp("^" + String(p).replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\/\*$/, "(?:/(.*))?") + "$");
      featRoutes.push({ re: re, fn: fn });
    },
    api: api, h: h, esc: esc, href: href, num: num, human: human, ago: ago, tfmt: tfmt,
    view: function () { return view; },
    isMember: function () { return lsGet("isMember", "") === "1"; },   // (a hint; the Badger checks the cookie)
    isAdmin: function () { return !!state.admin; },
    memberName: function () { return lsGet("boardName", ""); },
    upload: function (path, blob, type) { return api(path, { upload: blob, type: type || blob.type }); },
    // a member-only page that nobody has joined from this device yet
    needMember: function (what) { h('<h1>' + esc(what) + '</h1><p>This is for family members. Join with an invite code on the <a href="#/board/family">family board</a> first.</p>'); },
    // fetch a protected file (needs the headers) and give back an object URL for <img>/<a download>
    blobUrl: function (path) {
      return fetch(path, { credentials: "same-origin" }).then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.blob(); })
        .then(function (b) { return URL.createObjectURL(b); });
    }
  };
  function loadFeatures() {
    return fetch("/api/features").then(function (r) { return r.json(); }).then(function (j) {
      featList = (j && j.features) || [];
      featList.forEach(function (f) { featIds[f.id] = true; });
      return Promise.all(featList.filter(function (f) { return f.script && /^\/features\/[\w-]+\.js$/.test(f.script); }).map(function (f) {
        return new Promise(function (ok) {
          var s = document.createElement("script"); s.src = f.script; s.onload = ok;
          s.onerror = function () { console.warn("feature script failed: " + f.script); ok(); };
          document.head.appendChild(s);
        });
      }));
    }).catch(function () {}).then(function () { featuresLoaded = true; route(); refreshStatus(); });
  }

  // ── router ──
  function route() {
    var hash = location.hash.replace(/^#/, "") || "/";
    var qi = hash.indexOf("?"), path = qi >= 0 ? hash.slice(0, qi) : hash, qs = {};
    function dec(x) { try { return decodeURIComponent(x); } catch (e) { return x; } }
    if (qi >= 0) hash.slice(qi + 1).split("&").forEach(function (p) { var kv = p.split("="); qs[dec(kv[0])] = dec((kv[1] || "").replace(/\+/g, " ")); });
    var m;
    if (path === "/") return home();
    if ((m = path.match(/^\/c\/([\w-]+)$/))) return category(m[1]);
    if ((m = path.match(/^\/item\/([\w.\-]+)$/))) return itemView(m[1], qs.path);
    if (path === "/search") return searchView(qs.q || "");
    if (path === "/ask") return askView(qs.q || "");
    if (path === "/board") return boardView("community");
    if (path === "/board/family") return boardView("family");
    if ((m = path.match(/^\/join\/([A-Za-z0-9\-]{4,20})$/))) return boardView("family", m[1]);
    if (path === "/tools") return toolsView();
    if (path === "/files") return filesView(qs.d, qs.p);
    if (path === "/devices") return devicesView();
    if (path === "/status") return statusView();
    if (path === "/settings") return settingsView();
    for (var fi = 0; fi < featRoutes.length; fi++) {
      var fm = path.match(featRoutes[fi].re);
      if (fm) return featRoutes[fi].fn({ path: path, qs: qs, match: fm });
    }
    if (!featuresLoaded) return;                        // a feature page: re-routed once the feature scripts load
    h('<p class="muted">Page not found. <a href="#/">Home</a></p>');
  }

  document.getElementById("searchForm").onsubmit = function (e) {
    e.preventDefault();
    var q = document.getElementById("q").value.trim();
    if (q) location.hash = "#/search?q=" + encodeURIComponent(q);
  };
  window.addEventListener("hashchange", route);
  route();
  loadFeatures();
  refreshWho();
  refreshStatus();
  setInterval(refreshStatus, 30000);

  // Installable app + offline shell (needs a secure context or localhost;
  // on plain http:// LAN addresses browsers skip it and everything still works).
  if ("serviceWorker" in navigator && (window.isSecureContext || location.hostname === "localhost")) {
    navigator.serviceWorker.register("/sw.js").catch(function () {});
  }
})();
