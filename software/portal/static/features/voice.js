/* Voice mode — page script (feature "voice"). ES5, no frameworks.
   Recording: MediaRecorder where the browser allows the microphone (HTTPS or localhost; Chrome gives
   webm/opus, Safari mp4/aac, Firefox ogg/opus). On plain http:// the microphone API is missing, so the
   page offers <input type=file accept=audio/* capture>: on Android it opens the voice recorder, on an
   iPhone it lets you pick a Voice Memos recording. Either way the raw file goes to /api/voice/ask. */
(function () {
  var B = window.Badger;
  if (!B) return;
  var esc = B.esc;
  var MIMES = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus", "audio/webm", "audio/aac"];
  var STAGES = { queued: "Waiting for its turn…", listening: "Listening to your question…",
    thinking: "Looking it up in the library…", speaking: "Getting the spoken answer ready…" };
  var rec = null, timer = null, pollTimer = null;

  function canRecord() {
    return !!(window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  }
  function pickMime() {
    if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return "";
    for (var i = 0; i < MIMES.length; i++) if (MediaRecorder.isTypeSupported(MIMES[i])) return MIMES[i];
    return "";
  }
  function $(id) { return document.getElementById(id); }
  function say(msg) { var el = $("vState"); if (el) el.textContent = msg || ""; }

  function stopTracks(stream) {
    if (stream && stream.getTracks) stream.getTracks().forEach(function (t) { t.stop(); });
  }

  function startRecording(info) {
    var btn = $("vRec");
    navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }).then(function (stream) {
      var mime = pickMime(), chunks = [], left = info.max_seconds;
      var mr;
      try { mr = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream); }
      catch (e) { mr = new MediaRecorder(stream); }
      rec = { mr: mr, stream: stream };
      mr.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
      mr.onstop = function () {
        clearInterval(timer); timer = null; stopTracks(stream); rec = null;
        btn.textContent = "🎙️ Tap to ask"; btn.classList.remove("danger");
        var type = (mr.mimeType || mime || "audio/webm").split(";")[0];
        var blob = new Blob(chunks, { type: type });
        if (blob.size < 200) { say("That was too short — tap, ask your question, then tap again."); return; }
        send(blob, type, info);
      };
      mr.start(1000);
      btn.textContent = "■ Stop (" + left + " s)"; btn.classList.add("danger");
      say("Listening… ask your question, then tap Stop.");
      timer = setInterval(function () {
        left -= 1;
        if (left <= 0) { if (mr.state !== "inactive") mr.stop(); return; }
        btn.textContent = "■ Stop (" + left + " s)";
      }, 1000);
    }).catch(function () {
      say("The microphone isn’t available. Allow microphone access for this page, or use “Record with your phone” below.");
      var fb = $("vFallback"); if (fb) fb.style.display = "";
    });
  }

  function send(blob, type, info) {
    if (blob.size > info.max_bytes) { say("That recording is too big. Keep questions under " + info.max_seconds + " seconds."); return; }
    var longQ = $("vLong") && $("vLong").checked;
    clearAnswer();
    say("Sending…");
    setBusy(true);
    B.upload("/api/voice/ask" + (longQ ? "?long=1" : ""), blob, type || "application/octet-stream").then(function (r) {
      if (r._status !== 200) { setBusy(false); say(r.error || "The Badger couldn’t take that recording."); return; }
      say(STAGES.queued);
      poll(r.id, 0);
    }).catch(function () { setBusy(false); say("The recording couldn’t be sent (it may be too big, or the Wi-Fi dropped)."); });
  }

  function setBusy(on) {
    var b = $("vRec"), f = $("vFile");
    if (b) b.disabled = on;
    if (f) f.disabled = on;
  }

  function clearAnswer() {
    ["vHeard", "vAnswer", "vSources", "vAudio", "vNote"].forEach(function (id) { var el = $(id); if (el) el.innerHTML = ""; });
  }

  function poll(id, n) {
    clearTimeout(pollTimer);
    if (!$("vState")) return;                          // left the page
    if (n > 300) { setBusy(false); say("That took too long. Try again."); return; }
    B.api("/api/voice/job?id=" + encodeURIComponent(id)).then(function (j) {
      if (!$("vState")) return;
      if (j._status !== 200) { setBusy(false); say(j.error || "That question expired."); return; }
      if (j.transcript) $("vHeard").innerHTML = "<b>You asked:</b> “" + esc(j.transcript) + "”";
      if (j.answer) $("vAnswer").textContent = j.answer;
      if (j.sources && j.sources.length) $("vSources").innerHTML = '<h3 class="section">Sources</h3>' + j.sources.map(function (s) {
        return '<div class="result">' + (s.n ? "[" + B.num(s.n) + "] " : "") + '<a href="' + B.href(s.url) + '">' + esc(s.title) +
          '</a> <span class="src">' + esc(s.source) + "</span></div>";
      }).join("");
      if (j.stage === "error") { setBusy(false); say(j.error || "Something went wrong."); return; }
      if (j.stage === "done") {
        setBusy(false); say(j.ai === false ? "No AI model on this Badger — read out the best library matches instead." : "");
        if (j.note) $("vNote").textContent = j.note;
        if (j.audio) {
          $("vAudio").innerHTML = '<audio id="vPlayer" controls preload="auto" src="/api/voice/audio?id=' + encodeURIComponent(id) + '"></audio>';
          var a = $("vPlayer");
          try { var p = a.play(); if (p && p.catch) p.catch(function () { say("Tap ▶ to hear the answer."); }); } catch (e) { /* tap ▶ */ }
        }
        return;
      }
      say(j.position ? "Waiting for its turn (" + j.position + " ahead)…" : (STAGES[j.stage] || "Working…"));
      pollTimer = setTimeout(function () { poll(id, n + 1); }, 1000);
    }).catch(function () { pollTimer = setTimeout(function () { poll(id, n + 1); }, 2000); });
  }

  function adminHtml(info) {
    if (!B.isAdmin()) return "";
    return '<div class="card section"><h2>Admin</h2><p><label><input type="checkbox" id="vOn"' + (info.enabled ? " checked" : "") +
      "> Voice questions switched on</label></p><p class=\"small muted\" id=\"vAdmMsg\"></p></div>";
  }

  B.register("/voice", function () {
    if (rec && rec.mr && rec.mr.state !== "inactive") { try { rec.mr.stop(); } catch (e) { /* already stopped */ } }
    B.h('<h1>🎙️ Voice</h1><p class="muted">Loading…</p>');
    B.api("/api/voice/info").then(function (info) {
      if (info._status !== 200) {
        B.h('<h1>🎙️ Voice</h1><p class="muted">' + esc(info.error || "Not available.") + "</p>" +
          (info._status === 401 ? '<p>This is for family members. Join with an invite code on the <a href="#/board/family">family board</a> first.</p>' : ""));
        return;
      }
      var out = '<h1>🎙️ Voice</h1><p class="muted">Ask a question out loud. The Badger writes down what it heard, looks it up in its library and reads the answer to you.</p>' +
        '<div class="warnbox">Answers come from a small offline AI and the Badger’s articles. They can be wrong — check the sources, especially for medical questions. Your recording is deleted as soon as it has been turned into text.</div>';
      if (!info.enabled) out += '<div class="card section"><p>Voice questions are switched off on this Badger. You can still type a question on the <a href="#/ask">Ask page</a>.</p></div>';
      else if (!info.stt) out += '<div class="card section"><h2>Not installed yet</h2><p>' + esc(info.install_hint) + '</p><p>Meanwhile, type your question on the <a href="#/ask">Ask page</a>.</p></div>';
      else {
        var direct = canRecord();
        out += '<div class="card section">' +
          (direct ? '<div class="btns"><button class="btn" id="vRec" style="min-height:64px;font-size:1.2em">🎙️ Tap to ask</button></div>' : "") +
          '<div id="vFallback"' + (direct ? ' style="display:none"' : "") + ">" +
          (direct ? "" : '<p class="small">This browser only lets web pages use the microphone over a secure connection, so use your phone’s recorder instead (up to ' +
            esc(info.max_seconds) + ' seconds).</p>') +
          '<label class="btn" for="vFile" style="min-height:64px">🎙️ Record with your phone</label>' +
          '<input type="file" id="vFile" accept="audio/*" capture style="position:absolute;left:-9999px;width:1px;height:1px">' +
          (direct ? "" : '<p class="small muted">On an iPhone: record in Voice Memos, then pick it here. For one-tap recording, install the Badger certificate (<a href="#/devices">Add to your device</a>) and open <b>https://' +
            esc(location.hostname) + "</b>.</p>") + "</div>" +
          (info.allow_long ? '<p class="small"><label><input type="checkbox" id="vLong"> Longer answer (slower)</label></p>' : "") +
          '<p class="small muted" id="vState">' + (info.busy ? "Someone else is asking right now — you may have to wait a moment." : "") + "</p></div>" +
          '<div class="card section"><p id="vHeard" class="muted"></p><div class="answer" id="vAnswer"></div><div id="vAudio" class="section"></div>' +
          '<p class="small muted" id="vNote"></p><div id="vSources" class="small"></div></div>';
      }
      out += adminHtml(info);
      B.h(out);
      var btn = $("vRec");
      if (btn) btn.onclick = function () {
        if (rec && rec.mr && rec.mr.state !== "inactive") { rec.mr.stop(); return; }
        startRecording(info);
      };
      var file = $("vFile");
      if (file) file.onchange = function () {
        var f = file.files && file.files[0];
        if (f) send(f, f.type || "application/octet-stream", info);
        file.value = "";
      };
      var on = $("vOn");
      if (on) on.onchange = function () {
        B.api("/api/voice/settings", { body: { enabled: on.checked } }).then(function (r) {
          $("vAdmMsg").textContent = r.ok ? (r.enabled ? "Switched on." : "Switched off.") : (r.error || "Not saved.");
        }).catch(function () { $("vAdmMsg").textContent = "The Badger didn’t answer."; });
      };
    }).catch(function () {
      B.h('<h1>🎙️ Voice</h1><p class="muted">Couldn’t reach the Badger.</p>');
    });
  });
})();
