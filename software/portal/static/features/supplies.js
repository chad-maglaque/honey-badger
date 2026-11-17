/* Supplies & rationing tracker — page script (ES5, no build step). Every server string goes through Badger.esc. */
(function () {
  "use strict";
  var B = window.Badger;
  var ui = { q: "", cat: "", kcal: null, water: null, more: false };
  var PRESETS = [
    { label: "💧 Water jug (1 gal)", name: "Water jug", category: "water", unit: "gal", litres: 3.785 },
    { label: "💧 Water bottles (0.5 L)", name: "Water bottles", category: "water", unit: "bottles", litres: 0.5 },
    { label: "🥫 Canned food", name: "Canned food", category: "food", unit: "cans", kcal: 400 },
    { label: "🍚 Rice (kg)", name: "Rice", category: "food", unit: "kg", kcal: 3600 },
    { label: "🫘 Dried beans (kg)", name: "Dried beans", category: "food", unit: "kg", kcal: 3400 },
    { label: "🔋 AA batteries", name: "AA batteries", category: "batteries", unit: "pcs" },
    { label: "🔥 Propane (1 lb)", name: "Propane cylinder 1 lb", category: "fuel", unit: "cylinders", hours: 2 },
    { label: "💊 Pain reliever", name: "Ibuprofen 200 mg", category: "medical", unit: "tablets" }
  ];

  function el(id) { return document.getElementById(id); }
  function n1(x) { x = B.num(x); return (Math.round(x * 10) / 10).toString(); }
  function msg(id, text, bad) { var e = el(id); if (e) { e.textContent = text || ""; e.style.color = bad ? "var(--danger)" : ""; } }
  function qty(it) { return n1(it.qty) + (it.unit ? " " + it.unit : ""); }
  function daysText(d) { return d === null || d === undefined ? "—" : (d >= 365 ? n1(d / 365) + " years" : n1(d) + " days"); }
  function levelPill(level) {
    return level === "critical" ? '<span class="pill bad">critical</span>' : level === "low" ? '<span class="pill warn">low</span>' : level === "ok" ? '<span class="pill ok">ok</span>' : "";
  }
  function flagPills(it) {
    return it.flags.map(function (f) {
      if (f === "expired") return '<span class="pill bad">expired ' + B.esc(it.expiry) + "</span>";
      if (f === "expiring") return '<span class="pill warn">expires in ' + B.num(it.days_left) + " d</span>";
      if (f === "out") return '<span class="pill bad">none left</span>';
      if (f === "low") return '<span class="pill warn">low (want ' + B.esc(n1(it.min)) + ")</span>";
      return "";
    }).join(" ");
  }

  function render() {
    if (!B.isMember()) return B.needMember("Supplies");
    B.api("/api/supplies").then(function (s) {
      if (s._status === 401) return B.needMember("Supplies");
      if (s._status !== 200) return B.h('<h1>🥫 Supplies</h1><p class="warnbox">' + B.esc(s.error || "Not available right now.") + "</p>");
      show(s);
    });
  }

  function tile(icon, title, big, sub, level) {
    return '<div class="tile"><b>' + icon + " " + B.esc(title) + "</b><div style=\"font-size:1.5rem;font-weight:700\">" + B.esc(big) + "</div>" +
      '<div class="small muted">' + sub + "</div>" + levelPill(level) + "</div>";
  }

  function show(s) {
    var hh = s.household, w = s.water, f = s.food, cats = s.categories;
    var catOpts = cats.map(function (c) { return '<option value="' + B.esc(c.id) + '">' + B.esc(c.label) + "</option>"; }).join("");
    var label = {}; cats.forEach(function (c) { label[c.id] = c.label; });
    var has = {}; s.items.forEach(function (it) { has[it.category] = true; });
    var hoursTile = function (icon, title, x, what, cat) {
      if (!has[cat]) return tile(icon, title, "—", "none listed", null);
      return tile(icon, title, x.days !== null ? daysText(x.days) : n1(x.hours) + " h", x.days !== null ? n1(x.hours) + " hours" : what, x.level);
    };
    var people = B.num(hh.people) + " " + (B.num(hh.people) === 1 ? "person" : "people") + (B.num(hh.pets) ? " + " + B.num(hh.pets) + " pet" + (B.num(hh.pets) === 1 ? "" : "s") : "");
    var tiles = tile("💧", "Water", daysText(w.days), B.esc(n1(w.litres)) + " L · needs " + B.esc(n1(w.per_day)) + " L/day" + (w.uncounted ? " · " + B.num(w.uncounted) + " item(s) not counted (set litres)" : ""), w.level) +
      tile("🍚", "Food", daysText(f.days), B.esc(Math.round(B.num(f.kcal)).toLocaleString()) + " kcal · needs " + B.esc(Math.round(B.num(f.per_day)).toLocaleString()) + "/day" + (f.uncounted ? " · " + B.num(f.uncounted) + " item(s) not counted (set kcal)" : ""), f.level) +
      hoursTile("🔥", "Fuel", s.fuel, "hours of burn (set hours per unit)", "fuel") +
      hoursTile("🔋", "Batteries", s.batteries, "hours of use (set hours per unit)", "batteries");
    var c = s.counts;
    var attention = s.items.filter(function (it) { return it.flags.length; });
    var L = !!s.locked;          // sealed records locked: only what was added since it locked (write-only)
    if (L) attention = [];

    B.h('<h1>🥫 Supplies</h1><p class="muted small">For ' + B.esc(people) + ' · <a href="#" id="shh">change</a>. Days count only items that haven’t expired.</p>' +
      (L ? '<p class="warnbox">🔒 ' + B.esc(s.message) + (s.pending ? " (" + B.num(s.pending) + " change(s) waiting)" : "") + "</p>" : "") +
      (!hh.set && !L ? '<p class="warnbox">How many people (and pets) does this cover? <a href="#" id="shh2">Set the household</a> so the days are right.</p>' : "") +
      (L ? "" : '<div class="grid">' + tiles + "</div>") +
      '<div id="shhbox"></div>' +
      (attention.length ? '<div class="card section"><h2>Needs attention</h2><p class="small">' +
        (c.expired ? '<span class="pill bad">' + c.expired + " expired</span> " : "") + (c.expiring ? '<span class="pill warn">' + c.expiring + " expiring within " + B.num(s.settings.expiring_days) + " days</span> " : "") +
        (c.low + c.out ? '<span class="pill warn">' + (c.low + c.out) + " low or out</span>" : "") + "</p>" +
        attention.slice(0, 30).map(function (it) { return "<div>" + B.esc(it.name) + " — " + B.esc(qty(it)) + (it.location ? ' <span class="muted small">(' + B.esc(it.location) + ")</span>" : "") + " " + flagPills(it) + "</div>"; }).join("") + "</div>" : "") +

      '<form id="sadd" class="card section"><h2>Quick add</h2><div class="btns" id="spre">' +
      PRESETS.map(function (p, i) { return '<button type="button" class="btn ghost" data-pre="' + i + '">' + B.esc(p.label) + "</button>"; }).join("") + "</div>" +
      '<div class="row" style="margin-top:8px"><div style="flex:2;min-width:150px"><label for="sn">Item</label><input type="text" id="sn" maxlength="60" placeholder="e.g. Canned beans"></div>' +
      '<div style="flex:1;min-width:120px"><label for="sc">Category</label><select id="sc">' + catOpts + "</select></div></div>" +
      '<div class="row"><div style="flex:1;min-width:90px"><label for="sq">How many</label><input type="number" id="sq" min="0" step="any" inputmode="decimal" value="1"></div>' +
      '<div style="flex:1;min-width:90px"><label for="su">Unit</label><input type="text" id="su" maxlength="16" placeholder="cans, L, kg"></div></div>' +
      '<details id="smore"' + (ui.more ? " open" : "") + '><summary class="small">More details (where, expiry, minimum, calories…)</summary>' +
      '<div class="row"><div style="flex:1;min-width:140px"><label for="sl">Where it’s kept</label><input type="text" id="sl" maxlength="60" placeholder="e.g. basement shelf"></div>' +
      '<div style="flex:1;min-width:140px"><label for="se">Expires</label><input type="date" id="se"></div></div>' +
      '<div class="row"><div style="flex:1;min-width:100px"><label for="sm">Keep at least</label><input type="number" id="sm" min="0" step="any" inputmode="decimal"></div>' +
      '<div style="flex:1;min-width:100px"><label for="sk">kcal per unit <span class="muted small">(food)</span></label><input type="number" id="sk" min="0" step="any" inputmode="decimal"></div>' +
      '<div style="flex:1;min-width:100px"><label for="sw">Litres per unit <span class="muted small">(water)</span></label><input type="number" id="sw" min="0" step="any" inputmode="decimal"></div>' +
      '<div style="flex:1;min-width:100px"><label for="sh">Hours per unit <span class="muted small">(fuel, batteries)</span></label><input type="number" id="sh" min="0" step="any" inputmode="decimal"></div></div>' +
      '<label for="sno">Note</label><input type="text" id="sno" maxlength="200"></details>' +
      '<div class="btns" style="margin-top:8px"><button class="btn">Add</button><span id="sam" class="small muted"></span></div></form>' +

      '<div class="card"><div class="row"><input type="text" id="sfq" placeholder="Find an item" style="flex:1;min-width:150px" value="' + B.esc(ui.q) + '">' +
      '<select id="sfc" style="max-width:200px"><option value="">All categories</option>' + catOpts + "</select></div></div>" +
      '<div id="slist"></div>' +

      (L ? "" : '<details class="card section" id="sration"><summary><b>⚖️ Rationing</b> <span class="small muted">how long it lasts on less</span></summary><div id="srbox"></div></details>' +
      '<details class="card"><summary><b>📒 Recent changes</b></summary><div class="small">' + (s.log.map(function (l) {
        return "<div>" + B.esc(B.tfmt(l.at * 1000, { date: true, short: true })) + " — <b>" + B.esc(l.who) + "</b> " + B.esc(l.what) +
          (l.name ? " " + B.esc(l.name) : "") + (l.delta !== undefined ? " (" + (l.delta > 0 ? "+" : "") + B.esc(n1(l.delta)) + (l.unit ? " " + B.esc(l.unit) : "") + ")" : "") + "</div>";
      }).join("") || '<span class="muted">Nothing yet.</span>') + "</div></details>" +
      '<details class="card"><summary><b>📄 Spreadsheet (CSV)</b></summary>' +
      '<p class="small">Download the list for a spreadsheet, or add many items at once from one. Columns: name, category, quantity, unit, location, expiry (YYYY-MM-DD), minimum, kcal_per_unit, litres_per_unit, hours_per_unit, note.</p>' +
      '<div class="btns"><button class="btn ghost" id="sexp" type="button">Download CSV</button></div>' +
      '<label for="simp">Import a CSV</label><input type="file" id="simp" accept=".csv,text/csv">' +
      '<label style="font-weight:400"><input type="checkbox" id="srep"> Replace the whole list (otherwise matching items are updated and new ones added)</label>' +
      '<div class="btns"><button class="btn ghost" id="simpb" type="button">Import</button><span id="sim" class="small muted"></span></div></details>'));

    el("sfc").value = ui.cat;
    list(s, label);
    if (!s.locked) ration(s);
    wire(s, catOpts);
  }

  function list(s, label) {
    var q = ui.q.toLowerCase(), groups = {}, order = [];
    s.items.forEach(function (it) {
      if (ui.cat && it.category !== ui.cat) return;
      if (q && (it.name + " " + it.location + " " + it.note).toLowerCase().indexOf(q) < 0) return;
      if (!groups[it.category]) { groups[it.category] = []; order.push(it.category); }
      groups[it.category].push(it);
    });
    var html = order.map(function (cat) {
      return '<h2 class="section">' + B.esc(label[cat] || cat) + '</h2><div class="list">' + groups[cat].map(function (it) {
        var extra = [];
        if (it.location) extra.push(B.esc(it.location));
        if (it.expiry && it.flags.indexOf("expired") < 0 && it.flags.indexOf("expiring") < 0) extra.push("expires " + B.esc(it.expiry));
        if (it.category === "food" && it.kcal) extra.push(B.esc(Math.round(it.kcal * it.qty).toLocaleString()) + " kcal");
        if (it.category === "food" && !it.kcal) extra.push("<i>add kcal per unit to count it</i>");
        if (it.note) extra.push(B.esc(it.note));
        return '<div class="card"><div class="row"><div style="flex:1;min-width:170px"><b>' + B.esc(it.name) + "</b> " + flagPills(it) +
          '<div class="meta">' + extra.join(" · ") + "</div></div>" +
          '<div class="btns" style="align-items:center"><button class="btn ghost" data-act="minus" data-id="' + B.esc(it.id) + '" aria-label="One less">−</button>' +
          '<b style="min-width:4.5em;text-align:center">' + B.esc(qty(it)) + "</b>" +
          '<button class="btn ghost" data-act="plus" data-id="' + B.esc(it.id) + '" aria-label="One more">+</button>' +
          '<button class="btn ghost" data-act="use" data-id="' + B.esc(it.id) + '">Use…</button>' +
          '<button class="btn ghost" data-act="edit" data-id="' + B.esc(it.id) + '">Edit</button></div></div><div id="ed-' + B.esc(it.id) + '"></div></div>';
      }).join("") + "</div>";
    }).join("");
    el("slist").innerHTML = html || '<p class="muted">' + (s.items.length ? "Nothing matches." : "Nothing listed yet. Start with water and food: tap a quick-add button above.") + "</p>";
    el("slist").onclick = function (e) {
      var b = e.target.closest ? e.target.closest("[data-act]") : null;
      if (!b) return;
      var id = b.getAttribute("data-id"), it = null;
      for (var i = 0; i < s.items.length; i++) if (s.items[i].id === id) it = s.items[i];
      if (it) act(b.getAttribute("data-act"), it, s);
    };
  }

  function post(path, body, msgId) {
    return B.api(path, { body: body }).then(function (r) {
      if (r._status === 401) { render(); return null; }
      if (!r.ok) { if (msgId) msg(msgId, r.error || "That didn’t work.", true); else alert(r.error || "That didn’t work."); return null; }
      if (r.state) { var y = window.pageYOffset; show(r.state); window.scrollTo(0, y); }   // stay where the tap was
      return r;
    });
  }

  function act(what, it, s) {
    if (what === "minus") return post("/api/supplies/adjust", { id: it.id, delta: 1, use: true });
    if (what === "plus") return post("/api/supplies/adjust", { id: it.id, delta: 1 });
    if (what === "use") {
      var a = prompt("How many " + (it.unit || "units") + " of “" + it.name + "” were used?", "1");
      if (a === null) return;
      var v = parseFloat(String(a).replace(",", "."));
      if (!(v > 0)) return alert("Enter a number above zero.");
      return post("/api/supplies/adjust", { id: it.id, delta: v, use: true });
    }
    if (what === "edit") {
      var box = el("ed-" + it.id);
      if (box.innerHTML) { box.innerHTML = ""; return; }
      var catOpts = s.categories.map(function (c) { return '<option value="' + B.esc(c.id) + '"' + (c.id === it.category ? " selected" : "") + ">" + B.esc(c.label) + "</option>"; }).join("");
      var f = function (id, lab, val, type) { return '<div style="flex:1;min-width:110px"><label for="' + id + '">' + lab + '</label><input type="' + (type || "text") + '" id="' + id + '"' + (type === "number" ? ' step="any" min="0" inputmode="decimal"' : "") + ' value="' + B.esc(val === undefined || val === null ? "" : val) + '"></div>'; };
      box.innerHTML = '<form class="section" id="ef"><div class="row">' + f("en", "Name", it.name) + '<div style="flex:1;min-width:110px"><label for="ec">Category</label><select id="ec">' + catOpts + "</select></div></div>" +
        '<div class="row">' + f("eq", "Quantity", it.qty, "number") + f("eu", "Unit", it.unit) + f("em", "Keep at least", it.min || "", "number") + "</div>" +
        '<div class="row">' + f("el", "Where", it.location) + f("ee", "Expires", it.expiry, "date") + "</div>" +
        '<div class="row">' + f("ek", "kcal / unit", it.kcal || "", "number") + f("ew", "Litres / unit", it.litres || "", "number") + f("eh", "Hours / unit", it.hours || "", "number") + "</div>" +
        f("eno", "Note", it.note) +
        '<div class="btns" style="margin-top:8px"><button class="btn">Save</button><button class="btn danger" type="button" id="edel">Delete</button><span id="eem" class="small muted"></span></div></form>';
      el("ef").onsubmit = function (e) {
        e.preventDefault();
        post("/api/supplies/item", { id: it.id, name: el("en").value, category: el("ec").value, qty: el("eq").value || "0", unit: el("eu").value,
          min: el("em").value || "0", location: el("el").value, expiry: el("ee").value, kcal: el("ek").value || "0",
          litres: el("ew").value || "0", hours: el("eh").value || "0", note: el("eno").value }, "eem");
      };
      el("edel").onclick = function () {
        if (confirm("Delete “" + it.name + "” from the list?")) post("/api/supplies/delete", { id: it.id }, "eem");
      };
    }
  }

  function ration(s) {
    var hh = s.household, kcal = ui.kcal || hh.kcal, water = ui.water || hh.water_l;
    var people = B.num(hh.people), pets = B.num(hh.pets);
    function d(total, per, extra) { var x = per * people + extra; return x > 0 ? daysText(total / x) : "—"; }
    var rows = [2000, 1500, 1200, 1000, 800].map(function (k) {
      return "<tr><td>" + k + " kcal</td><td>" + d(s.food.kcal, k, pets * hh.pet_kcal) + "</td></tr>";
    }).join("");
    var wrows = [3.8, 2, 1].map(function (l) { return "<tr><td>" + l + " L</td><td>" + d(s.water.litres, l, pets * hh.pet_water_l) + "</td></tr>"; }).join("");
    el("srbox").innerHTML = '<p class="small">Per person per day. Adults need about 2000 kcal; below ~1200 for long, people weaken. Under 2 L of drinking water a day is only for a short emergency; heat, nursing and illness need more.</p>' +
      '<div class="row"><div style="flex:1;min-width:130px"><label for="srk">kcal / person / day</label><input type="number" id="srk" min="300" step="50" inputmode="numeric" value="' + B.esc(kcal) + '"></div>' +
      '<div style="flex:1;min-width:130px"><label for="srw">water L / person / day</label><input type="number" id="srw" min="0.5" step="0.1" inputmode="decimal" value="' + B.esc(water) + '"></div></div>' +
      '<p><b>At ' + B.esc(kcal) + " kcal you have " + B.esc(d(s.food.kcal, B.num(kcal), pets * hh.pet_kcal)) + " of food; at " + B.esc(water) + " L you have " + B.esc(d(s.water.litres, B.num(water), pets * hh.pet_water_l)) + " of water.</b></p>" +
      '<div class="row"><table class="files" style="flex:1"><tr><th>Food</th><th>lasts</th></tr>' + rows + '</table><table class="files" style="flex:1"><tr><th>Water</th><th>lasts</th></tr>' + wrows + "</table></div>";
    el("srk").onchange = function () { ui.kcal = B.num(el("srk").value) || null; ration(s); };
    el("srw").onchange = function () { ui.water = B.num(el("srw").value) || null; ration(s); };
  }

  function householdForm(s) {
    var hh = s.household, box = el("shhbox");
    if (box.innerHTML) { box.innerHTML = ""; return; }
    function f(id, lab, val, step) { return '<div style="flex:1;min-width:120px"><label for="' + id + '">' + lab + '</label><input type="number" id="' + id + '" min="0" step="' + (step || "any") + '" inputmode="decimal" value="' + B.esc(val) + '"></div>'; }
    box.innerHTML = '<form id="hf" class="card"><h2>Household</h2><div class="row">' + f("hp", "People", hh.people, 1) + f("hpe", "Pets", hh.pets, 1) + "</div>" +
      '<div class="row">' + f("hk", "kcal per person per day", hh.kcal, 50) + f("hw", "Water (L) per person per day", hh.water_l, 0.1) + "</div>" +
      '<div class="row">' + f("hpk", "kcal per pet per day <span class=\"muted small\">(0 if pet food is separate)</span>", hh.pet_kcal, 50) + f("hpw", "Water (L) per pet per day", hh.pet_water_l, 0.1) + "</div>" +
      '<div class="row">' + f("hf1", "Fuel hours used per day <span class=\"muted small\">(0 = just show hours)</span>", hh.fuel_h_day, 0.5) + f("hb", "Battery hours used per day", hh.battery_h_day, 0.5) + "</div>" +
      '<div class="btns" style="margin-top:8px"><button class="btn">Save</button><span id="hm" class="small muted"></span></div></form>';
    el("hf").onsubmit = function (e) {
      e.preventDefault();
      post("/api/supplies/household", { people: el("hp").value, pets: el("hpe").value, kcal: el("hk").value, water_l: el("hw").value,
        pet_kcal: el("hpk").value, pet_water_l: el("hpw").value, fuel_h_day: el("hf1").value, battery_h_day: el("hb").value }, "hm");
    };
  }

  function wire(s, catOpts) {
    el("shh").onclick = function (e) { e.preventDefault(); householdForm(s); };
    if (el("shh2")) el("shh2").onclick = function (e) { e.preventDefault(); householdForm(s); };
    el("smore").addEventListener("toggle", function () { ui.more = el("smore").open; });
    el("spre").onclick = function (e) {
      var b = e.target.closest ? e.target.closest("[data-pre]") : null;
      if (!b) return;
      var p = PRESETS[+b.getAttribute("data-pre")];
      el("sn").value = p.name; el("sc").value = p.category; el("su").value = p.unit;
      el("sk").value = p.kcal || ""; el("sw").value = p.litres || ""; el("sh").value = p.hours || "";
      el("sq").focus(); el("sq").select();
    };
    el("sadd").onsubmit = function (e) {
      e.preventDefault();
      if (!el("sn").value.trim()) return msg("sam", "Give the item a name.", true);
      post("/api/supplies/item", { name: el("sn").value, category: el("sc").value, qty: el("sq").value || "0", unit: el("su").value,
        location: el("sl").value, expiry: el("se").value, min: el("sm").value || "0", kcal: el("sk").value || "0",
        litres: el("sw").value || "0", hours: el("sh").value || "0", note: el("sno").value }, "sam").then(function (r) {
        if (r) { msg("sam", "Added."); if (el("sn")) el("sn").focus(); }
      });
    };
    var t = null;
    el("sfq").oninput = function () { clearTimeout(t); t = setTimeout(function () { ui.q = el("sfq").value; list(s, labels(s)); }, 200); };
    el("sfc").onchange = function () { ui.cat = el("sfc").value; list(s, labels(s)); };
    if (s.locked) return;                          // (no CSV while sealed records are locked)
    el("sexp").onclick = function () {
      B.blobUrl("/api/supplies/export.csv").then(function (u) {
        var a = document.createElement("a"); a.href = u; a.download = "supplies-" + s.today + ".csv";
        document.body.appendChild(a); a.click();
        setTimeout(function () { URL.revokeObjectURL(u); if (a.parentNode) a.parentNode.removeChild(a); }, 60000);
      }).catch(function (err) { alert("Couldn’t download: " + err.message); });
    };
    el("simpb").onclick = function () {
      var file = el("simp").files[0];
      if (!file) return msg("sim", "Choose a CSV file first.", true);
      var replace = el("srep").checked;
      if (replace && !confirm("Replace the whole supplies list with this file? Items not in the file are removed.")) return;
      msg("sim", "Importing…");
      B.upload("/api/supplies/import?mode=" + (replace ? "replace" : "merge"), file, "text/csv").then(function (r) {
        if (!r.ok) return msg("sim", r.error || "Import failed.", true);
        show(r.state);
        msg("sim", r.added + " added, " + r.updated + " updated" + (r.errors.length ? ". Skipped: " + r.errors.join("; ") : "."), r.errors.length > 0);
      }).catch(function () { msg("sim", "Import failed.", true); });
    };
  }

  function labels(s) { var l = {}; s.categories.forEach(function (c) { l[c.id] = c.label; }); return l; }

  B.register("/supplies", render);
})();
