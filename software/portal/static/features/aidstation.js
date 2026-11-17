/* Aid station: public status line for everyone; visit log for aid volunteers and the admin.
   Volunteers' device key lives in localStorage ("aidKey") and is sent as X-Aid-Token. ES5 only. */
(function () {
  "use strict";
  var B = window.Badger;
  if (!B) return;
  var esc = B.esc;
  var HELP = [["water", "💧 Water"], ["food", "🥫 Food"], ["first_aid", "⛑️ First aid"], ["shelter", "🏠 Shelter"], ["info", "ℹ️ Info"], ["other", "➕ Other"]];
  var ITEMS = [["water", "💧 Water"], ["food", "🥫 Food"], ["first_aid", "⛑️ First aid"], ["shelter", "🏠 Shelter"], ["charging", "🔌 Phone charging"], ["info", "ℹ️ Information"]];
  function label(list, id) { for (var i = 0; i < list.length; i++) if (list[i][0] === id) return list[i][1]; return id; }

  function ls(k, v) {
    try { if (v === undefined) return localStorage.getItem(k) || ""; if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) {}
    return "";
  }
  function adminToken() { try { return sessionStorage.getItem("adminToken") || ""; } catch (e) { return ""; } }
  function headers(json) {
    var h = {}; if (json) h["Content-Type"] = "application/json";
    var k = ls("aidKey"); if (k) h["X-Aid-Token"] = k;
    var a = adminToken(); if (a) h["X-Admin-Token"] = a;
    return h;
  }
  function api(path, body) {
    return fetch(path, { credentials: "same-origin", method: body ? "POST" : "GET", headers: headers(!!body), body: body ? JSON.stringify(body) : undefined })
      .then(function (r) { return r.json().then(function (j) { j._status = r.status; return j; }, function () { return { _status: r.status, error: "HTTP " + r.status }; }); })
      .catch(function () { return { _status: 0, error: "No answer from the Badger. Are you on its Wi-Fi?" }; });
  }
  function $(id) { return document.getElementById(id); }
  // full-width text fields in a form card (style.css only stretches textareas)
  function wide(root) { root.querySelectorAll("input[type=text], select").forEach(function (i) { i.style.width = "100%"; i.style.boxSizing = "border-box"; }); }
  function when(t) { return esc(B.tfmt(t, { date: true, short: true })); }
  function midnight() { var d = new Date(); d.setHours(0, 0, 0, 0); return Math.floor(d.getTime() / 1000); }

  function statusLine(st) {
    if (!st || !st.set) return '<p class="muted">The aid station hasn’t posted its status yet.</p>';
    var items = (st.items || []).map(function (i) { return esc(label(ITEMS, i)); }).join(" · ");
    return '<p style="font-size:1.15rem;margin:0"><b>' + (st.open ? "🟢 Aid station open" : "🔴 Aid station closed") + "</b>" +
      (st.open && items ? " · " + items : "") + "</p>" + (st.text ? "<p>" + esc(st.text) + "</p>" : "") +
      '<p class="small muted">Updated ' + when(st.updated) + (st.stale ? " — this may be out of date" : "") + "</p>";
  }

  var NAV = [["", "New visit"], ["today", "Today"], ["followups", "Follow-ups"], ["search", "Search"], ["status", "Station status"]];
  function nav(sub, me) {
    var list = NAV.slice(); if (me.admin) list.push(["admin", "Volunteers & privacy"]);
    return '<div class="btns" role="tablist" style="margin-bottom:10px">' + list.map(function (n) {
      return '<a class="btn' + (n[0] === sub ? "" : " ghost") + '" href="#/aid' + (n[0] ? "/" + n[0] : "") + '">' + esc(n[1]) + "</a>";
    }).join("") + '<button class="btn ghost" id="aidCsv">⬇ CSV</button></div>';
  }

  function page(ctx) {
    var sub = (ctx.match && ctx.match[1]) || "";
    B.h("<h1>⛑️ Aid station</h1><p class=\"muted\">Loading…</p>");
    api("/api/aidstation/me").then(function (me) {
      if (me._status !== 200) return B.h("<h1>⛑️ Aid station</h1><p class=\"muted\">" + esc(me.error || "Couldn’t load.") + "</p>");
      if (me.token_invalid) ls("aidKey", null);
      var head = "<h1>⛑️ Aid station</h1><div class=\"card\">" + statusLine(me.status) + "</div>";
      if (!me.staff && !me.admin) return publicView(head, me);
      B.h(head + '<p class="small muted section">' + (me.staff ? "Signed in as aid volunteer <b>" + esc(me.staff.name) + "</b>. " : "Signed in as admin. ") +
        "These records are private: only aid volunteers and the admin can see them." +
        (me.staff ? ' <a href="#" id="aidLeave">Sign this device out</a>' : "") + "</p>" + nav(sub, me) + '<div id="aidBody"></div>');
      $("aidCsv").onclick = exportCsv;
      if ($("aidLeave")) $("aidLeave").onclick = function (e) {
        e.preventDefault();
        if (!confirm("Sign this device out of the aid station? You’ll need a new code from the Badger’s owner to sign in again.")) return;
        api("/api/aidstation/leave", {}).then(function () { ls("aidKey", null); home(); });
      };
      var body = $("aidBody");
      if (sub === "today") return todayView(body);
      if (sub === "followups") return listView(body, { followup: "open" }, "Follow-ups still open", "Nothing waiting for follow-up. 👍");
      if (sub === "search") return searchView(body);
      if (sub === "status") return statusForm(body, me.status);
      if (sub === "admin" && me.admin) return adminView(body);
      entryForm(body, null, me.staff ? me.staff.name : "");
    });
  }
  B.register("/aid/*", page);
  function home() { if (location.hash === "#/aid") page({ match: [null, ""] }); else location.hash = "#/aid"; }

  function publicView(head, me) {
    B.h(head + '<p class="small muted">The aid station keeps a private log of who came and what help they got. Only its volunteers and the Badger’s owner can see it.</p>' +
      '<details class="card section"><summary><b>Aid volunteer? Sign in here</b></summary>' +
      '<form id="aidJoin"><p class="small muted">Ask the Badger’s owner for a volunteer code. It works once, on one device, for a limited time.</p>' +
      '<label for="ajc">Volunteer code</label><input type="text" id="ajc" autocomplete="off" autocapitalize="characters" placeholder="K7QX-M2PD-9Z">' +
      '<label for="ajn">Your name (if the code doesn’t have one)</label><input type="text" id="ajn" maxlength="40" autocomplete="name">' +
      '<label for="ajd">This device</label><input type="text" id="ajd" maxlength="40" placeholder="e.g. Maria’s phone">' +
      '<div class="btns" style="margin-top:8px"><button class="btn">Sign in</button><span id="ajs" class="small muted"></span></div></form></details>');
    wide($("aidJoin"));
    $("aidJoin").onsubmit = function (e) {
      e.preventDefault();
      api("/api/aidstation/join", { code: $("ajc").value, name: $("ajn").value, device: $("ajd").value }).then(function (r) {
        if (r.ok && r.key) { ls("aidKey", r.key); ls("aidHandler", r.staff.name); home(); }
        else $("ajs").textContent = r.error || "That didn’t work.";
      });
    };
  }

  function toggles(name, list, chosen) {
    return '<div class="btns" style="margin:6px 0">' + list.map(function (t) {
      var on = chosen.indexOf(t[0]) >= 0;
      return '<button type="button" class="btn' + (on ? "" : " ghost") + '" data-' + name + '="' + esc(t[0]) + '" aria-pressed="' + on + '" style="min-height:48px">' + esc(t[1]) + "</button>";
    }).join("") + "</div>";
  }
  function wireToggles(root, name) {
    root.querySelectorAll("[data-" + name + "]").forEach(function (b) {
      b.onclick = function () { var on = b.getAttribute("aria-pressed") !== "true"; b.setAttribute("aria-pressed", String(on)); b.className = "btn" + (on ? "" : " ghost"); };
    });
  }
  function picked(root, name) {
    var out = []; root.querySelectorAll("[data-" + name + "]").forEach(function (b) { if (b.getAttribute("aria-pressed") === "true") out.push(b.getAttribute("data-" + name)); });
    return out;
  }

  // Quick entry, built for one thumb: tap the help given, type a name, save.
  function entryForm(box, e, myName, done) {
    var edit = !!e; e = e || {};
    var handler = e.handled_by || ls("aidHandler") || myName || "";
    box.innerHTML = '<form id="aidForm" class="card"><h2>' + (edit ? "Edit visit" : "New visit") + "</h2>" +
      '<label>Help given</label>' + toggles("help", HELP, e.help || []) +
      '<label for="afp">Person — name or description</label><input type="text" id="afp" maxlength="80" required autocomplete="off" placeholder="e.g. Ana Ruiz, or: man ~40, blue coat" value="' + esc(e.person || "") + '">' +
      '<label for="afc">How many people</label><input type="number" id="afc" min="1" max="99" inputmode="numeric" value="' + esc(e.count || 1) + '" style="max-width:7em">' +
      '<label for="afn">Needs</label><textarea id="afn" maxlength="300" rows="2" placeholder="What they asked for or need">' + esc(e.needs || "") + "</textarea>" +
      '<details' + (e.notes || e.followup ? " open" : "") + '><summary class="small">More: notes, follow-up, handled by</summary>' +
      '<label for="afo">Notes</label><textarea id="afo" maxlength="1000" rows="3">' + esc(e.notes || "") + "</textarea>" +
      '<label style="font-weight:400"><input type="checkbox" id="aff"' + (e.followup ? " checked" : "") + "> Follow-up needed</label>" +
      '<input type="text" id="afq" maxlength="200" placeholder="Follow-up: what and when, e.g. check dressing tomorrow 9 am" value="' + esc(e.followup_note || "") + '">' +
      '<label for="afh">Handled by</label><input type="text" id="afh" maxlength="40" value="' + esc(handler) + '"></details>' +
      '<div class="btns" style="margin-top:10px"><button class="btn" style="min-height:48px;min-width:8em">Save</button>' +
      (edit ? '<button type="button" class="btn ghost" id="afx">Cancel</button><button type="button" class="btn danger" id="afd">Delete</button>' : "") +
      '<span id="afs" class="small muted" role="status"></span></div></form>';
    var form = box.querySelector("form");
    function q(id) { return form.querySelector("#" + id); }
    wireToggles(form, "help");
    wide(form);
    if (!edit) q("afp").focus();
    if (edit) {
      q("afx").onclick = function () { if (done) done(); };
      q("afd").onclick = function () {
        if (!confirm("Delete this record for good?")) return;
        api("/api/aidstation/delete", { id: e.id }).then(function (r) { if (r.ok) { if (done) done(); } else q("afs").textContent = r.error || "Failed"; });
      };
    }
    form.onsubmit = function (ev) {
      ev.preventDefault();
      var body = { person: q("afp").value, count: parseInt(q("afc").value, 10) || 1, help: picked(form, "help"), needs: q("afn").value,
        notes: q("afo").value, followup: q("aff").checked, followup_note: q("afq").value, handled_by: q("afh").value };
      if (edit) body.id = e.id;
      ls("aidHandler", body.handled_by);
      q("afs").textContent = "Saving…";
      api("/api/aidstation/entry", body).then(function (r) {
        if (!r.ok) { q("afs").textContent = r.error || "Couldn’t save."; return; }
        if (edit) { if (done) done(); return; }
        entryForm(box, null, myName);
        var st = box.querySelector("#afs"); if (st) st.textContent = "Saved ✓ " + r.entry.person;
      });
    };
  }

  function entryCard(e) {
    var fu = e.followup ? (e.followup_done ? '<span class="pill ok">follow-up done</span>' : '<span class="pill warn">follow-up needed</span>') : "";
    return '<div class="card" data-id="' + esc(e.id) + '"><div class="row"><div style="flex:1;min-width:200px">' +
      "<h3>" + esc(e.person) + (e.count > 1 ? ' <span class="pill">' + B.num(e.count) + " people</span>" : "") + " " + fu + "</h3>" +
      '<div class="meta">' + when(e.at) + " · " + ((e.help || []).map(function (h) { return esc(label(HELP, h)); }).join(", ") || "no help type") +
      " · by " + esc(e.handled_by || e.by) + "</div>" +
      (e.needs ? '<p class="small"><b>Needs:</b> ' + esc(e.needs) + "</p>" : "") +
      (e.notes ? '<p class="small" style="white-space:pre-wrap">' + esc(e.notes) + "</p>" : "") +
      (e.followup && e.followup_note ? '<p class="small"><b>Follow-up:</b> ' + esc(e.followup_note) + "</p>" : "") +
      '</div><div class="btns">' + (e.followup ? '<button class="btn ghost" data-fu="' + esc(e.id) + '" data-done="' + (e.followup_done ? "0" : "1") + '">' +
        (e.followup_done ? "Reopen follow-up" : "✓ Follow-up done") + "</button>" : "") +
      '<button class="btn ghost" data-edit="' + esc(e.id) + '">Edit</button></div></div></div>';
  }

  function renderList(box, entries, emptyMsg, reload) {
    box.innerHTML = entries.length ? '<div class="list">' + entries.map(entryCard).join("") + "</div>" : '<p class="muted">' + esc(emptyMsg) + "</p>";
    var byId = {}; entries.forEach(function (e) { byId[e.id] = e; });
    box.querySelectorAll("[data-fu]").forEach(function (b) {
      b.onclick = function () { api("/api/aidstation/followup", { id: b.getAttribute("data-fu"), done: b.getAttribute("data-done") === "1" }).then(reload); };
    });
    box.querySelectorAll("[data-edit]").forEach(function (b) {
      b.onclick = function () {
        var card = b.closest ? b.closest(".card") : b.parentNode.parentNode.parentNode;
        entryForm(card, byId[b.getAttribute("data-edit")], "", reload);
      };
    });
  }

  function listView(box, params, title, emptyMsg) {
    box.innerHTML = "<h2>" + esc(title) + '</h2><div id="aidList" class="muted">Loading…</div>';
    function load() {
      var qs = Object.keys(params).map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(params[k]); }).join("&");
      api("/api/aidstation/entries?" + qs).then(function (r) {
        if (r._status !== 200) { $("aidList").textContent = r.error || "Couldn’t load."; return; }
        renderList($("aidList"), r.entries, emptyMsg, load);
      });
    }
    load();
  }

  function todayView(box) {
    var since = midnight();
    box.innerHTML = '<div id="aidSum" class="card">Loading…</div><div id="aidToday"></div>';
    api("/api/aidstation/summary?since=" + since).then(function (s) {
      if (s._status !== 200) { $("aidSum").textContent = s.error || "Couldn’t load."; return; }
      $("aidSum").innerHTML = "<h2>Today so far</h2><p style=\"font-size:1.2rem\"><b>" + B.num(s.visits) + "</b> visits · <b>" + B.num(s.people) + "</b> people</p>" +
        '<div class="btns">' + HELP.map(function (h) { return '<span class="pill" style="font-size:.95rem">' + esc(h[1]) + " <b>" + B.num(s.help[h[0]]) + "</b></span>"; }).join("") + "</div>" +
        '<p class="small muted">' + B.num(s.followups_open) + " follow-up(s) open · " + B.num(s.total_records) + " records kept (deleted after " + B.num(s.retention_days) + " days)</p>";
    });
    listView($("aidToday"), { since: since }, "Today’s visits", "No visits recorded today yet.");
  }

  function searchView(box) {
    box.innerHTML = '<form id="aidSearch" class="btns"><input type="text" id="aidQ" placeholder="Name, need, note…" style="flex:1;min-width:180px" maxlength="100"><button class="btn">Search</button></form><div id="aidRes" class="section"></div>';
    $("aidQ").focus();
    $("aidSearch").onsubmit = function (e) {
      e.preventDefault();
      var q = $("aidQ").value.trim(); if (!q) return;
      listView($("aidRes"), { q: q }, "Results for “" + q + "”", "Nothing matches.");
    };
  }

  function statusForm(box, st) {
    st = st && st.set ? st : { open: false, items: [], text: "" };
    box.innerHTML = '<form id="aidSt" class="card"><h2>Station status</h2><p class="small muted">Shown to everyone on the Badger’s Wi-Fi. Don’t put names or anything private here.</p>' +
      '<label style="font-weight:400"><input type="radio" name="aso" value="1"' + (st.open ? " checked" : "") + "> 🟢 Open</label>" +
      '<label style="font-weight:400"><input type="radio" name="aso" value="0"' + (st.open ? "" : " checked") + "> 🔴 Closed</label>" +
      "<label>Available now</label>" + toggles("item", ITEMS, st.items || []) +
      '<label for="ast">Short note (optional)</label><input type="text" id="ast" maxlength="100" placeholder="e.g. Church hall on Main St, until 8 pm" value="' + esc(st.text || "") + '">' +
      '<div class="btns" style="margin-top:8px"><button class="btn">Update status</button><span id="ass" class="small muted"></span></div></form>';
    var form = $("aidSt");
    wireToggles(form, "item");
    wide(form);
    form.onsubmit = function (e) {
      e.preventDefault();
      var open = form.querySelector("input[name=aso]:checked");
      api("/api/aidstation/status", { open: !!(open && open.value === "1"), items: picked(form, "item"), text: $("ast").value }).then(function (r) {
        $("ass").textContent = r.ok ? "Updated ✓" : (r.error || "Failed");
      });
    };
  }

  function exportCsv() {
    fetch("/api/aidstation/export.csv", { credentials: "same-origin", headers: headers(false) }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.blob();
    }).then(function (b) {
      var u = URL.createObjectURL(b), a = document.createElement("a");
      a.href = u; a.download = "aid-station-" + new Date().toISOString().slice(0, 10) + ".csv";
      document.body.appendChild(a); a.click();
      setTimeout(function () { URL.revokeObjectURL(u); a.parentNode.removeChild(a); }, 2000);
    }).catch(function (e) { alert("Export failed: " + e.message); });
  }

  function adminView(box) {
    box.innerHTML = '<div id="aidAdm" class="muted">Loading…</div>';
    api("/api/aidstation/admin").then(function (d) {
      if (d._status !== 200) { $("aidAdm").textContent = d.error || "Admin login required."; return; }
      var staff = d.staff.map(function (s) {
        return '<div class="msg"><span class="who">' + esc(s.name) + '</span><span class="when">' + esc(s.device) + " · last seen " + when(s.last_seen) +
          '</span> <button class="btn ghost" data-rev="' + esc(s.id) + '">Revoke</button></div>';
      }).join("") || '<p class="muted">No volunteers yet.</p>';
      $("aidAdm").innerHTML = '<div class="card"><h2>Aid volunteers</h2>' +
        '<p class="small muted">Give each volunteer a one-time code. It works once, on one device, for ' + B.num(d.code_hours) + " hours. They don’t need to be family members. Revoking stops that device at once.</p>" +
        '<form id="aidInv" class="btns"><input type="text" id="aiN" maxlength="40" placeholder="Volunteer’s name" style="flex:1;min-width:160px"><button class="btn">Create code</button></form><div id="aiOut"></div>' +
        '<h3 class="section">Signed-in devices</h3>' + staff +
        (d.codes.length ? '<p class="small muted">' + d.codes.length + " unused code(s) waiting.</p>" : "") +
        '<div class="btns section"><button class="btn danger" id="aiAll">End of shift: sign out everyone</button></div></div>' +
        '<div class="card section"><h2>Privacy & retention</h2>' +
        '<p class="small">Records hold names and health needs. They stay on the Badger only, are visible only to aid volunteers and the admin, and are <b>deleted automatically</b> after the number of days below. They are not encrypted on the Badger’s drive: anyone who takes the Badger apart could read them. Export only when you must, and delete exports when done.</p>' +
        '<form id="aidRet" class="btns"><label for="aiD" style="margin:0">Delete records after</label><input type="number" id="aiD" min="1" max="365" value="' + B.num(d.retention_days) + '" style="max-width:6em"> days <button class="btn ghost">Save</button></form>' +
        '<p class="small muted">Default ' + B.num(d.retention_default) + ' days (prepper.toml). <span id="aiRs"></span></p>' +
        '<div class="btns"><button class="btn danger" id="aiWipe">Delete all records now</button></div></div>' +
        '<div class="card section"><h2>Recent activity</h2>' + (d.audit.map(function (a) {
          return '<div class="small">' + when(a.at) + " — " + esc(a.who) + ": " + esc(a.what) + "</div>"; }).join("") || '<p class="muted small">Nothing yet.</p>') + "</div>";
      $("aidInv").onsubmit = function (e) {
        e.preventDefault();
        api("/api/aidstation/admin", { op: "invite", name: $("aiN").value }).then(function (r) {
          $("aiOut").innerHTML = r.ok ? '<div class="card" style="border-color:var(--accent)"><p>Code for <b>' + esc(r.name || "a volunteer") + '</b>:</p><p class="mono" style="font-size:1.6rem">' + esc(r.code) +
            '</p><p class="small">On their phone: join the Badger’s Wi-Fi, open Aid station → “Aid volunteer? Sign in here”, and type the code. Works once, until ' + when(r.expires) + ".</p></div>"
            : '<p class="small">' + esc(r.error || "Failed") + "</p>";
        });
      };
      box.querySelectorAll("[data-rev]").forEach(function (b) {
        b.onclick = function () { api("/api/aidstation/admin", { op: "revoke", id: b.getAttribute("data-rev") }).then(function () { adminView(box); }); };
      });
      $("aiAll").onclick = function () {
        if (!confirm("Sign out every volunteer device now? They will need new codes.")) return;
        api("/api/aidstation/admin", { op: "revoke_all" }).then(function () { adminView(box); });
      };
      $("aidRet").onsubmit = function (e) {
        e.preventDefault();
        api("/api/aidstation/admin", { op: "retention", days: parseInt($("aiD").value, 10) }).then(function (r) {
          $("aiRs").textContent = r.ok ? "Saved. " + B.num(r.deleted) + " old record(s) deleted." : (r.error || "Failed");
        });
      };
      $("aiWipe").onclick = function () {
        var t = prompt("This deletes every aid-station record for good. Type DELETE to confirm.");
        if (t !== "DELETE") return;
        api("/api/aidstation/admin", { op: "wipe", confirm: "DELETE" }).then(function (r) { alert(r.ok ? B.num(r.deleted) + " record(s) deleted." : (r.error || "Failed")); adminView(box); });
      };
    });
  }
})();
