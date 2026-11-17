/* Family document vault — page script (ES5, no build step). Every server string goes through Badger.esc. */
(function () {
  "use strict";
  var B = window.Badger;
  var timer = null, catLabels = {}, filter = { q: "", cat: "" };

  function el(id) { return document.getElementById(id); }
  function msg(id, text, bad) { var e = el(id); if (e) { e.textContent = text || ""; e.style.color = bad ? "var(--danger)" : ""; } }
  function when(sec) { return B.tfmt(sec * 1000, { date: true, short: true }); }
  function icon(f) {
    if (f.kind === "note") return "📝";
    if (/^image\//.test(f.type || "")) return "🖼️";
    if (f.type === "application/pdf") return "📄";
    return "📎";
  }
  function onPage() { return /^#\/vault/.test(location.hash); }
  function clearTimer() { if (timer) { clearTimeout(timer); timer = null; } }

  function render() {
    clearTimer();
    if (!B.isMember()) return B.needMember("Document vault");
    B.api("/api/vault/status").then(function (s) {
      if (s._status === 401) return B.needMember("Document vault");
      if (s._status !== 200) return B.h('<h1>🔐 Document vault</h1><p class="warnbox">' + B.esc(s.error || "The vault isn’t available right now.") + "</p>");
      (s.categories || []).forEach(function (c) { catLabels[c.id] = c.label; });
      statusWarning = typeof s.warning === "string" ? s.warning : "";
      if (!s.setup) return setupView(s);
      if (!s.unlocked) return unlockView(s);
      mainView(s);
    });
  }

  var statusWarning = "";      // e.g. "you're on plain http" (status.warning), shown on every vault page
  function head(sub) {
    return '<h1>🔐 Document vault</h1>' + (statusWarning ? '<p class="warnbox small">' + B.esc(statusWarning) + "</p>" : "") +
      (sub ? '<p class="muted small">' + sub + "</p>" : "");
  }

  // ── first time ──
  function setupView(s) {
    if (s.ready === false) {
      return B.h(head() + '<p class="warnbox">The vault needs the <b>openssl</b> program, which isn’t working on this Badger. Ask whoever looks after it to re-run the installer.</p>');
    }
    B.h(head("Keep copies of IDs, passports, insurance, deeds, prescriptions, medical notes, family photos and contact lists. Everything is encrypted on the Badger’s disk: if it’s stolen, nobody can read them.") +
      '<form id="vs" class="card"><h2>Set up the family vault</h2>' +
      '<p class="small">Choose a <b>family passphrase</b> that every adult can remember, at least ' + B.num(s.min_passphrase) + ' characters. A short sentence or four random words works well (e.g. <i>purple tractor sings loudly</i>).</p>' +
      '<label for="vp1">Family passphrase</label><input type="password" id="vp1" autocomplete="new-password">' +
      '<label for="vp2">Type it again</label><input type="password" id="vp2" autocomplete="new-password">' +
      '<p class="warnbox small">Nobody — not the Badger’s owner, not the admin PIN — can open the vault without this passphrase. Next you’ll get a <b>recovery key</b> to print or write down: it’s the only way back if the passphrase is forgotten. Lose both and the documents are gone.</p>' +
      '<div class="btns"><button class="btn">Create the vault</button><span id="vsm" class="small muted"></span></div></form>');
    el("vs").onsubmit = function (e) {
      e.preventDefault();
      var a = el("vp1").value, b = el("vp2").value;
      if (a !== b) return msg("vsm", "The two passphrases don’t match.", true);
      msg("vsm", "Setting up… (a few seconds)");
      B.api("/api/vault/setup", { body: { passphrase: a } }).then(function (r) {
        if (!r.ok) return msg("vsm", r.error || "That didn’t work.", true);
        recoveryView(r.recovery_key, true);
      });
    };
  }

  function recoveryView(key, first) {
    clearTimer();
    B.h(head() + '<div class="card"><h2>Your recovery key</h2>' +
      '<p>Print this page or write the key down <b>now</b>, and keep it somewhere safe away from the Badger (with your passports, a safe, a trusted relative). It is shown only once.</p>' +
      '<p class="mono" style="font-size:1.25rem;line-height:1.7;word-break:break-word;padding:12px;border:2px dashed var(--muted);border-radius:8px">' + B.esc(key) + "</p>" +
      '<p class="small muted">If the family passphrase is forgotten, this key lets a family member set a new one. ' + (first ? "" : "Your old recovery key no longer works.") + "</p>" +
      '<label style="font-weight:400"><input type="checkbox" id="vok"> I printed it or wrote it down</label>' +
      '<div class="btns" style="margin-top:8px"><button class="btn ghost" id="vpr" type="button">Print</button><button class="btn" id="vgo" type="button" disabled>Continue</button></div></div>');
    el("vpr").onclick = function () { window.print(); };
    el("vok").onchange = function () { el("vgo").disabled = !el("vok").checked; };
    el("vgo").onclick = function () { render(); };
  }

  // ── locked ──
  function unlockView(s, note) {
    B.h(head("Family members unlock the vault with the family passphrase. It locks itself after " + B.num(s.idle_minutes) + " minutes without use.") +
      (note ? '<p class="warnbox small">' + B.esc(note) + "</p>" : "") +
      '<form id="vu" class="card"><h2>🔒 Locked</h2>' +
      '<label for="vup">Family passphrase</label><input type="password" id="vup" autocomplete="current-password">' +
      '<div class="btns" style="margin-top:8px"><button class="btn">Unlock</button><span id="vum" class="small muted">' +
      (s.wait ? "Too many wrong tries. Wait " + Math.ceil(B.num(s.wait) / 60) + " minutes." : "") + "</span></div>" +
      '<p class="small"><a href="#" id="vfg">Forgot the passphrase?</a></p></form><div id="vrw"></div>');
    el("vu").onsubmit = function (e) {
      e.preventDefault();
      msg("vum", "Unlocking…");
      B.api("/api/vault/unlock", { body: { passphrase: el("vup").value } }).then(function (r) {
        if (r.ok) return render();
        el("vup").value = "";
        msg("vum", r.error || "That didn’t work.", true);
      });
    };
    el("vfg").onclick = function (e) {
      e.preventDefault();
      el("vrw").innerHTML = '<form id="vr" class="card"><h2>Use the recovery key</h2>' +
        '<p class="small">Type the recovery key you printed when the vault was set up (52 letters and digits; dashes and spaces don’t matter), then choose a new family passphrase. Every other device gets locked.</p>' +
        '<label for="vrk">Recovery key</label><input type="text" id="vrk" autocomplete="off" autocapitalize="characters" spellcheck="false" class="mono">' +
        '<label for="vr1">New family passphrase</label><input type="password" id="vr1" autocomplete="new-password">' +
        '<label for="vr2">Type it again</label><input type="password" id="vr2" autocomplete="new-password">' +
        '<div class="btns" style="margin-top:8px"><button class="btn">Set new passphrase</button><span id="vrm" class="small muted"></span></div>' +
        '<p class="small muted">No recovery key either? Then the documents can’t be opened by anyone — that’s what keeps them safe from thieves.</p></form>';
      el("vr").onsubmit = function (ev) {
        ev.preventDefault();
        if (el("vr1").value !== el("vr2").value) return msg("vrm", "The two passphrases don’t match.", true);
        msg("vrm", "Checking…");
        B.api("/api/vault/recover", { body: { recovery: el("vrk").value, passphrase: el("vr1").value } }).then(function (r) {
          if (r.ok) return render();
          msg("vrm", r.error || "That didn’t work.", true);
        });
      };
    };
  }

  function lockedAgain() {
    B.api("/api/vault/status").then(function (s) {
      if (s._status === 200 && s.setup && !s.unlocked && onPage()) unlockView(s, "The vault locked itself. Unlock it again to continue.");
    });
  }
  function check(r) { if (r && r._status === 423) { lockedAgain(); return false; } return true; }

  // ── unlocked ──
  function mainView(s) {
    var cats = s.categories || [];
    var catOpts = cats.map(function (c) { return '<option value="' + B.esc(c.id) + '">' + B.esc(c.label) + "</option>"; }).join("");
    var pct = s.quota ? Math.min(100, Math.round(100 * B.num(s.used) / B.num(s.quota))) : 0;
    B.h(head() +
      '<div class="card"><div class="row"><div style="flex:1;min-width:180px"><b>🔓 Unlocked on this device</b>' +
      '<div class="meta">Locks after ' + B.num(s.idle_minutes) + ' minutes without use · ' + B.num(s.count) + " items · " + B.esc(B.human(B.num(s.used))) + " of " + B.esc(B.human(B.num(s.quota))) + "</div>" +
      '<div class="bar" style="margin-top:6px"><i style="width:' + pct + '%"></i></div></div>' +
      '<div class="btns"><button class="btn" id="vlock" type="button">Lock now</button></div></div></div>' +

      '<details class="card"><summary><b>➕ Add documents or photos</b></summary>' +
      '<form id="vadd"><label for="vf">Files (photos, PDFs…, up to ' + B.esc(B.human(B.num(s.max_file))) + ' each)</label><input type="file" id="vf" multiple>' +
      '<label for="vn">Name <span class="muted small">(optional; one file only)</span></label><input type="text" id="vn" maxlength="120">' +
      '<label for="vc">Category</label><select id="vc">' + catOpts + "</select>" +
      '<label for="vno">Note <span class="muted small">(optional, e.g. “expires 2031”, “policy #…”)</span></label><input type="text" id="vno" maxlength="1000">' +
      '<div class="btns" style="margin-top:8px"><button class="btn">Encrypt and save</button><span id="vam" class="small muted"></span></div></form></details>' +

      '<details class="card"><summary><b>📝 Type a note</b> <span class="small muted">(contact list, medications, allergies…)</span></summary>' +
      '<form id="vnote"><label for="vnt">Title</label><input type="text" id="vnt" maxlength="120" placeholder="e.g. Emergency contacts">' +
      '<label for="vnc">Category</label><select id="vnc">' + catOpts + "</select>" +
      '<label for="vnx">Text</label><textarea id="vnx" maxlength="20000" rows="6"></textarea>' +
      '<div class="btns" style="margin-top:8px"><button class="btn">Save note</button><span id="vnm" class="small muted"></span></div></form></details>' +

      '<div class="card"><div class="row"><input type="text" id="vq" placeholder="Search names and notes" style="flex:1;min-width:160px" value="' + B.esc(filter.q) + '">' +
      '<select id="vcat" style="max-width:220px"><option value="">All categories</option>' + catOpts + "</select></div></div>" +
      '<div id="vview"></div><div id="vlist" class="list"></div>' +

      '<details class="card section"><summary><b>⚙️ Passphrase, recovery key, backup</b></summary>' +
      '<form id="vpw"><h3>Change the family passphrase</h3><label for="vpo">Current passphrase</label><input type="password" id="vpo" autocomplete="current-password">' +
      '<label for="vpn1">New passphrase</label><input type="password" id="vpn1" autocomplete="new-password"><label for="vpn2">Type it again</label><input type="password" id="vpn2" autocomplete="new-password">' +
      '<div class="btns" style="margin-top:8px"><button class="btn">Change passphrase</button><span id="vpm" class="small muted"></span></div>' +
      '<p class="small muted">Every other device gets locked. Tell the family the new one.</p></form>' +
      '<form id="vrec"><h3>New recovery key</h3><p class="small">Lost the printed key? Make a new one; the old one stops working.</p><label for="vrp">Family passphrase</label><input type="password" id="vrp" autocomplete="current-password">' +
      '<div class="btns" style="margin-top:8px"><button class="btn ghost">Make a new recovery key</button><span id="vrcm" class="small muted"></span></div></form>' +
      '<h3>Backup</h3><p class="small">Download an <b>encrypted</b> copy of the whole vault to keep on a USB stick or another computer. It still needs the family passphrase to open; instructions are inside.</p>' +
      '<div class="btns"><button class="btn ghost" id="vexp" type="button">Download encrypted backup</button><span id="vem" class="small muted"></span></div>' +
      '<h3>Lost a phone?</h3><div class="btns"><button class="btn danger" id="vlall" type="button">Lock the vault on every device</button></div></details>' +

      '<details class="card"><summary><b>Recent activity</b></summary><div class="small">' + ((s.activity || []).map(function (a) {
        return "<div>" + B.esc(when(a.at)) + " — <b>" + B.esc(a.who) + "</b> " + B.esc(a.what) + "</div>";
      }).join("") || '<span class="muted">Nothing yet.</span>') + "</div></details>");
    el("vcat").value = filter.cat;
    wire(s);
    loadList();
    timer = setTimeout(function () { if (onPage()) lockedAgain(); }, (B.num(s.idle_left) + 3) * 1000);
  }

  function loadList() {
    B.api("/api/vault/list", { body: { q: filter.q, category: filter.cat } }).then(function (r) {
      if (!check(r)) return;
      var box = el("vlist");
      if (!box) return;
      if (r._status !== 200) { box.innerHTML = '<p class="warnbox">' + B.esc(r.error || "Couldn’t read the vault.") + "</p>"; return; }
      box.innerHTML = r.files.map(function (f) {
        var img = /^image\/(jpeg|png)$/.test(f.type || "");
        return '<div class="card"><div class="row"><div style="flex:1;min-width:200px"><h3>' + icon(f) + " " + B.esc(f.name) + "</h3>" +
          '<div class="meta">' + B.esc(catLabels[f.cat] || f.cat) + " · " + B.esc(B.human(B.num(f.size))) + " · added " + B.esc(when(f.added)) + (f.by ? " by " + B.esc(f.by) : "") + "</div>" +
          (f.note ? '<p class="small">' + B.esc(f.note) + "</p>" : "") + "</div>" +
          '<div class="btns">' + (f.kind === "note" ? '<button class="btn" data-act="note" data-id="' + B.esc(f.id) + '">Open</button>' :
            (img ? '<button class="btn" data-act="view" data-id="' + B.esc(f.id) + '">View</button>' : "") +
            '<button class="btn' + (img ? " ghost" : "") + '" data-act="save" data-id="' + B.esc(f.id) + '">Download</button>') +
          '<button class="btn ghost" data-act="edit" data-id="' + B.esc(f.id) + '">Edit</button>' +
          '<button class="btn ghost" data-act="del" data-id="' + B.esc(f.id) + '">Delete</button></div></div></div>';
      }).join("") || '<p class="muted">' + (filter.q || filter.cat ? "Nothing matches." : "The vault is empty. Add copies of your most important papers first: IDs, insurance, prescriptions.") + "</p>";
      box.onclick = function (e) {
        var b = e.target.closest ? e.target.closest("[data-act]") : null;
        if (!b) return;
        var id = b.getAttribute("data-id"), f = null;
        for (var i = 0; i < r.files.length; i++) if (r.files[i].id === id) f = r.files[i];
        if (f) act(b.getAttribute("data-act"), f);
      };
    });
  }

  function fileUrl(f, inline) { return "/api/vault/file/" + encodeURIComponent(f.id) + (inline ? "?inline=1" : ""); }

  function act(what, f) {
    var view = el("vview");
    if (what === "save" || what === "view") {
      B.blobUrl(fileUrl(f, what === "view")).then(function (u) {
        if (what === "view") {
          view.innerHTML = '<div class="card"><div class="row"><b style="flex:1">' + B.esc(f.name) + '</b><button class="btn ghost" id="vclose" type="button">Close</button></div>' +
            '<img alt="" style="max-width:100%;margin-top:8px;border-radius:6px" src="' + B.esc(u) + '"></div>';
          el("vclose").onclick = function () { view.innerHTML = ""; URL.revokeObjectURL(u); };
          view.scrollIntoView();
          return;
        }
        var a = document.createElement("a");
        a.href = u; a.download = f.name; a.rel = "noopener";
        document.body.appendChild(a); a.click();
        setTimeout(function () { URL.revokeObjectURL(u); if (a.parentNode) a.parentNode.removeChild(a); }, 60000);
      }).catch(function (err) { if (/423/.test(String(err))) lockedAgain(); else alert("Couldn’t open it: " + err.message); });
    } else if (what === "note") {
      B.api("/api/vault/note/" + encodeURIComponent(f.id)).then(function (r) {
        if (!check(r)) return;
        if (r._status !== 200) return alert(r.error || "Couldn’t open the note.");
        view.innerHTML = '<div class="card"><div class="row"><b style="flex:1">📝 ' + B.esc(f.name) + '</b><button class="btn ghost" id="vnedit" type="button">Edit text</button><button class="btn ghost" id="vclose" type="button">Close</button></div>' +
          '<div id="vnbody" style="white-space:pre-wrap;margin-top:8px">' + B.esc(r.text) + "</div></div>";
        el("vclose").onclick = function () { view.innerHTML = ""; };
        el("vnedit").onclick = function () {
          el("vnbody").innerHTML = '<textarea id="vnet" rows="10" maxlength="20000">' + B.esc(r.text) + '</textarea><div class="btns"><button class="btn" id="vnsave" type="button">Save</button><span id="vnem" class="small muted"></span></div>';
          el("vnsave").onclick = function () {
            B.api("/api/vault/edit", { body: { id: f.id, text: el("vnet").value } }).then(function (x) {
              if (!check(x)) return;
              if (!x.ok) return msg("vnem", x.error || "Couldn’t save.", true);
              view.innerHTML = ""; loadList();
            });
          };
        };
        view.scrollIntoView();
      });
    } else if (what === "edit") {
      var catOpts = Object.keys(catLabels).map(function (c) { return '<option value="' + B.esc(c) + '"' + (c === f.cat ? " selected" : "") + ">" + B.esc(catLabels[c]) + "</option>"; }).join("");
      view.innerHTML = '<form id="ved" class="card"><h3>Edit details</h3><label for="ven">Name</label><input type="text" id="ven" maxlength="120" value="' + B.esc(f.name) + '">' +
        '<label for="vec">Category</label><select id="vec">' + catOpts + '</select><label for="veo">Note</label><input type="text" id="veo" maxlength="1000" value="' + B.esc(f.note || "") + '">' +
        '<div class="btns" style="margin-top:8px"><button class="btn">Save</button><button class="btn ghost" id="vecl" type="button">Cancel</button><span id="vem2" class="small muted"></span></div></form>';
      el("vecl").onclick = function () { view.innerHTML = ""; };
      el("ved").onsubmit = function (e) {
        e.preventDefault();
        B.api("/api/vault/edit", { body: { id: f.id, name: el("ven").value, category: el("vec").value, note: el("veo").value } }).then(function (x) {
          if (!check(x)) return;
          if (!x.ok) return msg("vem2", x.error || "Couldn’t save.", true);
          view.innerHTML = ""; loadList();
        });
      };
      view.scrollIntoView();
    } else if (what === "del") {
      if (!confirm("Delete “" + f.name + "” from the vault? This can’t be undone.")) return;
      B.api("/api/vault/delete", { body: { id: f.id } }).then(function (x) {
        if (!check(x)) return;
        if (!x.ok) alert(x.error || "Couldn’t delete it.");
        loadList();
      });
    }
  }

  function wire(s) {
    el("vlock").onclick = function () { B.api("/api/vault/lock", { body: {} }).then(render); };
    var t = null;
    el("vq").oninput = function () { clearTimeout(t); t = setTimeout(function () { filter.q = el("vq").value; loadList(); }, 250); };
    el("vcat").onchange = function () { filter.cat = el("vcat").value; loadList(); };

    el("vadd").onsubmit = function (e) {
      e.preventDefault();
      var files = el("vf").files, list = [], i;
      for (i = 0; i < files.length; i++) list.push(files[i]);
      if (!list.length) return msg("vam", "Choose a file first.", true);
      for (i = 0; i < list.length; i++) if (list[i].size > B.num(s.max_file)) return msg("vam", "“" + list[i].name + "” is too big (" + B.human(list[i].size) + ").", true);
      var cat = el("vc").value, note = el("vno").value, name = list.length === 1 ? el("vn").value : "", done = 0;
      function next() {
        if (!list.length) { msg("vam", "Saved " + done + " file" + (done === 1 ? "" : "s") + "."); el("vadd").reset(); return render(); }
        var f = list.shift();
        msg("vam", "Encrypting “" + f.name + "”…");
        B.api("/api/vault/upload-start", { body: { name: name || f.name, category: cat, note: note, size: f.size } }).then(function (r) {
          if (!check(r)) return null;
          if (!r.upload) { msg("vam", r.error || "Couldn’t start the upload.", true); return null; }
          return B.upload("/api/vault/upload/" + r.upload, f, "application/octet-stream");
        }).then(function (r) {
          if (!r) return;
          if (!check(r)) return;
          if (!r.ok) return msg("vam", (r.error || "Upload failed.") + " (" + f.name + ")", true);
          done++; next();
        }).catch(function () { msg("vam", "The upload failed. Check the Wi-Fi and try again.", true); });
      }
      next();
    };

    el("vnote").onsubmit = function (e) {
      e.preventDefault();
      B.api("/api/vault/note", { body: { name: el("vnt").value, category: el("vnc").value, text: el("vnx").value } }).then(function (r) {
        if (!check(r)) return;
        if (!r.ok) return msg("vnm", r.error || "Couldn’t save the note.", true);
        render();
      });
    };

    el("vpw").onsubmit = function (e) {
      e.preventDefault();
      if (el("vpn1").value !== el("vpn2").value) return msg("vpm", "The two new passphrases don’t match.", true);
      msg("vpm", "Changing…");
      B.api("/api/vault/passphrase", { body: { old: el("vpo").value, "new": el("vpn1").value } }).then(function (r) {
        if (!r.ok) return msg("vpm", r.error || "That didn’t work.", true);
        el("vpw").reset(); msg("vpm", "Changed. Other devices are locked.");
      });
    };
    el("vrec").onsubmit = function (e) {
      e.preventDefault();
      B.api("/api/vault/recovery-key", { body: { passphrase: el("vrp").value } }).then(function (r) {
        if (!r.ok) return msg("vrcm", r.error || "That didn’t work.", true);
        recoveryView(r.recovery_key, false);
      });
    };
    el("vexp").onclick = function () {
      B.api("/api/vault/export", { body: {} }).then(function (r) {
        if (!check(r)) return;
        if (!r.url || !/^\/api\/vault\/export\/[\w-]+$/.test(r.url)) return msg("vem", r.error || "Couldn’t make a backup.", true);
        msg("vem", "Downloading… keep this page open until it finishes.");
        location.href = r.url;
      });
    };
    el("vlall").onclick = function () {
      if (!confirm("Lock the vault on every family device, including this one?")) return;
      B.api("/api/vault/lock", { body: { all: true } }).then(render);
    };
  }

  window.addEventListener("hashchange", function () { if (!onPage()) clearTimer(); });
  B.register("/vault", render);
})();
