/* NOAA forecast as text — page script (feature "forecast"). ES5, no frameworks. */
(function () {
  var B = window.Badger;
  if (!B) return;
  var esc = B.esc;

  function when(at) {
    if (typeof at !== "number") return "unknown time";
    var ms = at * 1000;
    var txt = window.NuTime ? B.tfmt(ms) : new Date(ms).toLocaleString();
    return esc(txt) + " (" + esc(B.ago(new Date(ms).toISOString())) + ")";
  }

  function para(text) {
    return String(text || "").split(/\n+/).map(function (l) { return "<p>" + esc(l) + "</p>"; }).join("");
  }

  function sectionHtml(s) {
    var out = '<div class="card section"><h2>' + esc(s.title) + "</h2>";
    if (s.kind === "forecast" && s.periods && s.periods.length) {
      if (s.intro) out += '<p class="muted small">' + esc(s.intro) + "</p>";
      out += '<div class="list">' + s.periods.map(function (p) {
        return '<div class="result"><b>' + esc(p.name) + "</b><div>" + esc(p.text) + "</div></div>";
      }).join("") + "</div>";
    } else {
      out += para(s.text);
    }
    return out + "</div>";
  }

  function forecastHtml(f, older) {
    if (!f) return "";
    var out = '<div class="warnbox">' + esc(NOTE) + "</div>";
    out += '<p class="small muted section">Captured ' + when(f.at) +
      (f.freq ? " · " + esc(f.freq) + " MHz" : "") + (older ? " · <b>older version</b>" : "") +
      (f.new === false ? " · same forecast as the time before" : "") + "</p>";
    if (f.short) out += '<div class="card"><h3>In short</h3><p class="mono">' + esc(f.short) + '</p><p class="small muted">' +
      (f.short_by === "ai" ? "Summarised by the Badger’s offline AI." : "Summarised automatically.") + "</p></div>";
    var secs = f.sections || [];
    out += secs.length ? secs.map(sectionHtml).join("") : '<p class="muted">Nothing readable in this capture.</p>';
    if (f.raw) out += '<details class="section"><summary class="small">Word-for-word transcript</summary><p class="small muted">' +
      esc(f.raw) + "</p></details>";
    return out;
  }

  var NOTE = "Automatically transcribed from NOAA Weather Radio — may contain errors.";
  var STATE_TEXT = { running: "⏳ ", no_signal: "📡 ", no_radio: "📻 ", no_whisper: "🧩 ", busy: "⏳ ", error: "⚠️ ", skipped: "🔋 ", ok: "" };

  function statusHtml(r) {
    var st = r.status || {}, out = "";
    if (!r.available) {
      out += '<div class="card"><h2>Speech recognition isn’t installed</h2><p>' + esc(r.install_hint) +
        '</p><p class="small muted">Until then this page stays empty; weather alerts still work.</p></div>';
    }
    if (st.state && st.state !== "ok") {
      out += '<div class="card section"><p>' + (STATE_TEXT[st.state] || "") + esc(st.message || st.state) + "</p>" +
        (st.at ? '<p class="small muted">' + when(st.at) + "</p>" : "") + "</div>";
    }
    if (r.update_pending) out += '<p class="small muted">A new capture has been asked for — it starts within a minute.</p>';
    return out;
  }

  function adminHtml(r) {
    if (!B.isAdmin() || !r.settings) return "";
    var s = r.settings;
    return '<div class="card section"><h2>Admin</h2>' +
      '<div class="btns"><button class="btn" id="fcNow">Update now</button><span class="small muted" id="fcMsg"></span></div>' +
      '<p class="section"><label><input type="checkbox" id="fcBoard"' + (s.post_board ? " checked" : "") +
      "> Post new forecasts on the community board (at most every 6 hours)</label></p>" +
      '<p><label><input type="checkbox" id="fcMesh"' + (s.post_mesh ? " checked" : "") +
      "> Send the short version to the mesh community channel (at most twice a day)</label></p>" +
      '<p><label for="fcEvery">Listen every</label> <select id="fcEvery">' + [1, 2, 3, 4, 6, 8, 12, 24].map(function (h) {
        return '<option value="' + h + '"' + (h === s.interval_hours ? " selected" : "") + ">" + h + " h</option>";
      }).join("") + "</select></p></div>";
  }

  function wireAdmin() {
    var now = document.getElementById("fcNow");
    if (!now) return;
    var msg = document.getElementById("fcMsg");
    now.onclick = function () {
      now.disabled = true;
      B.api("/api/forecast/update", { body: {} }).then(function (r) {
        msg.textContent = r.message || r.error || "";
      }).catch(function () { msg.textContent = "The Badger didn’t answer."; }).then(function () { now.disabled = false; });
    };
    function save() {
      B.api("/api/forecast/settings", { body: {
        post_board: document.getElementById("fcBoard").checked,
        post_mesh: document.getElementById("fcMesh").checked,
        interval_hours: parseInt(document.getElementById("fcEvery").value, 10)
      } }).then(function (r) { msg.textContent = r.ok ? "Saved." : (r.error || "Not saved."); })
        .catch(function () { msg.textContent = "The Badger didn’t answer."; });
    }
    ["fcBoard", "fcMesh", "fcEvery"].forEach(function (id) { document.getElementById(id).onchange = save; });
  }

  function historyHtml(r, currentId) {
    var h = (r.history || []).filter(function (i) { return i.id !== currentId; });
    if (!h.length) return "";
    return '<h2 class="section">Older versions</h2><div class="list">' + h.slice(0, 40).map(function (i) {
      return '<div class="result"><a href="#/forecast?id=' + encodeURIComponent(i.id) + '">' + when(i.at) + "</a>" +
        (i.new ? "" : ' <span class="small muted">(unchanged)</span>') + '<div class="small muted">' + esc(i.headline) + "</div></div>";
    }).join("") + "</div>";
  }

  B.register("/forecast", function (ctx) {
    B.h('<h1>📻 NOAA forecast</h1><p class="muted">Loading…</p>');
    var id = ctx.qs && ctx.qs.id;
    B.api("/api/forecast").then(function (r) {
      if (r._status !== 200) throw new Error(r.error || "error");
      NOTE = r.note || NOTE;
      var head = '<h1>📻 NOAA forecast</h1><p class="muted">The Badger listens to NOAA Weather Radio every ' +
        esc(r.interval_hours) + " hours and writes down the forecast it hears.</p>";
      if (id && /^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$/.test(id)) {
        return B.api("/api/forecast/item?id=" + encodeURIComponent(id)).then(function (f) {
          B.h(head + '<p><a href="#/forecast">← Latest forecast</a></p>' +
            (f._status === 200 ? forecastHtml(f, true) : '<p class="muted">' + esc(f.error || "Not found.") + "</p>") +
            historyHtml(r, id));
        });
      }
      var body = head + statusHtml(r);
      body += r.latest ? forecastHtml(r.latest, false) :
        (r.available ? '<p class="muted section">No forecast captured yet. The first one appears a few minutes after the weather radio starts listening.</p>' : "");
      body += adminHtml(r) + historyHtml(r, r.latest && r.latest.id);
      B.h(body);
      wireAdmin();
    }).catch(function (e) {
      B.h('<h1>📻 NOAA forecast</h1><p class="muted">Couldn’t load the forecast (' + esc((e && e.message) || "no answer") + ").</p>");
    });
  });
})();
