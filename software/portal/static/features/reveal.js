/* What this Badger reveals — page script (feature "reveal"). ES5, no frameworks. Admin only. */
(function () {
  var B = window.Badger;
  if (!B) return;
  var esc = B.esc;
  var MARK = { ok: "✅", warn: "⚠️", info: "ℹ️" };

  function render(d, msg) {
    var items = d.items || [];
    var out = '<div class="crumbs"><a href="#/settings">Settings</a> › What this Badger reveals</div><h1>👁️ What this Badger reveals</h1>' +
      '<p class="small muted">Everything that tells someone outside the family something about this household — and a fix where there is one. ' +
      "Only the admin and the Badger’s own screen see this page.</p>";
    if (msg) out += '<div class="card">' + msg + "</div>";
    out += '<p><b>' + B.num(d.summary && d.summary.warn) + "</b> to look at · " + B.num(d.summary && d.summary.ok) + " fine</p>";
    out += '<div class="list">' + items.map(function (it, i) {
      return '<div class="result"><b>' + (MARK[it.state] || "") + " " + esc(it.title) + "</b>" +
        (it.detail ? '<div class="small">' + esc(it.detail) + "</div>" : "") +
        (it.how ? '<div class="small muted">' + esc(it.how) + "</div>" : "") +
        (it.fix ? '<div class="btns"><button class="btn ghost" data-fix="' + i + '">' + esc(it.fix.label) + "</button></div>" : "") + "</div>";
    }).join("") + "</div>" +
      '<p class="small section"><a href="#/posture">Security posture →</a> · <a href="#/circles">Trusted circle →</a></p>';
    B.h(out);
    Array.prototype.forEach.call(document.querySelectorAll("[data-fix]"), function (b) {
      b.onclick = function () {
        var it = items[Number(b.getAttribute("data-fix"))];
        if (!it || !it.fix || !confirm(it.fix.label + "?")) return;
        b.disabled = true;
        B.api("/api/reveal/fix", { body: { action: it.fix.action } }).then(function (r) {
          if (r.items) render(r, esc(r.message || "Done."));
          else render(d, esc(r.error || "That didn’t work."));
        });
      };
    });
  }

  B.register("/reveal", function () {
    B.h('<h1>👁️ What this Badger reveals</h1><p class="muted">Checking…</p>');
    B.api("/api/reveal").then(function (d) {
      if (!d || !d.items) return B.h('<h1>👁️ What this Badger reveals</h1><p>For the admin. <a href="#/settings">Sign in on the Settings page</a>.</p>');
      render(d);
    });
  });
})();
