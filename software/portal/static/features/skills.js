/* Skills & resources directory: browse, search, add; edit/remove your own (edit tokens kept in
   localStorage "skillsTokens"); admin hides/removes. ES5 only. */
(function () {
  "use strict";
  var B = window.Badger;
  if (!B) return;
  var esc = B.esc;
  var CATS = { medical: "🩺 Medical & first aid", trades: "🔧 Trades & repair", radio: "📻 Radio & comms", care: "🧒 Childcare & caring",
    language: "🗣️ Language & translation", food: "🥫 Food & cooking", water: "💧 Water", power: "⚡ Power & fuel",
    tools: "🪚 Tools & equipment", transport: "🚙 Transport", shelter: "🏠 Shelter & space", other: "➕ Other" };
  var ORDER = ["medical", "trades", "radio", "care", "language", "food", "water", "power", "tools", "transport", "shelter", "other"];
  var view = { kind: "offer", cat: "", q: "" };

  function tokens() { try { return JSON.parse(localStorage.getItem("skillsTokens") || "{}") || {}; } catch (e) { return {}; } }
  function saveTokens(t) { try { localStorage.setItem("skillsTokens", JSON.stringify(t)); } catch (e) {} }
  function $(id) { return document.getElementById(id); }
  // full-width text fields in a form card (style.css only stretches textareas)
  function wide(root) { root.querySelectorAll("input[type=text], select").forEach(function (i) { i.style.width = "100%"; i.style.boxSizing = "border-box"; }); }

  B.register("/skills", function (ctx) {
    if (ctx.qs && (ctx.qs.kind === "need" || ctx.qs.kind === "offer" || ctx.qs.kind === "all")) view.kind = ctx.qs.kind;
    B.h('<h1>🤝 Skills & resources</h1>' +
      '<p class="muted">Neighbours list what they can offer — skills, tools, power, water — and what they need.</p>' +
      '<div class="warnbox">Everything here can be seen by <b>anyone on the Badger’s Wi-Fi</b>. Share only what you’re comfortable with: a first name and “ask at the blue house on Oak St” is enough.</div>' +
      '<div class="btns" role="tablist" id="skTabs"></div>' +
      '<form id="skFind" class="btns" style="margin-top:8px"><input type="text" id="skQ" maxlength="100" placeholder="Search: nurse, generator, Spanish…" style="flex:1;min-width:160px">' +
      '<select id="skCat"><option value="">All categories</option>' + ORDER.map(function (c) { return '<option value="' + c + '">' + esc(CATS[c]) + "</option>"; }).join("") + "</select>" +
      '<button class="btn ghost">Search</button></form>' +
      '<div class="btns section"><button class="btn" id="skAdd">➕ Add an offer or need</button></div><div id="skForm"></div>' +
      '<div id="skList" class="section muted">Loading…</div>');
    $("skQ").value = view.q; $("skCat").value = view.cat;
    tabs();
    $("skFind").onsubmit = function (e) { e.preventDefault(); view.q = $("skQ").value.trim(); view.cat = $("skCat").value; load(); };
    $("skCat").onchange = function () { view.cat = $("skCat").value; load(); };
    $("skAdd").onclick = function () { form(null); };
    load();
  });

  function tabs() {
    $("skTabs").innerHTML = [["offer", "Offers"], ["need", "Needs"], ["all", "All"]].map(function (t) {
      return '<button class="btn' + (view.kind === t[0] ? "" : " ghost") + '" data-kind="' + t[0] + '" aria-selected="' + (view.kind === t[0]) + '">' + t[1] + "</button>";
    }).join("");
    $("skTabs").querySelectorAll("[data-kind]").forEach(function (b) {
      b.onclick = function () { view.kind = b.getAttribute("data-kind"); tabs(); load(); };
    });
  }

  function load() {
    var qs = "?q=" + encodeURIComponent(view.q) + "&category=" + encodeURIComponent(view.cat) + "&kind=" + (view.kind === "all" ? "" : view.kind);
    B.api("/api/skills" + qs).then(function (r) {
      var box = $("skList"); if (!box) return;
      if (r._status !== 200) { box.textContent = r.error || "Couldn’t load the directory."; return; }
      var mine = tokens(), admin = B.isAdmin();
      if (!view.q && !view.cat && view.kind === "all" && !admin) {        // forget tokens of entries that expired or were removed
        var ids = {}; r.entries.forEach(function (e) { ids[e.id] = 1; });
        Object.keys(mine).forEach(function (k) { if (!ids[k]) delete mine[k]; }); saveTokens(mine);
      }
      box.className = "section";
      box.innerHTML = (r.entries.length ? '<div class="list">' + r.entries.map(function (e) { return card(e, mine[e.id], admin); }).join("") + "</div>"
        : '<p class="muted">' + (view.q || view.cat ? "Nothing matches. Try other words, or post a need." : "Nothing listed yet. Be the first!") + "</p>") +
        (r.expire_days ? '<p class="small muted">Entries disappear after ' + B.num(r.expire_days) + " days unless their owner renews them.</p>" : "");
      wire(box, r.entries);
    });
  }

  function card(e, tok, admin) {
    var need = e.kind === "need";
    return '<div class="card"' + (e.hidden ? ' style="opacity:.6"' : "") + '><div class="row"><div style="flex:1;min-width:200px">' +
      '<div class="meta">' + (need ? '<span class="pill warn">NEED</span> ' : '<span class="pill ok">OFFER</span> ') + esc(CATS[e.category] || e.category) +
      (e.hidden ? ' <span class="pill bad">hidden</span>' : "") + (admin && e.flags ? ' <span class="pill bad">reported ×' + B.num(e.flags) + "</span>" : "") + "</div>" +
      "<h3>" + esc(e.title) + "</h3>" +
      (e.name ? '<p class="small"><b>' + esc(e.name) + "</b></p>" : "") +
      (e.availability ? '<p class="small">🕒 ' + esc(e.availability) + "</p>" : "") +
      (e.contact ? '<p class="small">📍 ' + esc(e.contact) + "</p>" : "") +
      (e.details ? '<p class="small" style="white-space:pre-wrap">' + esc(e.details) + "</p>" : "") +
      '<div class="meta">Updated ' + esc(B.tfmt(e.updated, { date: true, short: true })) + "</div></div>" +
      '<div class="btns">' +
      (tok ? '<button class="btn ghost" data-edit="' + esc(e.id) + '">Edit</button><button class="btn ghost" data-renew="' + esc(e.id) + '">Still current</button><button class="btn ghost" data-del="' + esc(e.id) + '">Remove</button>' : "") +
      (admin ? '<button class="btn ghost" data-mod="' + (e.hidden ? "unhide" : "hide") + '" data-id="' + esc(e.id) + '">' + (e.hidden ? "Unhide" : "Hide") + "</button>" +
        '<button class="btn danger" data-mod="remove" data-id="' + esc(e.id) + '">Remove</button>' +
        (e.flags ? '<button class="btn ghost" data-mod="clear_flags" data-id="' + esc(e.id) + '">Clear reports</button>' : "") : "") +
      (!tok && !admin ? '<button class="btn ghost small" data-flag="' + esc(e.id) + '" title="Tell the Badger’s owner this looks wrong or abusive">Report</button>' : "") +
      "</div></div></div>";
  }

  function wire(box, entries) {
    var byId = {}; entries.forEach(function (e) { byId[e.id] = e; });
    function tok(id) { return tokens()[id]; }
    box.querySelectorAll("[data-edit]").forEach(function (b) { b.onclick = function () { form(byId[b.getAttribute("data-edit")]); }; });
    box.querySelectorAll("[data-renew]").forEach(function (b) {
      b.onclick = function () { var id = b.getAttribute("data-renew"); B.api("/api/skills/edit", { body: { id: id, token: tok(id) } }).then(function (r) { if (!r.ok) alert(r.error || "Failed"); load(); }); };
    });
    box.querySelectorAll("[data-del]").forEach(function (b) {
      b.onclick = function () {
        if (!confirm("Remove your entry?")) return;
        var id = b.getAttribute("data-del");
        B.api("/api/skills/remove", { body: { id: id, token: tok(id) } }).then(function (r) {
          if (r.ok || r._status === 404) { var t = tokens(); delete t[id]; saveTokens(t); } else alert(r.error || "Failed");
          load();
        });
      };
    });
    box.querySelectorAll("[data-mod]").forEach(function (b) {
      b.onclick = function () {
        var op = b.getAttribute("data-mod");
        if (op === "remove" && !confirm("Remove this entry for everyone?")) return;
        B.api("/api/skills/moderate", { body: { id: b.getAttribute("data-id"), op: op } }).then(function (r) { if (!r.ok) alert(r.error || "Failed"); load(); });
      };
    });
    box.querySelectorAll("[data-flag]").forEach(function (b) {
      b.onclick = function () {
        if (!confirm("Report this entry to the Badger’s owner as wrong or abusive?")) return;
        B.api("/api/skills/flag", { body: { id: b.getAttribute("data-flag") } }).then(function (r) { b.textContent = r.ok ? "Reported" : (r.error || "Failed"); b.disabled = true; });
      };
    });
  }

  function form(e) {
    var edit = !!e; e = e || { kind: view.kind === "need" ? "need" : "offer", category: view.cat || "other" };
    var box = $("skForm");
    box.innerHTML = '<form id="skF" class="card"><h2>' + (edit ? "Edit your entry" : "Add an offer or need") + "</h2>" +
      '<label style="font-weight:400;display:inline-block;margin-right:14px"><input type="radio" name="skk" value="offer"' + (e.kind !== "need" ? " checked" : "") + "> I can offer</label>" +
      '<label style="font-weight:400;display:inline-block"><input type="radio" name="skk" value="need"' + (e.kind === "need" ? " checked" : "") + "> I need</label>" +
      '<label for="skc">Category</label><select id="skc">' + ORDER.map(function (c) { return '<option value="' + c + '"' + (e.category === c ? " selected" : "") + ">" + esc(CATS[c]) + "</option>"; }).join("") + "</select>" +
      '<label for="skt">What</label><input type="text" id="skt" maxlength="80" required placeholder="e.g. Registered nurse · Generator (3 kW) · Need insulin storage" value="' + esc(e.title || "") + '">' +
      '<label for="skn">Name (optional)</label><input type="text" id="skn" maxlength="40" autocomplete="given-name" placeholder="A first name is enough" value="' + esc(e.name || "") + '">' +
      '<label for="sko">Where / how to find you (optional)</label><input type="text" id="sko" maxlength="200" placeholder="e.g. blue house at Oak & 3rd, or: ask at the church" value="' + esc(e.contact || "") + '">' +
      '<label for="ska">When (optional)</label><input type="text" id="ska" maxlength="120" placeholder="e.g. evenings, weekends, any time" value="' + esc(e.availability || "") + '">' +
      '<label for="skd">Details (optional)</label><textarea id="skd" maxlength="500" rows="3">' + esc(e.details || "") + "</textarea>" +
      '<p class="small muted">Visible to anyone on the Badger’s Wi-Fi. This device keeps a key so you can edit or remove it later.</p>' +
      '<div class="btns"><button class="btn">' + (edit ? "Save" : "Add") + '</button><button type="button" class="btn ghost" id="skX">Cancel</button><span id="skS" class="small muted"></span></div></form>';
    wide($("skF"));
    $("skt").focus();
    $("skX").onclick = function () { box.innerHTML = ""; };
    $("skF").onsubmit = function (ev) {
      ev.preventDefault();
      var k = box.querySelector("input[name=skk]:checked");
      var body = { kind: k ? k.value : "offer", category: $("skc").value, title: $("skt").value, name: $("skn").value,
        contact: $("sko").value, availability: $("ska").value, details: $("skd").value };
      if (edit) { body.id = e.id; body.token = tokens()[e.id]; }
      $("skS").textContent = "Saving…";
      B.api(edit ? "/api/skills/edit" : "/api/skills/add", { body: body }).then(function (r) {
        if (!r.ok) { $("skS").textContent = r.error || "Couldn’t save."; return; }
        if (!edit && r.token) { var t = tokens(); t[r.id] = r.token; saveTokens(t); }
        box.innerHTML = '<p class="small">Saved ✓</p>';
        load();
      });
    };
  }
})();
