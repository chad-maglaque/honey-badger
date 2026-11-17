/* Missing persons and lost & found. Posters keep an edit token per post in localStorage
   ("missingTokens"). Photos are re-encoded to JPEG on a canvas before upload (≤1600 px, ≤1 MB),
   which also drops EXIF/GPS. ES5 only. */
(function () {
  "use strict";
  var B = window.Badger;
  if (!B) return;
  var esc = B.esc;
  var PHOTO_LIMIT = 1000000, PHOTO_SIDE = 1600;
  var view = { kind: "person", q: "" };
  var STATUS = {
    missing: ["MISSING", "bad"], safe: ["Found safe", "ok"], closed: ["Closed", ""],
    lost: ["LOST", "warn"], found: ["FOUND", "ok"], returned: ["Returned to owner", ""]
  };

  function $(id) { return document.getElementById(id); }
  // full-width text fields in a form card (style.css only stretches textareas)
  function wide(root) { root.querySelectorAll("input[type=text], select").forEach(function (i) { i.style.width = "100%"; i.style.boxSizing = "border-box"; }); }
  function tokens() { try { return JSON.parse(localStorage.getItem("missingTokens") || "{}") || {}; } catch (e) { return {}; } }
  function saveTokens(t) { try { localStorage.setItem("missingTokens", JSON.stringify(t)); } catch (e) {} }
  function adminToken() { try { return sessionStorage.getItem("adminToken") || ""; } catch (e) { return ""; } }
  function when(t) { return esc(B.tfmt(t, { date: true, short: true })); }

  B.register("/missing", function (ctx) {
    if (ctx.qs && (ctx.qs.kind === "item" || ctx.qs.kind === "person")) view.kind = ctx.qs.kind;
    if (ctx.qs && ctx.qs.q) view.q = String(ctx.qs.q).slice(0, 100);
    B.h('<h1>🔎 Missing & found</h1>' +
      '<p class="muted">Post a missing person or a lost or found item. If someone is found safe, mark it so everyone can see.</p>' +
      '<div class="btns" role="tablist" id="mpTabs"></div>' +
      '<form id="mpFind" class="btns" style="margin-top:8px"><input type="text" id="mpQ" maxlength="100" placeholder="Search by name — partial is fine" style="flex:1;min-width:160px"><button class="btn ghost">Search</button></form>' +
      '<div class="btns section"><button class="btn" id="mpAdd"></button></div><div id="mpForm"></div>' +
      '<div id="mpAdmin"></div><div id="mpList" class="section muted">Loading…</div>');
    $("mpQ").value = view.q;
    tabs();
    $("mpFind").onsubmit = function (e) { e.preventDefault(); view.q = $("mpQ").value.trim(); load(); };
    $("mpAdd").onclick = function () { form(null); };
    load();
  });

  function tabs() {
    $("mpTabs").innerHTML = [["person", "👤 People"], ["item", "🎒 Lost & found things"]].map(function (t) {
      return '<button class="btn' + (view.kind === t[0] ? "" : " ghost") + '" data-kind="' + t[0] + '" aria-selected="' + (view.kind === t[0]) + '">' + t[1] + "</button>";
    }).join("");
    $("mpTabs").querySelectorAll("[data-kind]").forEach(function (b) {
      b.onclick = function () { view.kind = b.getAttribute("data-kind"); tabs(); $("mpForm").innerHTML = ""; load(); };
    });
    $("mpAdd").textContent = view.kind === "person" ? "➕ Report a missing person" : "➕ Lost or found something";
  }

  function load() {
    B.api("/api/missing?kind=" + view.kind + "&q=" + encodeURIComponent(view.q)).then(function (r) {
      var box = $("mpList"); if (!box) return;
      if (r._status !== 200) { box.textContent = r.error || "Couldn’t load the posts."; return; }
      var mine = tokens(), admin = B.isAdmin();
      box.className = "section";
      box.innerHTML = (r.posts.length ? '<div class="list">' + r.posts.map(function (p) { return card(p, mine[p.id], admin, r.mesh); }).join("") + "</div>"
        : '<p class="muted">' + (view.q ? "No post matches “" + esc(view.q) + "”. Try part of the name, or another spelling." : "Nothing posted here.") + "</p>") +
        '<p class="small muted">Posts marked found safe, closed or returned disappear after ' + B.num(r.keep_days) + " days.</p>";
      $("mpAdmin").innerHTML = admin && r.mesh_pending ? '<div class="warnbox">📡 ' + B.num(r.mesh_pending) + " radio announcement request(s) waiting for your approval below.</div>" : "";
      wire(box, r.posts);
    });
  }

  function card(p, tok, admin, meshOn) {
    var s = STATUS[p.status] || [p.status, ""], person = p.kind === "person";
    var border = p.status === "safe" ? "border:3px solid var(--ok)" : p.status === "missing" ? "border:2px solid var(--danger)" : "";
    var faded = p.status === "closed" || p.status === "returned" || p.hidden;
    var banner = p.status === "safe" ? '<p style="font-size:1.3rem;font-weight:700;color:var(--ok);margin:0 0 6px">✅ FOUND SAFE</p>' : "";
    var photo = p.photo ? (p.hidden ? '<img data-blob="' + esc(p.photo) + '" alt="" style="max-width:100%;max-height:320px;border-radius:8px;margin:6px 0">'
      : '<img src="' + B.href(p.photo) + '" alt="Photo of ' + esc(p.name) + '" loading="lazy" style="max-width:100%;max-height:320px;border-radius:8px;margin:6px 0">') : "";
    var canAnnounce = meshOn && (p.status === "missing" || p.status === "safe" || p.status === "lost" || p.status === "found");
    var own = "";
    if (tok) {
      own = (person && p.status === "missing" ? '<button class="btn" data-st="safe" data-id="' + esc(p.id) + '">✅ Found safe</button>' : "") +
        (!person && p.status !== "returned" ? '<button class="btn ghost" data-st="returned" data-id="' + esc(p.id) + '">Returned to owner</button>' : "") +
        (person && p.status !== "closed" ? '<button class="btn ghost" data-st="closed" data-id="' + esc(p.id) + '">Close</button>' : "") +
        '<button class="btn ghost" data-edit="' + esc(p.id) + '">Edit</button>' +
        '<label class="btn ghost" style="cursor:pointer">' + (p.photo ? "Replace photo" : "Add photo") + '<input type="file" accept="image/*" data-photo="' + esc(p.id) + '" style="display:none"></label>' +
        (p.photo ? '<button class="btn ghost" data-nophoto="' + esc(p.id) + '">Remove photo</button>' : "") +
        (canAnnounce && (p.mesh === "none" || p.mesh === "rejected") ? '<button class="btn ghost" data-ask="' + esc(p.id) + '">📡 Ask for a radio announcement</button>' : "") +
        '<button class="btn danger" data-del="' + esc(p.id) + '">Delete post</button>';
    }
    var mod = "";
    if (admin) {
      mod = '<div class="section" style="border-top:1px solid var(--line);padding-top:8px"><span class="small muted">Admin:</span> ' +
        '<button class="btn ghost" data-mod="' + (p.hidden ? "unhide" : "hide") + '" data-id="' + esc(p.id) + '">' + (p.hidden ? "Unhide" : "Hide") + "</button>" +
        '<button class="btn danger" data-mod="remove" data-id="' + esc(p.id) + '">Remove</button>' +
        (p.flags ? '<button class="btn ghost" data-mod="clear_flags" data-id="' + esc(p.id) + '">Clear ' + B.num(p.flags) + " report(s)</button>" : "") +
        (meshOn && p.mesh_text ? '<details' + (p.mesh === "requested" ? " open" : "") + '><summary class="small">📡 Radio announcement' + (p.mesh === "requested" ? " — <b>requested</b>" : p.mesh === "sent" ? " — sent " + when(p.mesh_sent_at) : "") + "</summary>" +
          '<textarea maxlength="200" rows="3" data-mtext="' + esc(p.id) + '">' + esc(p.mesh_text) + "</textarea>" +
          '<p class="small muted">Goes to every Meshtastic radio in range on the community channel. Keep it short; airtime is limited to a few announcements an hour.</p>' +
          '<div class="btns"><button class="btn" data-mod="mesh_send" data-id="' + esc(p.id) + '">Send on radio</button>' +
          (p.mesh === "requested" ? '<button class="btn ghost" data-mod="mesh_reject" data-id="' + esc(p.id) + '">Decline</button>' : "") + "</div></details>" : "") +
        "</div>";
    }
    return '<div class="card" style="' + border + (faded ? ";opacity:.65" : "") + '">' + banner +
      '<div class="meta"><span class="pill ' + s[1] + '" style="font-size:.85rem;font-weight:700">' + esc(s[0]) + "</span>" +
      (p.hidden ? ' <span class="pill bad">hidden</span>' : "") + (admin && p.flags ? ' <span class="pill bad">reported ×' + B.num(p.flags) + "</span>" : "") +
      (p.mesh === "requested" ? ' <span class="pill">📡 announcement requested</span>' : p.mesh === "sent" ? ' <span class="pill">📡 announced on radio</span>' : "") + "</div>" +
      '<h3 style="font-size:1.25rem">' + esc(p.name) + "</h3>" + photo +
      (p.description ? '<p style="white-space:pre-wrap">' + esc(p.description) + "</p>" : "") +
      (p.where || p.when ? '<p class="small"><b>' + (person ? "Last seen" : p.status === "found" ? "Found" : "Lost") + ":</b> " + esc(p.where) + (p.where && p.when ? " · " : "") + esc(p.when) + "</p>" : "") +
      (p.contact ? '<p class="small"><b>Contact:</b> ' + esc(p.contact) + "</p>" : "") +
      '<div class="meta">Posted ' + when(p.created) + (p.status_at && p.status_at !== p.created ? " · status changed " + when(p.status_at) : "") + "</div>" +
      '<div class="btns" style="margin-top:8px">' + own + (!tok && !admin ? '<button class="btn ghost small" data-flag="' + esc(p.id) + '">Report a problem</button>' : "") + "</div>" + mod +
      '<p class="small" data-msg="' + esc(p.id) + '" role="status"></p></div>';
  }

  function msg(box, id, text) { var el = box.querySelector('[data-msg="' + id + '"]'); if (el) el.textContent = text; }

  function wire(box, posts) {
    var byId = {}; posts.forEach(function (p) { byId[p.id] = p; });
    function tok(id) { return tokens()[id]; }
    function after(id) { return function (r) { if (r.ok) load(); else msg(box, id, r.error || "That didn’t work."); }; }
    box.querySelectorAll("img[data-blob]").forEach(function (img) {
      B.blobUrl(img.getAttribute("data-blob")).then(function (u) { img.src = u; }).catch(function () {});
    });
    box.querySelectorAll("[data-st]").forEach(function (b) {
      b.onclick = function () {
        var id = b.getAttribute("data-id"), st = b.getAttribute("data-st");
        if (st === "safe" && !confirm("Mark " + byId[id].name + " as FOUND SAFE? Everyone on the Badger will see it.")) return;
        B.api("/api/missing/update", { body: { id: id, token: tok(id), status: st } }).then(after(id));
      };
    });
    box.querySelectorAll("[data-edit]").forEach(function (b) { b.onclick = function () { form(byId[b.getAttribute("data-edit")]); }; });
    box.querySelectorAll("[data-ask]").forEach(function (b) {
      b.onclick = function () { var id = b.getAttribute("data-ask"); B.api("/api/missing/update", { body: { id: id, token: tok(id), mesh_request: true } }).then(after(id)); };
    });
    box.querySelectorAll("[data-nophoto]").forEach(function (b) {
      b.onclick = function () { var id = b.getAttribute("data-nophoto"); B.api("/api/missing/photo-remove", { body: { id: id, token: tok(id) } }).then(after(id)); };
    });
    box.querySelectorAll("[data-photo]").forEach(function (inp) {
      inp.onchange = function () {
        var id = inp.getAttribute("data-photo"); if (!inp.files || !inp.files[0]) return;
        msg(box, id, "Preparing photo…");
        sendPhoto(id, tok(id), inp.files[0], function (err) { if (err) msg(box, id, err); else load(); });
      };
    });
    box.querySelectorAll("[data-del]").forEach(function (b) {
      b.onclick = function () {
        var id = b.getAttribute("data-del");
        if (!confirm("Delete this post for good? If the person was found, “Found safe” lets everyone know instead.")) return;
        B.api("/api/missing/remove", { body: { id: id, token: tok(id) } }).then(function (r) {
          if (r.ok || r._status === 404) { var t = tokens(); delete t[id]; saveTokens(t); load(); } else msg(box, id, r.error || "Failed");
        });
      };
    });
    box.querySelectorAll("[data-mod]").forEach(function (b) {
      b.onclick = function () {
        var id = b.getAttribute("data-id"), op = b.getAttribute("data-mod"), body = { id: id, op: op };
        if (op === "remove" && !confirm("Remove this post for everyone?")) return;
        if (op === "mesh_send") {
          var ta = box.querySelector('[data-mtext="' + id + '"]'); body.text = ta ? ta.value : "";
          if (!confirm("Send this on the community radio channel now?\n\n" + body.text)) return;
        }
        B.api("/api/missing/moderate", { body: body }).then(after(id));
      };
    });
    box.querySelectorAll("[data-flag]").forEach(function (b) {
      b.onclick = function () {
        if (!confirm("Tell the Badger’s owner this post looks wrong or abusive?")) return;
        B.api("/api/missing/flag", { body: { id: b.getAttribute("data-flag") } }).then(function (r) { b.textContent = r.ok ? "Reported — thank you" : (r.error || "Failed"); b.disabled = true; });
      };
    });
  }

  function form(p) {
    var edit = !!p, kind = edit ? p.kind : view.kind, person = kind === "person";
    p = p || { status: person ? "missing" : "lost" };
    var box = $("mpForm");
    var statusPick = person ? "" : '<label style="font-weight:400;display:inline-block;margin-right:14px"><input type="radio" name="mps" value="lost"' + (p.status !== "found" ? " checked" : "") + "> I lost something</label>" +
      '<label style="font-weight:400;display:inline-block"><input type="radio" name="mps" value="found"' + (p.status === "found" ? " checked" : "") + "> I found something</label>";
    box.innerHTML = '<form id="mpF" class="card"><h2>' + (edit ? "Edit post" : person ? "Report a missing person" : "Lost or found something") + "</h2>" + statusPick +
      '<label for="mpn">' + (person ? "Name(s)" : "What is it") + '</label><input type="text" id="mpn" maxlength="120" required placeholder="' + (person ? "e.g. Ana Ruiz" : "e.g. Brown dog “Rex”, blue backpack") + '" value="' + esc(p.name || "") + '">' +
      '<label for="mpd">Description</label><textarea id="mpd" maxlength="1000" rows="3" placeholder="' + (person ? "Age, height, hair, clothes, medical needs…" : "Colour, size, markings…") + '">' + esc(p.description || "") + "</textarea>" +
      '<label for="mpw">' + (person ? "Last seen where" : "Where") + '</label><input type="text" id="mpw" maxlength="200" placeholder="e.g. Elm St playground" value="' + esc(p.where || "") + '">' +
      '<label for="mpt">' + (person ? "Last seen when" : "When") + '</label><input type="text" id="mpt" maxlength="80" placeholder="e.g. today 14:00" value="' + esc(p.when || "") + '">' +
      '<label for="mpc">How to reach you</label><input type="text" id="mpc" maxlength="200" placeholder="e.g. Maria at the school gym, or the check-in board" value="' + esc(p.contact || "") + '">' +
      (edit ? "" : '<label for="mpp">Photo (optional)</label><input type="file" id="mpp" accept="image/*">' +
        '<p class="small muted">The photo is shrunk on your phone first, and its hidden location data is removed.</p>' +
        '<label style="font-weight:400"><input type="checkbox" id="mpm"> Ask the Badger’s owner to announce this on the mesh radio</label>') +
      '<div class="warnbox">Everything here, including the photo and how to reach you, can be seen by <b>anyone on the Badger’s Wi-Fi</b>. This device keeps a key so you can update the post later.</div>' +
      '<div class="btns"><button class="btn">' + (edit ? "Save" : "Post") + '</button><button type="button" class="btn ghost" id="mpX">Cancel</button><span id="mpS" class="small muted" role="status"></span></div></form>';
    wide($("mpF"));
    $("mpn").focus();
    $("mpX").onclick = function () { box.innerHTML = ""; };
    $("mpF").onsubmit = function (ev) {
      ev.preventDefault();
      var st = box.querySelector("input[name=mps]:checked");
      var body = { name: $("mpn").value, description: $("mpd").value, where: $("mpw").value, when: $("mpt").value, contact: $("mpc").value };
      if (!person && st) body.status = st.value;
      if (edit) { body.id = p.id; body.token = tokens()[p.id]; } else { body.kind = kind; body.mesh_request = $("mpm").checked; }
      var file = !edit && $("mpp").files && $("mpp").files[0];
      $("mpS").textContent = "Posting…";
      B.api(edit ? "/api/missing/update" : "/api/missing/add", { body: body }).then(function (r) {
        if (!r.ok) { $("mpS").textContent = r.error || "Couldn’t post."; return; }
        if (!edit) { var t = tokens(); t[r.id] = r.token; saveTokens(t); }
        if (!file) { box.innerHTML = ""; load(); return; }
        $("mpS").textContent = "Posted. Adding the photo…";
        sendPhoto(r.id, r.token, file, function (err) {
          if (err) { $("mpS").textContent = "Posted, but the photo failed: " + err + " You can add it from the post."; load(); return; }
          box.innerHTML = ""; load();
        });
      });
    };
  }

  // ── photos: re-encode on a canvas (drops EXIF/GPS), shrink until it fits ──
  function toBlob(c, q, done) {
    if (c.toBlob) return c.toBlob(done, "image/jpeg", q);
    var d = c.toDataURL("image/jpeg", q), bin = atob(d.split(",")[1]), arr = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    done(new Blob([arr], { type: "image/jpeg" }));
  }
  function reencode(file, cb) {
    var url = URL.createObjectURL(file), img = new Image();
    img.onload = function () {
      var w = img.naturalWidth || img.width, h = img.naturalHeight || img.height, qs = [0.85, 0.75, 0.65, 0.55, 0.45];
      if (!w || !h) { URL.revokeObjectURL(url); return cb("That photo couldn’t be read."); }
      function attempt(scale, qi) {
        var c = document.createElement("canvas");
        c.width = Math.max(1, Math.round(w * scale)); c.height = Math.max(1, Math.round(h * scale));
        var g = c.getContext("2d"); g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height); g.drawImage(img, 0, 0, c.width, c.height);
        toBlob(c, qs[qi], function (b) {
          if (b && b.size <= PHOTO_LIMIT) { URL.revokeObjectURL(url); return cb(null, b); }
          if (qi + 1 < qs.length) return attempt(scale, qi + 1);
          if (scale > 0.15) return attempt(scale * 0.7, 0);
          URL.revokeObjectURL(url); cb("Couldn’t make the photo small enough.");
        });
      }
      attempt(Math.min(1, PHOTO_SIDE / Math.max(w, h)), 0);
    };
    img.onerror = function () { URL.revokeObjectURL(url); cb("That file isn’t a photo this browser can open."); };
    img.src = url;
  }
  function sendPhoto(id, token, file, cb) {
    reencode(file, function (err, blob) {
      if (err) return cb(err);
      var h = { "Content-Type": "image/jpeg", "X-Badger-Upload": "1" };
      if (token) h["X-Edit-Token"] = token;
      var a = adminToken(); if (a) h["X-Admin-Token"] = a;
      fetch("/api/missing/photo/" + encodeURIComponent(id), { credentials: "same-origin", method: "POST", headers: h, body: blob })
        .then(function (r) { return r.json(); })
        .then(function (j) { cb(j.ok ? null : (j.error || "Upload failed.")); })
        .catch(function () { cb("Upload failed — are you still on the Badger’s Wi-Fi?"); });
    });
  }
})();
