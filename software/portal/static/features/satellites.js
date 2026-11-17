/* Weather satellites — Meteor-M pass times and the pictures the Badger recorded. ES5, no frameworks. */
(function () {
  var timer = null;
  function stop() { if (timer) { clearInterval(timer); timer = null; } }
  window.addEventListener("hashchange", function () { if (!/^#\/satellites(\?|$)/.test(location.hash)) stop(); });

  function tm(t, date) {
    if (!t) return "—";
    if (window.NuTime) return Badger.tfmt(t * 1000, { short: true, date: !!date });
    var d = new Date(t * 1000), hm = ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
    return date ? d.toDateString().slice(4, 10) + " " + hm : hm;
  }
  var DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  function dir(b) { return b == null ? "?" : DIRS[Math.round(b / 45) % 8]; }
  function badge(p) {
    var cls = { record: "ok", recording: "ok", done: "", skip: "warn", off: "" }[p.decision] || "";
    var txt = { record: "will record", recording: "recording", done: "done", skip: "not recorded", off: "times only" }[p.decision] || p.decision;
    return '<span class="pill ' + cls + '">' + Badger.esc(txt) + "</span>";
  }

  /* Which antenna is on SDR 2, what it is good for, and (admins) a switch to say you changed it. */
  var ANT = { adsb: "Aircraft whip (1090 MHz)", satellite: "Satellite V-dipole (137 MHz, outdoors)", both: "Wideband antenna (both)" };
  function antennaCard(r, apiPath) {
    var out = "";
    if (r.sdr2_state === "missing" || r.sdr2_state === "failed") {
      out += '<div class="warnbox">' + (r.sdr2_state === "missing" ? "SDR 2 (the aircraft and satellite radio) isn’t plugged in or can’t be found."
        : "SDR 2 isn’t working" + (r.sdr2_why ? " (" + Badger.esc(r.sdr2_why) + ")" : "") + "; the Badger tries it again in a while.") +
        " Until it is back, aircraft scans borrow the weather radio for a few minutes at a time (weather alerts pause meanwhile)" +
        " and satellite passes are not recorded unless an admin allows it. Check SDR 2’s USB cable inside the case.</div>";
    }
    if (r.problem) out += '<div class="warnbox">' + Badger.esc(r.problem) + "</div>";
    else if (r.note) out += '<p class="small muted">' + Badger.esc(r.note) + "</p>";
    if (r.sdr2_state === "ok" || r.sdr2_state === "failed") {
      out += '<div class="card small"><b>SDR 2 antenna:</b> ' + Badger.esc(ANT[r.antenna] || r.antenna) +
        '<div class="muted">Aircraft need the short 1090 MHz whip on the SDR 2 jack. Satellite pictures need the V-dipole set up outdoors ' +
        "(arms in a V, about 53 cm each, lying flat, on its 3 m cable). Only one antenna fits at a time.</div>";
      if (Badger.isAdmin()) {
        out += '<div class="btns" style="margin-top:8px">' + ["adsb", "satellite", "both"].map(function (k) {
          return '<button class="btn' + (k === r.antenna ? "" : " ghost") + '" data-ant="' + k + '" aria-pressed="' + (k === r.antenna) + '">' + Badger.esc(ANT[k]) + "</button>";
        }).join("") + '</div><div class="muted">Tell the Badger when you change the antenna.</div>';
      }
      out += "</div>";
    }
    return out;
  }
  function wireAntenna(apiPath, after) {
    var bs = document.querySelectorAll("[data-ant]");
    for (var i = 0; i < bs.length; i++) bs[i].onclick = function () {
      var k = this.getAttribute("data-ant");
      Badger.api(apiPath, { method: "POST", body: { antenna: k } }).then(function (r) {
        if (r._status !== 200) alert(r.error || "Not saved."); else setTimeout(after, 500);
      });
    };
  }
  function tleCard(rows) {
    var out = "", worst = null;
    rows.forEach(function (r) { if (!r.loaded) worst = worst || "missing"; else if (r.too_old) worst = "old"; else if (r.approximate && worst !== "old") worst = worst || "stale"; });
    out += rows.map(function (r) {
      return "<div>" + Badger.esc(r.name) + ": " + (r.loaded ? "orbit data " + Badger.num(r.age_days).toFixed(0) + " days old" +
        (r.too_old ? ' <span class="pill bad">too old to use</span>' : r.approximate ? ' <span class="pill warn">times approximate</span>' : ' <span class="pill ok">fresh</span>') :
        '<span class="pill bad">no orbit data</span>') + "</div>";
    }).join("");
    if (worst) out += '<p class="small muted">Orbit data is refreshed by itself whenever the Badger has internet. Without internet, an admin can paste fresh data below (from a phone that has signal: celestrak.org, “Meteor”).</p>';
    return '<div class="card small">' + out + "</div>";
  }

  function draw(j) {
    var el = document.getElementById("satBody"); if (!el) { stop(); return; }
    if (j._status === 429) return;
    if (j._status !== 200) { el.innerHTML = '<p class="muted">Couldn’t reach the Badger.</p>'; return; }
    var out = "";
    if (j.weather_radio === "paused") out += '<div class="warnbox">⚠ Weather alerts are paused for the ' + Badger.esc(j.paused_by || "radio") + " until " + Badger.esc(tm(j.paused_until)) + ".</div>";
    if (j.clock_suspect) out += '<div class="warnbox">The Badger’s clock looks wrong (older than the orbit data). Pass times can’t be trusted until it has GPS or internet time.</div>';
    if (!j.running) out += '<div class="warnbox">The radio service isn’t running, so nothing will be recorded. An admin can check Settings → Services (prepper-sdr).</div>';
    out += '<div class="card"><b>' + (j.state === "recording" ? "🛰️ Recording " + Badger.esc((j.current && j.current.name) || "") + " until " + Badger.esc(tm(j.current && j.current.until)) :
      j.state === "processing" ? "🛰️ Making the picture from the last pass…" : "How recording works") + "</b>" +
      '<p class="small">' + Badger.esc(j.mode || "") + "</p>" +
      (j.device === "weather" ? '<p class="small muted">Used today: ' + Badger.num(j.used_today_min) + " of " + Badger.num(j.daily_max_min) + " minutes.</p>" : "") +
      (j.installed === false && j.device ? '<p class="small">SatDump (the picture decoder) isn’t installed on this Badger.</p>' : "") + "</div>";

    if (j.running) out += antennaCard(j);
    out += '<h2 class="section">Next passes</h2>';
    if (!j.position) out += '<p class="muted">The Badger doesn’t know where it is yet — it needs a GPS fix (or a home location in settings) to work out pass times.</p>';
    var up = (j.passes || []).filter(function (p) { return p.los * 1000 > Date.now() - 600000; }).slice(0, 12);
    if (j.position && !up.length) out += '<p class="muted">No passes worked out yet (orbit data missing?).</p>';
    if (up.length) out += '<div class="list">' + up.map(function (p) {
      return '<div class="card"><div class="row"><div><b>' + Badger.esc(tm(p.aos, true)) + "–" + Badger.esc(tm(p.los)) + "</b> " + Badger.esc(p.name) +
        '<div class="meta">Highest ' + Badger.num(p.max_elev).toFixed(0) + "° · rises in the " + dir(p.aos_az) + ", sets in the " + dir(p.los_az) +
        (p.approximate ? " · approximate" : "") + "</div></div>" + badge(p) + "</div>" +
        (p.reason ? '<p class="small muted">' + Badger.esc(p.reason) + "</p>" : "") + "</div>";
    }).join("") + "</div>";

    out += '<h2 class="section">Orbit data</h2>' + tleCard(j.tle || []);

    out += '<h2 class="section">Pictures</h2>';
    var imgs = j.images || [];
    if (!imgs.length) out += '<p class="muted">No pictures yet.</p>';
    else out += '<div class="list">' + imgs.map(function (im) {
      var src = "/api/satellites/image/" + encodeURIComponent(im.id);
      return '<div class="card"><a href="' + Badger.href(src) + '" target="_blank" rel="noopener"><img src="' + Badger.href(src) +
        '" alt="Weather satellite picture" loading="lazy" style="width:100%;height:auto;max-height:60vh;object-fit:contain;background:#000;border-radius:8px"></a>' +
        '<div class="meta">' + Badger.esc(im.name || "Meteor-M") + " · " + Badger.esc(tm(im.aos || im.created, true)) +
        (im.max_elev != null ? " · highest " + Badger.num(im.max_elev).toFixed(0) + "°" : "") +
        (im.bytes ? " · " + Badger.esc(Badger.human(im.bytes)) : "") + "</div></div>";
    }).join("") + "</div>";
    var hist = (j.history || []).filter(function (h) { return h.result !== "image"; }).slice(-3).reverse();
    if (hist.length) out += '<p class="small muted section">' + hist.map(function (h) {
      return Badger.esc(tm(h.at, true)) + " " + Badger.esc(h.name) + ": " + Badger.esc(h.result) + (h.detail ? " — " + Badger.esc(h.detail) : ""); }).join("<br>") + "</p>";

    out += '<p class="small muted section">Good pictures need the 137 MHz V-dipole on SDR 2, set up outdoors with a clear view of the sky. The aircraft whip, and the weather radio’s folding whip, hear 137 MHz poorly: expect noisy or empty pictures with them.</p>';
    if (Badger.isAdmin()) out += '<h2 class="section">Paste orbit data</h2><div class="card"><p class="small">Two-line elements (TLE) for the satellites above, e.g. from celestrak.org.</p>' +
      '<textarea id="tleText" rows="6" style="width:100%;font-family:monospace" maxlength="20000" aria-label="Orbit data"></textarea>' +
      '<div class="btns"><button class="btn" id="tleSave">Save</button><button class="btn ghost" id="tleFetch">Download now (needs internet)</button><span class="small" id="tleMsg"></span></div></div>';
    el.innerHTML = out;
    wire();
    wireAntenna("/api/satellites/antenna", refresh);
  }

  function wire() {
    var save = document.getElementById("tleSave"), fetchBtn = document.getElementById("tleFetch"), msg = document.getElementById("tleMsg");
    if (save) save.onclick = function () {
      var t = document.getElementById("tleText").value;
      stop();
      Badger.api("/api/satellites/tle", { method: "POST", body: { text: t } }).then(function (r) {
        msg.textContent = r.ok ? "Saved: " + r.loaded.join(", ") + (r.problems && r.problems.length ? " (" + r.problems.join("; ") + ")" : "") :
          (r.error || "Not saved") + (r.problems && r.problems.length ? " " + r.problems.join("; ") : "");
        timer = setInterval(refresh, 30000);
      });
    };
    if (fetchBtn) fetchBtn.onclick = function () {
      fetchBtn.disabled = true;
      Badger.api("/api/satellites/tle/refresh", { method: "POST", body: {} }).then(function (r) { msg.textContent = r.message || r.error || ""; });
    };
  }

  function refresh() {
    var ta = document.getElementById("tleText");
    if (ta && ta.value) return;                // don't wipe what an admin is typing
    Badger.api("/api/satellites").then(draw).catch(function () {});
  }

  Badger.register("/satellites", function () {
    stop();
    Badger.h('<div class="crumbs"><a href="#/">Home</a> › Weather satellites</div><h1>🛰️ Weather satellites</h1><div id="satBody"><p class="muted">Loading…</p></div>');
    refresh();
    timer = setInterval(refresh, 30000);
  });
})();
