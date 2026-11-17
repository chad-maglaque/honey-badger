/* Security posture — page script (feature "posture"). ES5, no frameworks. */
(function () {
  var B = window.Badger;
  if (!B) return;
  var esc = B.esc;
  var SRC = { posture: "", override: "your setting", ignored: "your setting is ignored in Lockdown" };

  function table(rows) {
    return '<table class="files"><tr><th>What</th><th>Now</th><th></th></tr>' + rows.map(function (r) {
      var note = r.source === "posture" ? "" : '<div class="small muted">' + esc(SRC[r.source] || r.source) +
        (r.from ? ": " + esc(r.from) : "") + (r.source !== "posture" ? " · the posture says: " + esc(r.posture) : "") + "</div>";
      return "<tr><td>" + esc(r.label) + "</td><td><b>" + esc(r.value) + "</b>" + note + "</td><td></td></tr>";
    }).join("") + "</table>";
  }

  function render(d, msg) {
    var admin = B.isAdmin();
    var out = '<div class="crumbs"><a href="#/settings">Settings</a> › Security posture</div><h1>🛡️ Security posture</h1>';
    if (msg) out += '<div class="card">' + msg + "</div>";
    if (d.damaged) out += '<p class="warnbox">The posture file was damaged, so the Badger treats itself as in <b>Lockdown</b> until you choose again.</p>';
    out += '<div class="card"><p>Now: <b>' + esc(d.label) + "</b>" + (d.changed_at ? ' <span class="small muted">since ' +
      esc(B.tfmt(d.changed_at * 1000, { date: true, short: true })) + (d.by ? " · " + esc(d.by) : "") + "</span>" : "") + "</p>" +
      '<div class="list">' + d.levels.map(function (l) {
        return '<div class="result"><b>' + esc(l.label) + (l.id === d.level ? " ✓" : "") + '</b><div class="small">' + esc(l.blurb) + "</div>" +
          (admin && l.id !== d.level ? '<div class="btns"><button class="btn' + (l.id === "lockdown" ? "" : " ghost") + '" data-lv="' + esc(l.id) + '">Switch to ' + esc(l.label) + "</button>" +
            (l.id === "lockdown" ? '<button class="btn ghost" data-lv="lockdown" data-wifi="off">Lockdown and turn the Wi-Fi off</button>' : "") + "</div>" : "") + "</div>";
      }).join("") + "</div>" +
      (admin ? "" : '<p class="small muted">Only the admin (or someone at the Badger’s screen) can change it.</p>') +
      (d.wifi_open && d.level !== "lockdown" ? '<p class="small">The Wi-Fi has no password now. Lockdown gives it one (the usual safe change: it switches back by itself unless someone rejoins and confirms within 10 minutes; the Badger’s screen shows the password).</p>' : "") +
      "</div>" +
      '<h2 class="section">What it changes</h2><div class="card">' + table(d.settings) +
      (admin ? '<div class="btns section"><button class="btn ghost" id="clrOv">Use the posture’s own radio commands and visitor settings</button></div>' : "") +
      '<p class="small muted">The posture sets all of these at once. A line marked “your setting” comes from prepper.toml or the Settings page and wins — except in Lockdown, where a setting that shows more than Lockdown allows is ignored.</p></div>' +
      '<p class="small"><a href="#/reveal">What this Badger reveals →</a> · <a href="#/circles">Trusted circle →</a></p>';
    B.h(out);
    Array.prototype.forEach.call(document.querySelectorAll("[data-lv]"), function (b) {
      b.onclick = function () {
        var lv = b.getAttribute("data-lv"), wifi = b.getAttribute("data-wifi");
        if (lv === "lockdown" && !confirm("Lockdown: radio silence, no sharing, visitors see only the library" + (wifi ? ", and the Wi-Fi goes off" : "") + ". Continue?")) return;
        var body = { level: lv };
        if (wifi) body.wifi = wifi;
        B.api("/api/posture", { body: body }).then(function (r) {
          if (!r.ok) return render(d, esc(r.error || "That didn’t work."));
          var m = "Posture is now <b>" + esc(r.posture.label) + "</b>.";
          if (r.wifi && r.wifi.action === "password" && r.wifi.ok)
            m += r.wifi.passphrase ? " New Wi-Fi password: <b class=\"mono\">" + esc(r.wifi.passphrase) + "</b>." : " The new Wi-Fi password is on the Badger’s screen (knob → Join Wi-Fi).";
          if (r.wifi && r.wifi.action === "password" && !r.wifi.ok) m += " The Wi-Fi password could not be set: " + esc(r.wifi.output || "");
          if (r.wifi && r.wifi.action === "off") m += r.wifi.ok ? " The Wi-Fi is turning off." : " The Wi-Fi could not be turned off.";
          (r.notes || []).forEach(function (n) { m += '<p class="small">' + esc(n) + "</p>"; });
          render(r.posture, m);
        });
      };
    });
    var clr = document.getElementById("clrOv");
    if (clr) clr.onclick = function () {
      B.api("/api/posture/overrides", { body: { clear: ["mesh_commands", "mesh_public_position", "visitor_radio_pages", "visitor_community"] } }).then(function (r) {
        render(r.posture || d, r.ok ? "Back to the posture’s own settings." : esc(r.error || "That didn’t work."));
      });
    };
  }

  B.register("/posture", function () {
    B.h('<h1>🛡️ Security posture</h1><p class="muted">Loading…</p>');
    B.api("/api/posture").then(function (d) {
      if (!d || !d.levels) return B.h('<h1>🛡️ Security posture</h1><p>For the admin and family members. <a href="#/settings">Sign in on the Settings page</a>.</p>');
      render(d);
    });
  });
})();
