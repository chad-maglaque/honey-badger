/* Ke-Shi / Nu:Time™ core for browsers — mirrors software/nutime/nutime.py.
   1 day = 10 gongs = 1,000 beats = 100,000 ticks; tick = 0.864 s; display GG:BB:TT.
   Integer milliseconds throughout (max 8.64e12 < 2^53), truncating, never rounding ahead.
   Derive from the absolute instant on every render; never accumulate ticks. */
(function (root) {
  "use strict";
  var DAY_MS = 86400000, TICKS = 100000;
  var LABELS = {
    zh: { gong: "更", beat: "分", tick: "刻", spoken: ["gēng", "fēn", "kè"] },
    en: { gong: "gong", beat: "beat", tick: "tick", spoken: ["gong", "beat", "tick"] },
    fr: { gong: "gong", beat: "beat", tick: "tic", spoken: ["gong", "beat", "tic"] },
    es: { gong: "gong", beat: "beat", tick: "tic", spoken: ["gong", "beat", "tic"] },
    de: { gong: "gong", beat: "beat", tick: "tick", spoken: ["gong", "beat", "tick"] }
  };
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function mod(a, n) { return ((a % n) + n) % n; }

  function fields(ticks) {
    ticks = mod(ticks, TICKS);
    var gong = Math.floor(ticks / 10000), beat = Math.floor(ticks / 100) % 100, tick = ticks % 100;
    return { ticksOfDay: ticks, gong: gong, beat: beat, tick: tick, beatsOfDay: ticks / 100,
             display: pad(gong) + ":" + pad(beat) + ":" + pad(tick) };
  }
  function ticksFromMs(msIntoDay, dayMs) {
    dayMs = dayMs || DAY_MS;
    // floor, never round: 863.6 ms is still tick 0 (solar offsets make msIntoDay fractional)
    return Math.floor(mod(Math.floor(msIntoDay), dayMs) * TICKS / dayMs);
  }

  // ── solar model (NOAA general solar position) ──
  function terms(ms) {
    var d = new Date(ms), y = d.getUTCFullYear();
    var leap = (y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0)) ? 366 : 365;
    var doy = Math.floor((Date.UTC(y, d.getUTCMonth(), d.getUTCDate()) - Date.UTC(y, 0, 1)) / DAY_MS) + 1;
    var hour = d.getUTCHours() + d.getUTCMinutes() / 60 + (d.getUTCSeconds() + d.getUTCMilliseconds() / 1000) / 3600;
    var g = 2 * Math.PI / leap * (doy - 1 + (hour - 12) / 24);
    var eqt = 229.18 * (0.000075 + 0.001868 * Math.cos(g) - 0.032077 * Math.sin(g) - 0.014615 * Math.cos(2 * g) - 0.040849 * Math.sin(2 * g));
    var decl = 0.006918 - 0.399912 * Math.cos(g) + 0.070257 * Math.sin(g) - 0.006758 * Math.cos(2 * g) +
               0.000907 * Math.sin(2 * g) - 0.002697 * Math.cos(3 * g) + 0.00148 * Math.sin(3 * g);
    return { eqt: eqt, decl: decl, utcMin: hour * 60 };
  }
  function sun(ms, lat, lon) {
    var t = terms(ms), R = Math.PI / 180;
    var ha = ((t.utcMin + t.eqt + 4 * lon) / 4 - 180) * R, phi = lat * R;
    var cz = Math.sin(phi) * Math.sin(t.decl) + Math.cos(phi) * Math.cos(t.decl) * Math.cos(ha);
    cz = Math.max(-1, Math.min(1, cz));
    var alt = 90 - Math.acos(cz) / R;
    var az = mod(Math.atan2(Math.sin(ha), Math.cos(ha) * Math.sin(phi) - Math.tan(t.decl) * Math.cos(phi)) / R + 180, 360);
    return { altitude: alt, azimuth: az };
  }
  // Sun events on calendar date y-m-d (month 1–12) *at the place*; returns UTC ms.
  // zenith 90.833 = sunrise/sunset, 96 = civil twilight. Mirrors nutime.sun_events().
  function sunEventsOn(y, m, d, lat, lon, zenith) {
    var mid = Date.UTC(y, m - 1, d);
    var t = terms(mid + DAY_MS / 2 - 4 * lon * 60000), R = Math.PI / 180, phi = lat * R;
    var x = Math.cos((zenith || 90.833) * R) / (Math.cos(phi) * Math.cos(t.decl)) - Math.tan(phi) * Math.tan(t.decl);
    var noon = mid + (720 - 4 * lon - t.eqt) * 60000;
    if (x < -1) return { polar: "day", solarNoon: noon };
    if (x > 1) return { polar: "night", solarNoon: noon };
    var h = Math.acos(x) / R;
    return { polar: null, solarNoon: noon, sunrise: mid + (720 - 4 * (lon + h) - t.eqt) * 60000,
             sunset: mid + (720 - 4 * (lon - h) - t.eqt) * 60000 };
  }
  // Today's events at the place: its local calendar date in `tz` (mirrors nutime.sun_events_at).
  function sunEvents(ms, lat, lon, tz, zenith) {
    var p = tz ? localParts(ms, tz) : null;
    if (!p) { var s = new Date(ms + lon / 15 * 3600000); p = { y: s.getUTCFullYear(), m: s.getUTCMonth() + 1, d: s.getUTCDate() }; }
    return sunEventsOn(p.y, p.m, p.d, lat, lon, zenith);
  }

  // ── reference layer ──
  // Wall-clock fields in an IANA zone. formatToParts is missing on old Safari (< 11),
  // so fall back to parsing format(), and with no time-zone support at all to the
  // device's own clock (and say nothing worse than "local").
  var partsCache = {};
  function localParts(ms, tz) {
    var d = new Date(ms), f;
    try {
      f = partsCache[tz] || (partsCache[tz] = new Intl.DateTimeFormat("en-US", { timeZone: tz, hour12: false,
        year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }));
      if (f.formatToParts) {
        var p = {}; f.formatToParts(d).forEach(function (x) { p[x.type] = x.value; });
        if (p.year && p.hour != null) return { y: +p.year, m: +p.month, d: +p.day, H: +p.hour % 24, M: +p.minute, S: +p.second };
      }
      var m = /(\d+)\/(\d+)\/(\d+),?\s+(\d+):(\d+):(\d+)/.exec(f.format(d));
      if (m) return { y: +m[3], m: +m[1], d: +m[2], H: +m[4] % 24, M: +m[5], S: +m[6] };
    } catch (e) {}
    return null;
  }
  function wallMs(ms, tz) {
    var p = localParts(ms, tz);
    if (!p) { var d = new Date(ms); p = { H: d.getHours(), M: d.getMinutes(), S: d.getSeconds() }; }
    return (p.H * 3600 + p.M * 60 + p.S) * 1000 + mod(Math.floor(ms), 1000);
  }
  function msIntoDay(ms, mode, o) {
    o = o || {};
    if (mode === "utc") return mod(ms, DAY_MS);
    if (mode === "civil") return wallMs(ms, o.tz || "UTC");                       // "wall" DST policy
    if (mode === "solar" || mode === "apparent") {
      var lon = o.quantize ? Math.round(o.lon / 3.6) * 3.6 : o.lon;
      var off = lon / 15 * 3600000 + (mode === "apparent" ? terms(ms).eqt * 60000 : 0);
      return mod(ms + off, DAY_MS);
    }
    throw new Error("unknown mode " + mode);
  }
  function now(ms, mode, o) {
    var r = fields(ticksFromMs(msIntoDay(ms, mode, o)));
    r.reference = { utc: "UTC", civil: "civil-local", solar: "solar-mean", apparent: "solar-apparent" }[mode];
    return r;
  }

  // ── sky colour from solar altitude (artistic, not physical) ──
  var STOPS = [[-90, [6, 9, 22]], [-18, [11, 16, 38]], [-12, [24, 30, 66]], [-6, [62, 58, 110]],
               [-2, [176, 96, 82]], [2, [238, 150, 90]], [8, [150, 190, 230]], [30, [96, 160, 228]], [90, [60, 130, 220]]];
  function sky(alt) {
    for (var i = 1; i < STOPS.length; i++) if (alt <= STOPS[i][0]) {
      var a = STOPS[i - 1], b = STOPS[i], t = (alt - a[0]) / (b[0] - a[0]);
      return "rgb(" + [0, 1, 2].map(function (k) { return Math.round(a[1][k] + (b[1][k] - a[1][k]) * t); }).join(",") + ")";
    }
    return "rgb(60,130,220)";
  }

  // ── one time formatter for the whole Badger: "local" | "utc" | "nutime" ──
  // The viewer's choice (localStorage "timefmt") beats the Badger default (NuTime.config.display).
  var config = { display: "local", tz: undefined, reference: "civil", lon: undefined };
  function pref() { try { return localStorage.getItem("timefmt") || config.display; } catch (e) { return config.display; } }
  function setPref(m) { try { localStorage.setItem("timefmt", m); } catch (e) {} }
  function cycle() { var order = ["local", "utc", "nutime"], i = order.indexOf(pref()); setPref(order[(i + 1) % 3]); return pref(); }
  function parseTime(v) {                       // ms | ISO with Z, ±HH:MM or ±HHMM offsets
    if (v == null) return NaN;
    if (typeof v === "number") return v < 1e12 ? v * 1000 : v;
    return Date.parse(String(v).replace(/([+-]\d{2})(\d{2})$/, "$1:$2"));
  }
  function ymd(ms, tz) {
    var p = tz ? localParts(ms, tz) : null;
    if (!p) { var d = new Date(ms); p = tz === "UTC" ? { y: d.getUTCFullYear(), m: d.getUTCMonth() + 1, d: d.getUTCDate() } : { y: d.getFullYear(), m: d.getMonth() + 1, d: d.getDate() }; }
    return p.y + "-" + pad(p.m) + "-" + pad(p.d);
  }
  function fmt(v, o) {
    o = o || {};
    var ms = parseTime(v), mode = o.mode || pref(), tz = o.tz || config.tz;
    if (isNaN(ms)) return "—";
    if (mode === "nutime") {
      var ref = o.reference || config.reference;
      var r = now(ms, ref, { tz: tz || "UTC", lon: o.lon != null ? o.lon : config.lon });
      var t = o.short ? r.display.slice(0, 5) : r.display;
      return (o.date ? ymd(ms, ref === "utc" ? "UTC" : tz) + " " : "") + t + " NT";
    }
    if (mode === "utc") {
      var iso = new Date(ms).toISOString();
      return (o.date ? iso.slice(0, 10) + " " : "") + iso.slice(11, o.short ? 16 : 19) + " UTC";
    }
    var opts = { timeZone: tz, hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZoneName: "short" };
    if (!o.short) opts.second = "2-digit";
    try {
      var f = new Intl.DateTimeFormat("en-US", opts);
      if (!f.formatToParts) return (o.date ? ymd(ms, tz) + " " : "") + f.format(new Date(ms));
      var parts = {}; f.formatToParts(new Date(ms)).forEach(function (x) { parts[x.type] = x.value; });
      return (o.date ? ymd(ms, tz) + " " : "") + pad(+parts.hour % 24) + ":" + parts.minute + (o.short ? "" : ":" + parts.second) + " " + (parts.timeZoneName || "");
    } catch (e) { return new Date(ms).toLocaleString(); }
  }
  var LABEL = { local: "Local", utc: "UTC", nutime: "Nu:Time™" };

  root.NuTime = { config: config, pref: pref, setPref: setPref, cycle: cycle, fmt: fmt, parseTime: parseTime, LABEL: LABEL, DAY_MS: DAY_MS, TICKS: TICKS, LABELS: LABELS, fields: fields, ticksFromMs: ticksFromMs,
                  msIntoDay: msIntoDay, now: now, sun: sun, sunEvents: sunEvents, sunEventsOn: sunEventsOn, localParts: localParts, sky: sky,
                  equationOfTimeMinutes: function (ms) { return terms(ms).eqt; } };
})(this);
