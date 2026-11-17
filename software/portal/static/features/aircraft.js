/* Aircraft — planes overhead, decoded from their ADS-B broadcasts by the Badger's radio.
   One radio: on demand ("Scan aircraft", up to 15 min, pauses weather alerts meanwhile).
   Second radio: watched all the time. ES5, no frameworks. */
(function () {
  var timer = null, lastReq = null;
  function stop() { if (timer) { clearInterval(timer); timer = null; } }
  window.addEventListener("hashchange", function () { if (!/^#\/aircraft(\?|$)/.test(location.hash)) stop(); });

  function tm(t) {
    if (!t) return "—";
    if (window.NuTime) return Badger.tfmt(t * 1000, { short: true });
    var d = new Date(t * 1000); return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
  }
  var DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  function dir(b) { return b == null ? "" : DIRS[Math.round(b / 45) % 8]; }
  function mi(km) { return km * 0.621371; }
  function alt(a) {
    if (a === "ground") return "on the ground";
    if (a == null) return "—";
    return String(Math.round(a / 100) * 100).replace(/\B(?=(\d{3})+(?!\d))/g, ",") + " ft";
  }
  function climb(v) { if (v == null || Math.abs(v) < 300) return ""; return v > 0 ? " ↑" : " ↓"; }
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
  var EMERG = { general: "EMERGENCY", lifeguard: "medical flight", minfuel: "low fuel", nordo: "no radio", unlawful: "HIJACK code", downed: "aircraft down" };

  function radioCard(r) {
    var out = "", can = Badger.isMember() || Badger.isAdmin();
    if (r.weather_radio === "paused") {
      out += '<div class="warnbox">⚠ Weather alerts are paused for the ' + Badger.esc(r.paused_by || "radio") + " until " + Badger.esc(tm(r.paused_until)) +
        ". The weather radio listens again right after.</div>";
    }
    out += '<div class="card"><div class="row"><div style="flex:1;min-width:200px">';
    if (!r.running) {
      out += "<b>The radio service isn't running.</b><p class=\"small muted\">An admin can check Settings → Services (prepper-sdr).</p>";
    } else if (r.state === "always_on" || r.state === "restarting") {
      out += "<b>📡 Watching all the time</b> on SDR 2." + (r.state === "restarting" ? ' <span class="pill warn">restarting</span>' : "");
    } else if (r.state === "scanning" || r.state === "starting") {
      out += "<b>📡 Scanning" + (r.until ? " until " + Badger.esc(tm(r.until)) : "") + "</b>" +
        (r.device === "weather" ? '<p class="small muted">Uses the weather radio; it goes back to listening for alerts when the scan ends.</p>' : "");
    } else if (r.state === "unavailable") {
      out += "<b>The aircraft decoder isn't installed on this Badger.</b>";
    } else if (r.state === "off") {
      out += "<b>Aircraft scanning is turned off.</b>";
    } else if (r.dedicated && r.antenna === "satellite") {
      out += "<b>Not listening for aircraft right now.</b>" +
        '<p class="small muted">SDR 2 has the satellite antenna on, so aircraft are on demand. A scan runs for up to ' +
        Badger.num(r.scan_minutes || 15) + " minutes and doesn’t affect weather alerts. For all-day aircraft, put the 1090 MHz whip back on SDR 2.</p>";
    } else if (r.dedicated) {
      out += "<b>Starting the aircraft receiver…</b>";
    } else {
      out += "<b>Not listening for aircraft right now.</b>" +
        '<p class="small muted">SDR 2 isn’t available, so a scan borrows the weather radio for up to ' +
        Badger.num(r.scan_minutes || 15) + " minutes (weather alerts pause meanwhile, never while an alert is live)." +
        (r.scans_left != null ? " Scans left today: " + Badger.num(r.scans_left) + "." : "") + "</p>";
    }
    if (r.message && (r.request === lastReq || !lastReq)) out += '<p class="small">' + Badger.esc(r.message) + "</p>";
    out += '</div><div class="btns">';
    var canScan = r.running && r.state === "idle" && !(r.dedicated && r.antenna !== "satellite");
    if (canScan && can) out += '<button class="btn" id="acScan">✈️ Scan aircraft for ' + Badger.num(r.scan_minutes || 15) + " min</button>";
    if (r.running && r.state === "scanning" && can) out += '<button class="btn ghost" id="acStop">Stop scan</button>';
    out += "</div></div>";
    if (canScan && !can) out += '<p class="small muted">Family members can start a scan. <a href="#/board/family">Join the family</a>.</p>';
    return out + "</div>" + (r.running ? antennaCard(r) : "");
  }

  function rows(list) {
    if (!list.length) return '<p class="muted">No aircraft heard right now.</p>';
    return '<table class="files"><tr class="small muted"><td>Flight</td><td>Height</td><td>Speed</td><td>Where</td></tr>' + list.map(function (a) {
      var name = a.flight || ("#" + a.hex.toUpperCase());
      var em = a.emergency ? ' <span class="pill bad">' + Badger.esc(EMERG[a.emergency] || a.emergency) + "</span>" : "";
      var where = a.dist_km != null ? Math.round(mi(a.dist_km)) + " mi " + dir(a.bearing) + ' <span class="small muted">(' + Math.round(a.dist_km) + " km)</span>" :
        (a.lat == null ? '<span class="small muted">no position</span>' : '<span class="small muted">' + a.lat.toFixed(2) + ", " + a.lon.toFixed(2) + "</span>");
      return "<tr><td><b>" + Badger.esc(name) + "</b>" + em + (a.squawk ? '<div class="small muted">squawk ' + Badger.esc(a.squawk) + "</div>" : "") + "</td>" +
        "<td>" + Badger.esc(alt(a.alt_ft)) + climb(a.vrate_fpm) + "</td>" +
        "<td>" + (a.speed_kt != null ? Math.round(a.speed_kt * 1.15078) + " mph" : "—") + "</td>" +
        "<td>" + where + "</td></tr>";
    }).join("") + "</table>";
  }

  function draw(j) {
    var el = document.getElementById("acBody"); if (!el) { stop(); return; }
    if (j._status === 429) return;
    if (j._status !== 200) { el.innerHTML = '<p class="muted">Couldn’t reach the Badger.</p>'; return; }
    var r = j.radio || {}, list = j.aircraft || [];
    var origin = j.origin ? '<p class="small muted">Distances from the Badger (' + Badger.esc(j.origin.source) + ").</p>" :
      '<p class="small muted">The Badger doesn’t know where it is yet (no GPS fix), so distances aren’t shown.</p>';
    el.innerHTML = radioCard(r) +
      '<h2 class="section">' + (list.length ? Badger.num(list.length) + " aircraft" : "Aircraft") + (j.stale && r.state !== "idle" ? ' <span class="pill warn">waiting for data</span>' : "") + "</h2>" +
      (j.stale && r.state === "idle" ? '<p class="muted">No aircraft data right now.</p>' : rows(list)) + (list.length ? origin : "") +
      '<div class="btns section"><a class="btn ghost" href="/maps/?aircraft=1">🗺️ Show on the map</a></div>' +
      '<p class="small muted">Planes broadcast their position, height and speed openly (ADS-B, 1090 MHz). Small planes, gliders and military flights often don’t. Range is about 50–150 miles, less with the short whip and indoors.</p>';
    var b = document.getElementById("acScan");
    if (b) b.onclick = function () { send("/api/aircraft/scan", b); };
    var s = document.getElementById("acStop");
    if (s) s.onclick = function () { send("/api/aircraft/stop", s); };
    wireAntenna("/api/aircraft/antenna", refresh);
  }

  function send(path, btn) {
    btn.disabled = true;
    Badger.api(path, { method: "POST", body: {} }).then(function (r) {
      if (r.id) lastReq = r.id;
      if (r._status >= 400) alert(r.error || "Not possible right now.");
      setTimeout(refresh, 1500);
    }).catch(function () { btn.disabled = false; alert("Couldn’t reach the Badger."); });
  }

  function refresh() { Badger.api("/api/aircraft").then(draw).catch(function () {}); }

  Badger.register("/aircraft", function () {
    stop();
    Badger.h('<div class="crumbs"><a href="#/">Home</a> › Aircraft</div><h1>✈️ Aircraft</h1><div id="acBody"><p class="muted">Loading…</p></div>');
    refresh();
    timer = setInterval(refresh, 5000);
  });
})();
