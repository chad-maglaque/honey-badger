/* Trusted circle — page script (feature "circles"). ES5, no frameworks. */
(function () {
  var B = window.Badger;
  if (!B) return;
  var esc = B.esc;
  var pendingOffer = null;

  function when(t) { return typeof t === "number" ? esc(B.tfmt(t * 1000, { short: true })) : ""; }

  function adminHtml(d) {
    var out = '<h2 class="section">Circle on the radio</h2><div class="card">';
    if (!d.in_circle) {
      out += '<p>Create a circle, then show its link (QR) <b>in person</b> to the households you trust. Or join theirs.</p>' +
        '<div class="btns"><input type="text" id="cName" maxlength="11" placeholder="Channel name (plain)"><button class="btn" id="cCreate">Create a circle</button></div>' +
        '<label for="cUrl">Join with a circle link someone showed you (needs https://)</label><div class="btns"><input type="text" id="cUrl" placeholder="https://meshtastic.org/e/#…"><button class="btn ghost" id="cJoin">Join</button></div>';
    } else {
      out += "<p>This Badger is in a circle (radio channel " + B.num(d.channel) + ").</p>" +
        '<p class="small muted">The circle link holds its key: family members see it on their own device (over https) and it is on the Badger’s screen (knob → Family radio → Circle link). Never send it by message.</p>' +
        '<div class="btns"><button class="btn ghost" id="cRotate">New circle key</button><button class="btn ghost" id="cLeave">Leave the circle</button></div>' +
        '<p class="small muted">A Badger was lost or stolen, or a household is no longer trusted? Make a new key, block their radio below, and show the new link in person to the households you keep.</p>';
    }
    out += '<div id="cOp" class="small"></div></div>';
    out += '<h2 class="section">What the circle may ask about</h2><div class="card">' +
      '<label for="cMe">This Badger’s name in the circle (a nickname or a street — never an address)</label><div class="btns"><input type="text" id="cMe" maxlength="24" value="' + esc(d.name) + '"><button class="btn ghost" id="cSaveMe">Save</button></div>' +
      '<p class="small">Share, per category (off until you turn it on):</p>' + d.categories.map(function (c) {
        return '<label><input type="checkbox" class="cShare" data-c="' + esc(c.id) + '"' + (d.share[c.id] ? " checked" : "") + "> " + esc(c.label) + "</label><br>";
      }).join("") +
      '<div class="btns"><button class="btn ghost" id="cSaveShare">Save sharing</button></div>' +
      '<p class="small muted">Circle members only ever get a pointer — “power: can fix generators — ask ' + esc(d.name || "you") + '” — and only when one of their radios asks about that one thing. Never where you are, never how much you have, never a list. Nothing is shared in Lockdown.</p></div>';
    out += '<h2 class="section">Offers</h2><div class="card"><div class="list">' + (d.offers.length ? d.offers.map(function (o) {
      return '<div class="result"><b>' + esc(o.topic) + "</b> <span class=\"small muted\">(" + esc(o.category) + (d.share[o.category] ? "" : ", not shared") + ")</span> " + esc(o.text || "") +
        ' <button class="btn ghost" data-rm="' + esc(o.id) + '">Remove</button></div>';
    }).join("") : '<p class="small muted">No offers yet.</p>') + "</div>" +
      '<div class="btns"><select id="oCat">' + d.categories.map(function (c) { return '<option value="' + esc(c.id) + '">' + esc(c.label) + "</option>"; }).join("") + '</select>' +
      '<select id="oTopic"></select><input type="text" id="oText" maxlength="80" placeholder="Short, no amounts, no address"><button class="btn ghost" id="oAdd">Add</button></div>' +
      '<div id="oMsg" class="small"></div>' +
      (window.Badger && d.categories.some(function (c) { return c.id === "aid"; }) ? '<p class="small muted">Aid station: add an “Aid-station status” offer such as “open · water, first aid” (no numbers).</p>' : "") + "</div>";
    out += '<h2 class="section">Blocked radios</h2><div class="card"><p class="small">' + ((d.blocked || []).map(function (b) {
      return '<span class="mono">' + esc(b.radio) + "</span> " + esc(b.label || "") + ' <button class="btn ghost" data-unblock="' + esc(b.radio) + '">×</button>';
    }).join("<br>") || "None.") + '</p><div class="btns"><input type="text" id="bRadio" maxlength="9" placeholder="!a1b2c3d4" style="width:9em"><input type="text" id="bLabel" maxlength="32" placeholder="Note (optional)"><button class="btn ghost" id="bAdd">Block</button></div></div>';
    out += '<h2 class="section">Who asked</h2><div class="card small">' + ((d.flags || []).length ? '<p class="warnbox">Refused and flagged: ' + d.flags.map(function (f) {
      return '<span class="mono">' + esc(f.from) + "</span> " + esc(f.what) + " (" + when(f.at) + ")";
    }).join("; ") + "</p>" : "") + ((d.log || []).length ? d.log.slice().reverse().map(function (l) {
      return "<div>" + when(l.at) + ' · <span class="mono">' + esc(l.from) + "</span> · " + esc(l.topic) + " · " + esc(l.result) + "</div>";
    }).join("") : '<p class="muted">Nobody yet.</p>') + "</div>";
    return out;
  }

  function render(d, msg) {
    var out = '<div class="crumbs"><a href="#/settings">Settings</a> › Trusted circle</div><h1>🤝 Trusted circle</h1>' +
      '<p class="small muted">A few households you have met and trust, whose Badgers share a private radio channel. Records stay on each Badger; the circle only learns who to ask.</p>';
    if (msg) out += '<div class="card">' + msg + "</div>";
    if (!d.sharing_allowed) out += '<p class="warnbox">Lockdown: nothing is shared with the circle and no questions go out.</p>';
    if (!d.bridge_running) out += '<p class="small muted">The radio bridge isn’t reporting right now (is the radio plugged in?).</p>';
    if (d.in_circle) {
      out += '<h2 class="section">Ask the circle</h2><div class="card"><div class="btns"><select id="aTopic">' + d.topics.map(function (t) {
        return '<option value="' + esc(t) + '">' + esc(t) + "</option>"; }).join("") + '</select><button class="btn" id="aAsk">Who can help?</button></div><div id="aMsg" class="small"></div>' +
        '<div class="list">' + (d.answers.length ? d.answers.slice().reverse().map(function (a) {
          return '<div class="result"><b>' + esc(a.text) + '</b><div class="small muted">' + when(a.at) + " · " + esc(a.topic) + ' · radio <span class="mono">' + esc(a.from) + "</span></div></div>";
        }).join("") : '<p class="small muted">No answers yet.</p>') + "</div>" +
        (B.isMember() ? '<div class="btns"><button class="btn ghost" id="cLink">Show the circle link</button></div><div id="cLinkOut" class="small"></div>' : "") + "</div>";
    } else if (!B.isAdmin()) {
      out += "<p>This Badger isn’t in a trusted circle. The admin can create or join one.</p>";
    }
    if (B.isAdmin()) out += adminHtml(d);
    B.h(out);
    wire(d);
  }

  function reload(msg) { B.api("/api/circles").then(function (d) { if (d && d.topics) render(d, msg); }); }

  function poll(id, tries) {
    B.api("/api/circles/op?id=" + encodeURIComponent(id)).then(function (r) {
      var el = document.getElementById("cOp");
      if (r.done) return reload(r.result.ok ? "Done (" + esc(r.result.op) + ")." : esc(r.result.error || "The radio said no."));
      if (el) el.textContent = "Waiting for the radio…";
      if (tries > 0) setTimeout(function () { poll(id, tries - 1); }, 2000);
      else if (el) el.textContent = "No answer from the radio bridge yet. Is the radio plugged in?";
    });
  }

  function op(body, confirmText) {
    if (confirmText && !confirm(confirmText)) return;
    B.api("/api/circles/op", { body: body }).then(function (r) {
      if (!r.ok) return render(lastData, esc(r.error || "That didn’t work."));
      poll(r.id, 15);
    });
  }

  var lastData = null;
  function wire(d) {
    lastData = d;
    function on(id, fn) { var el = document.getElementById(id); if (el) el.onclick = fn; }
    function val(id) { var el = document.getElementById(id); return el ? el.value : ""; }
    on("aAsk", function () {
      B.api("/api/circles/ask", { body: { topic: val("aTopic") } }).then(function (r) {
        document.getElementById("aMsg").textContent = r.ok ? r.message : (r.error || "That didn’t work.");
        if (r.ok) setTimeout(function () { reload(); }, 30000);
      });
    });
    on("cLink", function () {
      B.api("/api/circles/link").then(function (x) {
        var o = document.getElementById("cLinkOut");
        o.innerHTML = x.url ? '<p class="mono" style="word-break:break-all">' + esc(x.url) + '</p><p>Show it only in person. Whoever has it is in the circle.</p>' : esc(x.message || x.error || "Not available.");
      });
    });
    on("cCreate", function () { op({ op: "create", name: val("cName") || "Circle" }); });
    on("cJoin", function () { op({ op: "join", url: val("cUrl") }); });
    on("cRotate", function () { op({ op: "rotate" }, "Make a new circle key? Every household must get the new link from you in person."); });
    on("cLeave", function () { op({ op: "leave" }, "Leave the circle? The radio forgets its key."); });
    on("cSaveMe", function () { saveSettings({ name: val("cMe") }); });
    on("cSaveShare", function () {
      var share = {};
      Array.prototype.forEach.call(document.querySelectorAll(".cShare"), function (c) { share[c.getAttribute("data-c")] = !!c.checked; });
      saveSettings({ share: share });
    });
    var cat = document.getElementById("oCat");
    function topics() {
      var c = null;
      d.categories.forEach(function (x) { if (x.id === cat.value) c = x; });
      document.getElementById("oTopic").innerHTML = (c ? c.topics : []).map(function (t) { return '<option value="' + esc(t) + '">' + esc(t) + "</option>"; }).join("");
    }
    if (cat) { cat.onchange = topics; topics(); }
    on("oAdd", function () { addOffer({ op: "add", category: val("oCat"), topic: val("oTopic"), text: val("oText") }); });
    Array.prototype.forEach.call(document.querySelectorAll("[data-rm]"), function (b) {
      b.onclick = function () { B.api("/api/circles/offer", { body: { op: "remove", id: b.getAttribute("data-rm") } }).then(function () { reload(); }); };
    });
    on("bAdd", function () { block(val("bRadio"), true, val("bLabel")); });
    Array.prototype.forEach.call(document.querySelectorAll("[data-unblock]"), function (b) {
      b.onclick = function () { block(b.getAttribute("data-unblock"), false, ""); };
    });
  }

  function saveSettings(body, again) {
    if (again) body.confirm = true;
    B.api("/api/circles/settings", { body: body }).then(function (r) {
      if (r.needs_confirm && confirm(r.warning + "\n\nShare anyway?")) return saveSettings(body, true);
      reload(r.ok ? "Saved." : esc(r.warning || r.error || "That didn’t work."));
    });
  }

  function addOffer(body) {
    pendingOffer = body;
    B.api("/api/circles/offer", { body: body }).then(function (r) {
      if (r.needs_confirm && confirm(r.warning + "\n\nShare anyway?")) { pendingOffer.confirm = true; return addOffer(pendingOffer); }
      reload(r.ok ? (r.shared ? "Added. The circle can ask about it." : "Added — not shared until you turn that category on (and not in Lockdown).") : esc(r.warning || r.error || "That didn’t work."));
    });
  }

  function block(radio, yes, label) {
    B.api("/api/circles/block", { body: { radio: radio, block: yes, label: label } }).then(function (r) {
      reload(r.ok ? (yes ? "Blocked." : "Unblocked.") : esc(r.error || "That didn’t work."));
    });
  }

  B.register("/circles", function () {
    B.h('<h1>🤝 Trusted circle</h1><p class="muted">Loading…</p>');
    B.api("/api/circles").then(function (d) {
      if (!d || !d.topics) return B.h('<h1>🤝 Trusted circle</h1><p>For family members and the admin. <a href="#/settings">Settings</a>.</p>');
      render(d);
    });
  });
})();
