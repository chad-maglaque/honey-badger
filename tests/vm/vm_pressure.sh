#!/bin/bash
# Honey Badger — pressure tests, run as root INSIDE the Debian 13 arm64 test VM after os/install.sh, a reboot
# and tests/vm/vm_checks.sh (which puts wlan2 in the "phone" namespace and leaves a PIN lockout behind).
#
# Needs more simulated radios than vm_checks.sh: mac80211_hwsim radios=12 (wlan0 onboard, wlan1 the hotspot,
# wlan2 vm_checks' phone, wlan3…wlan11 nine more phones, each in its own network namespace "phN").
#
#   sudo bash tests/vm/vm_pressure.sh [section …] [--psk P] [--pin N]
#
# Sections (default: phones load features abuse mesh power diskfull clock):
#   phones    9 phones join at once: association, DHCP, DNS, captive probes, launcher
#   load      HTTP load through nginx from every phone: latency p50/p95/p99, errors, portal RSS/CPU, recovery
#   features  member + admin smoke over real nginx+portal (invite/join, vault, supplies, aid station, skills,
#             missing post + photo, sensors/forecast/aircraft/satellites)
#   abuse     post floods, oversized uploads, slow clients, parallel wrong PINs, malformed requests
#   mesh      tests/test_mesh_courtesy.py on the VM's python; meshbridge start/stop with no radio
#   power     kill -9 each service, crash storm, restart storm
#   diskfull  the portal's data folder and the updater's report folder on a full filesystem
#   clock     wall-clock jumps forward/back: admin sessions, PIN lockout, vault idle lock
#   calm      MINUTES (default 10) of watching: failed units, restart counters, journal error rate
#   prereboot arm a Wi-Fi password change (switch-back pending), record state; then reboot the VM
#   postreboot after the reboot: everything back, switch-back re-armed, panel key + permissions, revert
set -u
PSK=badgertest123; PIN=246813; AP=10.10.10.10; SSID=HoneyBadger; MINUTES=10
SECTIONS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --psk) PSK=$2; shift ;;
    --pin) PIN=$2; shift ;;
    --minutes) MINUTES=$2; shift ;;
    *) SECTIONS+=("$1") ;;
  esac
  shift
done
[ ${#SECTIONS[@]} -gt 0 ] || SECTIONS=(phones load features abuse mesh power diskfull clock)
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
IW=/usr/sbin/iw
OUT=/var/tmp/hb-pressure; mkdir -p "$OUT"
PHONES=(3 4 5 6 7 8 9 10 11)
pass=0; fail=0; infos=0
ok()   { echo "PASS  $*"; pass=$((pass + 1)); }
bad()  { echo "FAIL  $*"; fail=$((fail + 1)); }
info() { echo "INFO  $*"; infos=$((infos + 1)); }
grep -q ' badger.lan' /etc/hosts || echo "$AP badger.lan" >> /etc/hosts    # the VM itself as a client
portal_pid() { systemctl show -p MainPID --value prepper-portal; }
rss_kb() { awk '/VmRSS/{print $2}' "/proc/$1/status" 2>/dev/null; }
threads() { awk '/Threads/{print $2}' "/proc/$1/status" 2>/dev/null; }
cpu_ticks() { awk '{print $14 + $15}' "/proc/$1/stat" 2>/dev/null; }
tracebacks() { journalctl -u prepper-portal --since "$1" --no-pager 2>/dev/null | grep -c "Traceback"; }
wait_http() {  # wait_http URL SECONDS — until the URL answers 200
  local i; for i in $(seq 1 "$2"); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$1")" = 200 ] && return 0; sleep 1
  done; return 1
}

# ───────────────────────────── the HTTP client (stdlib Python) ─────────────────────────────
cat > /tmp/hbp.py <<'PY'
import http.client, json, os, random, socket, ssl, sys, threading, time, hashlib, secrets

HOST = "badger.lan"
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE


def conn(tls=False, host=HOST, timeout=30):
    if tls:
        return http.client.HTTPSConnection(host, 443, timeout=timeout, context=CTX)
    return http.client.HTTPConnection(host, 80, timeout=timeout)


def req(method, path, body=None, tls=False, headers=None, raw=None, host=HOST, timeout=30, c=None):
    h = {"Content-Type": "application/json"} if (body is not None or raw is None) else {}
    h.update(headers or {})
    own = c is None
    c = c or conn(tls, host, timeout)
    try:
        c.request(method, path, raw if raw is not None else (json.dumps(body) if body is not None else None), h)
        r = c.getresponse(); data = r.read()
        hd = {k.lower(): v for k, v in r.getheaders()}
    finally:
        if own:
            c.close()
    try:
        return r.status, json.loads(data), hd
    except ValueError:
        return r.status, data, hd


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs); k = min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))
    return round(xs[k] * 1000, 1)


def load(conc, secs, paths, keepalive):
    lat, codes, errs, lock = [], {}, {}, threading.Lock()
    stop = time.time() + secs

    def worker():
        c = None
        while time.time() < stop:
            p = random.choice(paths)
            t = time.time()
            try:
                if c is None or not keepalive:
                    c = conn(timeout=20)
                c.request("GET", p, headers={"Accept-Encoding": "identity"})
                r = c.getresponse(); r.read()
                if r.getheader("Connection", "").lower() == "close":
                    c.close(); c = None
                with lock:
                    lat.append(time.time() - t); codes[r.status] = codes.get(r.status, 0) + 1
                if not keepalive:
                    c.close(); c = None
            except Exception as e:  # noqa: BLE001
                with lock:
                    errs[type(e).__name__] = errs.get(type(e).__name__, 0) + 1
                try:
                    c and c.close()
                except Exception:  # noqa: BLE001
                    pass
                c = None
                time.sleep(0.05)
    ts = [threading.Thread(target=worker) for _ in range(conc)]
    t0 = time.time()
    [t.start() for t in ts]; [t.join() for t in ts]
    n = len(lat)
    bad = sum(v for k, v in codes.items() if k >= 500) + sum(errs.values())
    print(json.dumps({"n": n, "secs": round(time.time() - t0, 1), "rps": round(n / max(0.1, time.time() - t0), 1),
                      "p50": pct(lat, 50), "p95": pct(lat, 95), "p99": pct(lat, 99), "max": pct(lat, 100),
                      "codes": codes, "errors": errs, "bad": bad, "lat": [round(x, 4) for x in lat]}))


class T:
    def __init__(self):
        self.p = self.f = 0

    def check(self, name, cond, detail=""):
        if cond:
            self.p += 1; print(f"PASS  {name}")
        else:
            self.f += 1; print(f"FAIL  {name}  {str(detail)[:400]}")
        sys.stdout.flush()
        return cond


def jpeg(pad=0):
    def seg(m, b):
        return bytes([0xFF, m]) + (len(b) + 2).to_bytes(2, "big") + b
    out = b"\xff\xd8" + seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    out += seg(0xDB, b"\x00" + bytes(range(64)))
    out += seg(0xC0, b"\x08\x00\x10\x00\x10\x03" + b"".join(bytes([i + 1, 0x11, 0]) for i in range(3)))
    out += seg(0xC4, b"\x00" + bytes([0] * 15 + [1]) + b"\x00")
    out += seg(0xDA, b"\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00") + b"\x12\x34" * (60 + pad) + b"\xff\xd9"
    return out


def features(pin, state_file):
    """Member + admin smoke through nginx (https for secrets, as the launcher does)."""
    t = T(); st = {}
    s, j, _ = req("POST", "/api/admin/login", {"pin": pin}, tls=True)
    adm = j.get("token") if isinstance(j, dict) else None
    if not t.check("admin login over https", s == 200 and adm, (s, j)):
        print(json.dumps({"p": t.p, "f": t.f})); return
    A = {"X-Admin-Token": adm}
    name = "Kim" + secrets.token_hex(2)
    s, j, _ = req("POST", "/api/admin/members", {"op": "invite", "name": name}, tls=True, headers=A)
    code = j.get("code") if isinstance(j, dict) else None
    t.check("admin makes a family invite (https)", s == 200 and code, (s, j))
    s, j, _ = req("POST", "/api/admin/members", {"op": "invite", "name": name + "x"}, headers=A)
    t.check("invite over plain http doesn't leak a code or answers sensibly", s in (200, 400, 403), (s, j))
    s, j, hd = req("POST", "/api/members/join", {"code": code or "", "name": name, "device": "pressure phone"})
    key = j.get("key") if isinstance(j, dict) else None
    t.check("member joins with the code", s == 200 and key, (s, j))
    t.check("join sets an HttpOnly member cookie", "httponly" in hd.get("set-cookie", "").lower(), hd.get("set-cookie"))
    M = {"X-Member-Token": key or ""}
    s, j, _ = req("GET", "/api/members/me", headers=M)
    t.check("/api/members/me knows the member", s == 200 and j.get("member", {}).get("name") == name, (s, j))
    s, j, _ = req("GET", "/api/board?board=family", headers=M)
    t.check("family board readable by member", s == 200, (s, j))
    s, j, _ = req("GET", "/api/board?board=family")
    t.check("family board refused without key", s in (401, 403), s)
    # ── vault
    pw = "correct horse battery " + secrets.token_hex(3)
    s, j, _ = req("GET", "/api/vault/status", headers=M)
    t.check("vault status", s == 200, (s, j))
    pwfile = os.path.join(os.path.dirname(state_file), "vault-pw")   # the vault is family-wide: keep its passphrase
    s, j, _ = req("POST", "/api/vault/setup", {"passphrase": pw}, headers=M, timeout=60)
    if s == 200:
        t.check("vault setup returns a recovery key", bool(j.get("recovery_key")), j)
        open(pwfile, "w").write(pw)
    else:
        t.check("vault already set up by an earlier run", s in (400, 409), (s, j))
        pw = open(pwfile).read() if os.path.exists(pwfile) else None
    if pw:
        s, j, _ = req("POST", "/api/vault/lock", {}, headers=M)
        s, j, _ = req("POST", "/api/vault/unlock", {"passphrase": "wrong wrong wrong"}, headers=M, timeout=60)
        t.check("vault refuses a wrong passphrase", s in (401, 403), (s, j))
        s, j, _ = req("POST", "/api/vault/unlock", {"passphrase": pw}, headers=M, timeout=60)
        t.check("vault unlocks", s == 200, (s, j))
        data = os.urandom(3 * 1024 * 1024 + 17)
        data = b"%PDF-1.4\n" + data
        s, j, _ = req("POST", "/api/vault/upload-start", {"size": len(data), "name": "deed.pdf", "category": "home"}, headers=M)
        tok = j.get("upload") if isinstance(j, dict) else None
        t.check("vault upload-start", s == 200 and tok, (s, j))
        s, j, _ = req("POST", "/api/vault/upload/" + str(tok), raw=data,
                      headers=dict(M, **{"Content-Type": "application/pdf", "X-Badger-Upload": "1"}), timeout=120)
        fid = (j.get("file") or {}).get("id") if isinstance(j, dict) else None
        t.check("vault upload 3 MB through nginx", s == 200 and fid, (s, j))
        s, j, _ = req("POST", "/api/vault/list", {}, headers=M)
        t.check("vault list shows it", s == 200 and fid and fid in json.dumps(j), (s, str(j)[:200]))
        s, body, _ = req("GET", "/api/vault/file/" + str(fid), headers=M, timeout=120)
        t.check("vault download is byte-identical", s == 200 and isinstance(body, bytes) and
                hashlib.sha256(body).digest() == hashlib.sha256(data).digest(), (s, len(body) if isinstance(body, bytes) else body))
        s, j, _ = req("POST", "/api/vault/upload-start", {"size": 26 * 1024 * 1024, "name": "big.pdf"}, headers=M)
        t.check("vault refuses a 26 MB file at upload-start", s == 413, (s, j))
        big = 25 * 1024 * 1024
        s, j, _ = req("POST", "/api/vault/upload-start", {"size": big, "name": "max.pdf"}, headers=M)
        tok = j.get("upload") if isinstance(j, dict) else None
        if t.check("vault accepts a 25 MB upload-start", s == 200 and tok, (s, j)):
            t0 = time.time()
            s, j, _ = req("POST", "/api/vault/upload/" + tok, raw=b"%PDF-1.4\n" + os.urandom(big - 9),
                          headers=dict(M, **{"Content-Type": "application/pdf", "X-Badger-Upload": "1"}), timeout=300)
            t.check(f"vault stores a 25 MB file ({time.time() - t0:.1f} s)", s == 200, (s, j))
            st["vault_25mb_s"] = round(time.time() - t0, 1)
            if s == 200:
                req("POST", "/api/vault/delete", {"id": j["file"]["id"]}, headers=M)
        # lying sizes
        s, j, _ = req("POST", "/api/vault/upload-start", {"size": 1000, "name": "x.pdf"}, headers=M)
        tok = j.get("upload")
        s, j, _ = req("POST", "/api/vault/upload/" + str(tok), raw=b"%PDF" + b"x" * 5000,
                      headers=dict(M, **{"Content-Type": "application/pdf", "X-Badger-Upload": "1"}))
        t.check("vault refuses a body bigger than announced", s == 400, (s, j))
        s, j, _ = req("POST", "/api/vault/upload/nosuchtoken", raw=b"x" * 10,
                      headers=dict(M, **{"Content-Type": "application/pdf", "X-Badger-Upload": "1"}))
        t.check("vault refuses an unknown upload token", s == 404, (s, j))
        s, j, _ = req("POST", "/api/vault/lock", {}, headers=M)
        t.check("vault locks", s == 200, (s, j))
        s, j, _ = req("GET", "/api/vault/file/" + str(fid), headers=M)
        t.check("locked vault refuses downloads (423)", s == 423, s)
        st["vault_pw"] = pw
    # ── supplies
    s, j, _ = req("POST", "/api/supplies/item", {"name": "Drinking water", "category": "water", "qty": 20,
                                                   "unit": "L"}, headers=M)
    iid = j.get("id") if isinstance(j, dict) else None
    t.check("supplies add", s == 200 and iid, (s, j))
    s, j, _ = req("POST", "/api/supplies/adjust", {"id": iid, "delta": 2, "use": True}, headers=M)
    t.check("supplies use 2 → 18", s == 200 and j.get("qty") == 18, (s, j))
    s, j, _ = req("GET", "/api/supplies", headers=M)
    t.check("supplies list", s == 200 and "Drinking water" in json.dumps(j), (s, str(j)[:200]))
    s, j, _ = req("GET", "/api/supplies")
    t.check("supplies refused to strangers", s == 401, s)
    # ── aid station
    s, j, _ = req("POST", "/api/aidstation/admin", {"op": "invite", "name": "Volunteer"}, tls=True, headers=A)
    scode = j.get("code") if isinstance(j, dict) else None
    t.check("admin makes an aid-staff code", s == 200 and scode, (s, j))
    s, j, _ = req("POST", "/api/aidstation/join", {"code": scode or "", "name": "Vol", "device": "phone"})
    skey = j.get("key") if isinstance(j, dict) else None
    t.check("volunteer joins with the staff code", s == 200 and skey, (s, j))
    S = {"X-Aid-Token": skey or ""}
    s, j, _ = req("POST", "/api/aidstation/status", {"open": True, "items": ["water", "first_aid"], "text": "Gym"}, headers=S)
    t.check("staff opens the station", s == 200, (s, j))
    s, j, _ = req("GET", "/api/aidstation/status")
    t.check("public sees station open", s == 200 and "open" in json.dumps(j), (s, j))
    s, j, _ = req("POST", "/api/aidstation/entry", {"person": "J. Doe", "count": 2, "help": ["water"]}, headers=S)
    t.check("staff logs a visit", s == 200, (s, j))
    s, j, _ = req("GET", "/api/aidstation/entries", headers=S)
    t.check("staff reads entries", s == 200 and "J. Doe" in json.dumps(j), (s, str(j)[:200]))
    s, j, _ = req("GET", "/api/aidstation/entries")
    t.check("entries refused to strangers", s in (401, 403), s)
    s, body, _ = req("GET", "/api/aidstation/export.csv", headers=S)
    t.check("staff CSV export", s == 200, s)
    # ── skills
    s, j, _ = req("POST", "/api/skills/add", {"kind": "offer", "category": "medical", "title": "Nurse", "name": "Ana",
                                               "contact": "Oak St", "availability": "evenings"})
    t.check("skills add", s == 200 and j.get("id") and j.get("token"), (s, j))
    if s == 200:
        s2, j2, _ = req("POST", "/api/skills/edit", {"id": j["id"], "token": j["token"], "title": "Nurse (ER)"})
        t.check("skills edit with its token", s2 == 200, (s2, j2))
        s2, j2, _ = req("POST", "/api/skills/edit", {"id": j["id"], "token": "nope", "title": "x"})
        t.check("skills edit refused with a wrong token", s2 == 403, s2)
    s, j, _ = req("GET", "/api/skills")
    t.check("skills list", s == 200 and "Nurse (ER)" in json.dumps(j), (s, str(j)[:200]))
    # ── missing person + photo
    s, j, _ = req("POST", "/api/missing/add", {"kind": "person", "name": "Ana Ruiz", "description": "8, red jacket",
                                                "where": "Elm St", "when": "14:00", "contact": "school gym"})
    mid, mtok = (j.get("id"), j.get("token")) if isinstance(j, dict) else (None, None)
    t.check("missing post", s == 200 and mid, (s, j))
    PH = {"Content-Type": "image/jpeg", "X-Badger-Upload": "1", "X-Edit-Token": mtok or ""}
    s, j, _ = req("POST", f"/api/missing/photo/{mid}", raw=jpeg(), headers=PH)
    t.check("missing photo upload", s == 200, (s, j))
    s, j, _ = req("GET", "/api/missing")
    photo = None
    for p in (j.get("posts") or []) if isinstance(j, dict) else []:
        if p.get("id") == mid:
            photo = p.get("photo")
    s, body, hd = req("GET", photo if photo and photo.startswith("/") else f"/api/missing/photo/{photo or mid}.jpg")
    t.check("missing photo served as image/jpeg", s == 200 and hd.get("content-type", "").startswith("image/jpeg"),
            (s, hd.get("content-type"), photo))
    s, j, _ = req("POST", f"/api/missing/photo/{mid}", raw=jpeg(pad=700000), headers=PH)
    t.check("1.4 MB missing photo refused (413)", s == 413, (s, j))
    s, j, _ = req("POST", f"/api/missing/photo/{mid}", raw=b"<svg onload=alert(1)>", headers=dict(PH, **{"Content-Type": "image/svg+xml"}))
    t.check("svg 'photo' refused", s in (400, 415), (s, j))
    s, j, _ = req("POST", f"/api/missing/photo/{mid}", raw=b"\xff\xd8garbage", headers=PH)
    t.check("broken jpeg refused", s in (400, 415), (s, j))
    # ── radio/sensor pages (no hardware: they must answer calmly)
    for path in ("/api/sensors", "/api/sensors/alerts", "/api/forecast", "/api/aircraft", "/api/satellites",
                 "/api/features", "/api/alerts", "/api/time", "/api/updates", "/api/settings"):
        s, j, _ = req("GET", path, headers=M)
        t.check(f"GET {path} → {s}", s == 200, (s, str(j)[:200]))
    for path in ("/features/sensors.js", "/features/forecast.js", "/features/aircraft.js", "/features/satellites.js",
                 "/features/vault.js", "/features/supplies.js", "/features/aidstation.js", "/features/skills.js",
                 "/features/missing.js", "/app.js", "/"):
        s, body, _ = req("GET", path)
        t.check(f"static {path}", s == 200, s)
    s, j, _ = req("POST", "/api/satellites/tle/refresh", {}, tls=True, headers=A)
    t.check("admin TLE refresh request answers", s in (200, 202, 409, 503), (s, j))
    s, j, _ = req("POST", "/api/aircraft/scan", {}, headers=M)
    t.check("aircraft scan without a dongle is accepted or refused calmly", s in (200, 202, 409, 503), (s, j))
    time.sleep(8)
    s, j, _ = req("GET", "/api/aircraft")
    msg = (j.get("radio") or {}).get("message") if isinstance(j, dict) else None
    print(f"INFO  aircraft page message with no dongle: {msg!r}")
    s, j, _ = req("POST", "/api/forecast/update", {}, tls=True, headers=A)
    t.check("forecast 'update now' answers calmly", s in (200, 202, 409, 429, 503), (s, j))
    st.update(member_key=key, admin=adm, staff=skey)
    json.dump(st, open(state_file, "w"))
    print(json.dumps({"p": t.p, "f": t.f, **{k: v for k, v in st.items() if k.startswith("vault_25")}}))


def flood(path, body_fn, n, conc):
    codes, lock = {}, threading.Lock()

    def one(i):
        try:
            s, _, _ = req("POST", path, body_fn(i), timeout=20)
        except Exception as e:  # noqa: BLE001
            s = type(e).__name__
        with lock:
            codes[s] = codes.get(s, 0) + 1
    ts = []
    for i in range(n):
        th = threading.Thread(target=one, args=(i,)); th.start(); ts.append(th)
        if len(ts) >= conc:
            ts.pop(0).join()
    [x.join() for x in ts]
    print(json.dumps({str(k): v for k, v in codes.items()}))


def logins(n, pin):
    codes, lock = {}, threading.Lock()

    def one(i):
        try:
            s, _, _ = req("POST", "/api/admin/login", {"pin": "%06d" % random.randint(0, 999999)}, timeout=60)
        except Exception as e:  # noqa: BLE001
            s = type(e).__name__
        with lock:
            codes[s] = codes.get(s, 0) + 1
    ts = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    [x.start() for x in ts]; [x.join() for x in ts]
    s, j, hd = req("POST", "/api/admin/login", {"pin": pin})
    print(json.dumps({"codes": {str(k): v for k, v in codes.items()}, "right_pin": s, "retry_after": hd.get("retry-after")}))


def slow(n, secs, mode, host="127.0.0.1", port=80):
    """mode headers: send a header line every 5 s; body: an /api/board POST trickling its body."""
    socks = []
    for _ in range(n):
        try:
            s = socket.create_connection((host, port), timeout=5)
            if mode == "headers":
                s.sendall(b"GET /api/status HTTP/1.1\r\nHost: badger.lan\r\n")
            else:
                s.sendall(b"POST /api/board HTTP/1.1\r\nHost: badger.lan\r\nContent-Type: application/json\r\n"
                          b"Content-Length: 100000\r\n\r\n{")
            socks.append(s)
        except OSError:
            break
    opened = len(socks)
    end = time.time() + secs
    while time.time() < end and socks:
        time.sleep(5)
        alive = []
        for s in socks:
            try:
                s.sendall(b"X-a: b\r\n" if mode == "headers" else b" ")
                alive.append(s)
            except OSError:
                s.close()
        socks = alive
    left = len(socks)
    for s in socks:
        s.close()
    print(json.dumps({"opened": opened, "alive_at_end": left}))


def malformed(host="127.0.0.1", port=80):
    cases = [
        b"\x00\x01\x02garbage\r\n\r\n",
        b"GET / HTTP/9.9\r\nHost: x\r\n\r\n",
        b"GET /" + b"a" * 20000 + b" HTTP/1.1\r\nHost: x\r\n\r\n",
        b"GET /api/status HTTP/1.1\r\nHost: x\r\n" + b"X-Big: " + b"b" * 70000 + b"\r\n\r\n",
        b"POST /api/board HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: -5\r\n\r\n",
        b"POST /api/board HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 5\r\nContent-Length: 7\r\n\r\n{}{}{}{",
        b"POST /api/board HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n5\r\n{\"a\":\r\n0\r\n\r\n",
        b"POST /api/board HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 6\r\n\r\n[[[[[[",
        b"POST /api/board HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 40001\r\n\r\n" + b"[" * 20000 + b"]" * 20000 + b"1",
        b"POST /api/missing/photo/abc HTTP/1.1\r\nHost: x\r\nContent-Type: image/jpeg\r\nX-Badger-Upload: 1\r\nContent-Length: abc\r\n\r\n",
        b"GET /api/files?path=%00%2e%2e%2f HTTP/1.1\r\nHost: x\r\n\r\n",
        b"GET /api/search?q=" + b"%F0%9F" * 2000 + b" HTTP/1.1\r\nHost: x\r\n\r\n",
        b"GET /api/time?lat=1e999&lon=nan HTTP/1.1\r\nHost: x\r\n\r\n",
        b"DELETE /api/board HTTP/1.1\r\nHost: x\r\n\r\n",
        b"BREW /api/status HTTP/1.1\r\nHost: x\r\n\r\n",
        b"GET /api/status HTTP/1.1\r\nHost: x\r\nContent-Length: 30\r\n\r\nGET /api/admin/x HTTP/1.1\r\n\r\n",
        b"GET /api/../../etc/passwd HTTP/1.1\r\nHost: x\r\n\r\n",
        b"GET /api/vault/file/..%2f..%2fmeta.json HTTP/1.1\r\nHost: x\r\n\r\n",
        b"POST /api/admin/login HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 1000000\r\n\r\n{\"pin\":\"" + b"9" * 999990 + b"\"}",
    ]
    out = []
    for raw in cases:
        try:
            s = socket.create_connection((host, port), timeout=15)
            s.sendall(raw)
            s.settimeout(4)
            data = b""
            while b"\r\n" not in data:
                try:
                    b = s.recv(65536)
                except socket.timeout:
                    break
                if not b:
                    break
                data += b
            s.close()
            line = data.split(b"\r\n", 1)[0].decode("latin1")[:60]
        except Exception as e:  # noqa: BLE001
            line = "EXC " + type(e).__name__
        out.append([raw[:50].decode("latin1"), line])
    print(json.dumps(out))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "load":
        load(int(sys.argv[2]), float(sys.argv[3]), sys.argv[4].split(","), sys.argv[5] == "keep")
    elif cmd == "features":
        features(sys.argv[2], sys.argv[3])
    elif cmd == "flood":
        kind, n, conc = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
        tag = secrets.token_hex(2)
        if kind == "board":
            flood("/api/board", lambda i: {"name": f"f{tag}", "text": f"flood {tag} {i}"}, n, conc)
        elif kind == "skills":
            flood("/api/skills/add", lambda i: {"kind": "offer", "category": "other", "title": f"t{tag}{i}",
                                                "name": "x", "contact": "y"}, n, conc)
        elif kind == "missing":
            flood("/api/missing/add", lambda i: {"kind": "person", "name": f"n{tag}{i}", "description": "d",
                                                 "where": "w", "when": "now", "contact": "c"}, n, conc)
    elif cmd == "logins":
        logins(int(sys.argv[2]), sys.argv[3])
    elif cmd == "slow":
        slow(int(sys.argv[2]), float(sys.argv[3]), sys.argv[4], *(sys.argv[5:6] or []))
    elif cmd == "malformed":
        malformed()
    elif cmd == "req":       # req METHOD PATH [json] [header=value …] — prints status and body
        hdrs = dict(a.split("=", 1) for a in sys.argv[5:])
        tls = hdrs.pop("tls", "") == "1"
        body = json.loads(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else None
        s, j, hd = req(sys.argv[2], sys.argv[3], body, tls=tls, headers=hdrs)
        print(s, json.dumps(j) if not isinstance(j, bytes) else len(j), hd.get("retry-after", ""))
PY
J() { python3 /tmp/hbp.py "$@"; }

# ───────────────────────────── phones ─────────────────────────────
phone_up() {  # phone_up N — wlanN into namespace phN, join, DHCP
  local n=$1 ns=ph$1 phy
  ip netns list | grep -qw "$ns" || ip netns add "$ns"
  phy=$(for p in /sys/class/ieee80211/*; do [ -e "$p/device/net/wlan$n" ] && basename "$p"; done)
  [ -n "$phy" ] && $IW phy "$phy" set netns name "$ns"
  mkdir -p "/etc/netns/$ns"; echo "nameserver $AP" > "/etc/netns/$ns/resolv.conf"
  ip netns exec "$ns" ip link set lo up; ip netns exec "$ns" ip link set "wlan$n" up
  printf 'ctrl_interface=/run/wpa_%s\nnetwork={\n ssid="%s"\n psk="%s"\n key_mgmt=WPA-PSK SAE\n ieee80211w=1\n}\n' "$ns" "$SSID" "$PSK" > "/tmp/$ns-wpa.conf"
  pkill -f "wpa_supplicant.*wlan$n " 2>/dev/null
  ip netns exec "$ns" wpa_supplicant -B -i "wlan$n" -c "/tmp/$ns-wpa.conf" -P "/run/wpa_$ns.pid" >/dev/null
}
phone_dhcp() {
  local n=$1 ns=ph$1
  ip netns exec "$ns" timeout 30 dhclient -1 -lf "/tmp/$ns.leases" -pf "/run/dhclient-$ns.pid" "wlan$n" >/dev/null 2>&1
}
phone_ip() { ip netns exec "ph$1" ip -4 -br a show "wlan$1" 2>/dev/null | awk '{print $3}' | cut -d/ -f1; }
phone_reset() {  # drop and re-join every phone with $PSK
  local n
  for n in "${PHONES[@]}"; do
    pkill -f "wpa_supplicant.*wlan$n " 2>/dev/null; ip netns exec "ph$n" dhclient -r -lf "/tmp/ph$n.leases" -pf "/run/dhclient-ph$n.pid" "wlan$n" >/dev/null 2>&1
  done
  for n in "${PHONES[@]}"; do phone_up "$n"; done
  sleep 8; for n in "${PHONES[@]}"; do phone_dhcp "$n" & done; wait
}

sec_phones() {
  echo "== many phones at once (${#PHONES[@]} plus vm_checks' phone)"
  local n t0 assoc=0 leases=0 dns=0 cap=0 web=0
  t0=$(date +%s%N)
  for n in "${PHONES[@]}"; do phone_up "$n"; done
  for _ in $(seq 1 40); do
    assoc=0; for n in "${PHONES[@]}"; do ip netns exec "ph$n" $IW dev "wlan$n" link 2>/dev/null | grep -q Connected && assoc=$((assoc + 1)); done
    [ "$assoc" = "${#PHONES[@]}" ] && break; sleep 1
  done
  info "association of ${#PHONES[@]} phones took $(( ($(date +%s%N) - t0) / 1000000 )) ms"
  [ "$assoc" = "${#PHONES[@]}" ] && ok "all ${#PHONES[@]} phones associated" || bad "only $assoc/${#PHONES[@]} phones associated"
  t0=$(date +%s%N)
  for n in "${PHONES[@]}"; do phone_dhcp "$n" & done; wait
  info "parallel DHCP took $(( ($(date +%s%N) - t0) / 1000000 )) ms"
  local ips=""
  for n in "${PHONES[@]}"; do
    ip4=$(phone_ip "$n"); [ -n "$ip4" ] && leases=$((leases + 1)) && ips="$ips $ip4"
    [ "$(ip netns exec "ph$n" dig +short +time=3 @"$AP" badger.lan A | tail -1)" = "$AP" ] && dns=$((dns + 1))
  done
  [ "$leases" = "${#PHONES[@]}" ] && ok "all phones got a DHCP lease:$ips" || bad "only $leases leases:$ips"
  [ "$(echo "$ips" | tr ' ' '\n' | grep -c .)" = "$(echo "$ips" | tr ' ' '\n' | grep . | sort -u | wc -l)" ] && ok "leases are unique" || bad "duplicate leases:$ips"
  [ "$dns" = "${#PHONES[@]}" ] && ok "badger.lan resolves on every phone" || bad "badger.lan resolved on $dns phones"
  for n in "${PHONES[@]}"; do
    (code=$(ip netns exec "ph$n" curl -s -o /dev/null -w '%{http_code}' --max-time 10 --resolve "captive.apple.com:80:$AP" http://captive.apple.com/hotspot-detect.html)
     web=$(ip netns exec "ph$n" curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://badger.lan/)
     echo "$code $web" > "$OUT/ph$n.cap") &
  done; wait
  cap=$(cat "$OUT"/ph*.cap | awk '$1==302' | wc -l); web=$(cat "$OUT"/ph*.cap | awk '$2==200' | wc -l)
  [ "$cap" = "${#PHONES[@]}" ] && ok "captive probe 302 on every phone at once" || bad "captive probe ok on $cap phones"
  [ "$web" = "${#PHONES[@]}" ] && ok "launcher 200 on every phone at once" || bad "launcher ok on $web phones"
  local cl; cl=$(curl -s http://127.0.0.1/api/status | python3 -c 'import json,sys;print(json.load(sys.stdin).get("clients"))' 2>/dev/null)
  [ "${cl:-0}" -ge "${#PHONES[@]}" ] && ok "/api/status counts $cl clients" || bad "/api/status counts '$cl' clients"
  $IW dev "$(. /etc/default/prepper 2>/dev/null; echo "${AP_IFACE:-wlan1}")" station dump | grep -c Station | xargs -I{} echo "INFO  hostapd stations: {}"
}

# ───────────────────────────── load ─────────────────────────────
PATHS="/,/api/status,/api/library,/api/search?q=water,/api/search?q=first%20aid,/api/board,/api/features,/api/skills,/api/missing,/api/aidstation/status,/api/forecast,/api/satellites,/api/aircraft,/app.js"
agg() {  # agg FILES… — merge load results
  python3 - "$@" <<'PY'
import json, sys
lat, codes, errs, n, secs = [], {}, {}, 0, 0
for f in sys.argv[1:]:
    try:
        d = json.load(open(f))
    except Exception:
        continue
    lat += d["lat"]; n += d["n"]; secs = max(secs, d["secs"])
    for k, v in d["codes"].items(): codes[k] = codes.get(k, 0) + v
    for k, v in d["errors"].items(): errs[k] = errs.get(k, 0) + v
lat.sort()
p = lambda q: round(lat[min(len(lat) - 1, int(round(q / 100 * (len(lat) - 1))))] * 1000, 1) if lat else None
bad = sum(v for k, v in codes.items() if int(k) >= 500) + sum(errs.values())
print(json.dumps({"n": n, "rps": round(n / max(secs, 0.1), 1), "p50": p(50), "p95": p(95), "p99": p(99), "max": p(100),
                  "codes": codes, "errors": errs, "err_rate": round(bad / max(1, n + sum(errs.values())), 4)}))
PY
}
sec_load() {
  echo "== HTTP load through nginx"
  local pid r0 c0 t0 since res conc
  pid=$(portal_pid); since=$(date '+%Y-%m-%d %H:%M:%S')
  r0=$(rss_kb "$pid"); info "portal before: RSS ${r0} kB, threads $(threads "$pid")"
  J load 1 10 "/api/status" keep > "$OUT/base.json"; info "baseline 1 client /api/status: $(python3 -c 'import json;d=json.load(open("'"$OUT"'/base.json"));print("p50",d["p50"],"ms p95",d["p95"],"ms",d["rps"],"req/s")')"
  for conc in 2 8; do
    c0=$(cpu_ticks "$pid"); t0=$(date +%s)
    rm -f "$OUT"/load-*.json
    for n in "${PHONES[@]}"; do ip netns exec "ph$n" python3 /tmp/hbp.py load "$conc" 30 "$PATHS" new > "$OUT/load-$n.json" & done
    local peak=0; for _ in $(seq 1 15); do sleep 2; r=$(rss_kb "$pid"); [ "${r:-0}" -gt "$peak" ] && peak=$r; done
    wait
    res=$(agg "$OUT"/load-*.json)
    local cpu; cpu=$(( ( $(cpu_ticks "$pid") - c0 ) * 100 / ( $(getconf CLK_TCK) * ( $(date +%s) - t0 ) ) ))
    info "load ${#PHONES[@]} phones × $conc conns × 30 s (new conn each): $res; portal peak RSS ${peak} kB, threads now $(threads "$pid"), portal CPU ${cpu}% of one core"
    echo "$res" > "$OUT/load-summary-$conc.json"
    er=$(echo "$res" | python3 -c 'import json,sys;print(json.load(sys.stdin)["err_rate"])')
    python3 -c "import sys;sys.exit(0 if $er < 0.01 else 1)" && ok "error rate ${er} under load ($((${#PHONES[@]} * conc)) clients)" || bad "error rate ${er} under load ($((${#PHONES[@]} * conc)) clients)"
  done
  # keep-alive hammer of the cheapest and the dearest endpoints from the VM itself
  J load 32 20 "/api/status" keep > "$OUT/ka-status.json"; info "32 keep-alive clients /api/status: $(agg "$OUT/ka-status.json")"
  J load 16 20 "/api/search?q=water,/api/library" keep > "$OUT/ka-search.json"; info "16 keep-alive clients search+library: $(agg "$OUT/ka-search.json")"
  sleep 5
  [ "$(portal_pid)" = "$pid" ] && ok "portal survived the load without a restart" || bad "portal restarted during load (pid $pid → $(portal_pid))"
  J load 1 10 "/api/status" keep > "$OUT/after.json"
  info "after load 1 client /api/status: $(python3 -c 'import json;d=json.load(open("'"$OUT"'/after.json"));print("p50",d["p50"],"ms p95",d["p95"],"ms")'); portal RSS $(rss_kb "$pid") kB (was $r0), threads $(threads "$pid")"
  local tb; tb=$(tracebacks "$since"); [ "$tb" = 0 ] && ok "no portal tracebacks during load" || bad "$tb portal tracebacks during load"
}

# ───────────────────────────── features ─────────────────────────────
sec_features() {
  echo "== feature smoke (member + admin through nginx, from phone ph3)"
  local since; since=$(date '+%Y-%m-%d %H:%M:%S')
  ip netns exec ph3 python3 /tmp/hbp.py features "$PIN" "$OUT/feature-state.json" | tee "$OUT/features.txt" | grep -v '^{'
  pass=$((pass + $(grep -c '^PASS' "$OUT/features.txt"))); fail=$((fail + $(grep -c '^FAIL' "$OUT/features.txt")))
  tail -1 "$OUT/features.txt" | grep -q vault_25 && info "25 MB vault upload: $(tail -1 "$OUT/features.txt")"
  local perm; perm=$(find /var/lib/prepper/portal -type f -perm /o+r | head -5)
  [ -z "$perm" ] && ok "no world-readable files under /var/lib/prepper/portal" || bad "world-readable: $perm"
  find /var/lib/prepper/portal/features/vault -type f -newer "$OUT/features.txt" 2>/dev/null | head -1 >/dev/null
  local tb; tb=$(tracebacks "$since"); [ "$tb" = 0 ] && ok "no portal tracebacks during the feature smoke" || bad "$tb portal tracebacks during the feature smoke"
}

# ───────────────────────────── abuse ─────────────────────────────
sec_abuse() {
  echo "== abuse"
  local since pid res; since=$(date '+%Y-%m-%d %H:%M:%S'); pid=$(portal_pid)
  local BF; BF=$(find /var/lib/prepper/portal -maxdepth 2 -name 'board*.jsonl' | head -1)
  local bsize0; bsize0=$(stat -c %s "$BF" 2>/dev/null || echo 0)
  res=$(ip netns exec ph4 python3 /tmp/hbp.py flood board 200 20)
  info "board flood, one phone, 200 posts / 20 parallel: $res"
  echo "$res" | python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d.get("200",0)<=2 else 1)' && ok "per-IP board rate limit holds under a parallel flood" || bad "board flood got through: $res"
  for n in "${PHONES[@]}"; do ip netns exec "ph$n" python3 /tmp/hbp.py flood board 30 10 > "$OUT/bf-$n.json" & done; wait
  info "board flood from ${#PHONES[@]} phones: $(cat "$OUT"/bf-*.json | tr '\n' ' ')"
  info "$BF grew $bsize0 → $(stat -c %s "$BF" 2>/dev/null) bytes"
  res=$(ip netns exec ph5 python3 /tmp/hbp.py flood skills 60 10); info "skills flood, one phone: $res"
  echo "$res" | python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d.get("200",0)<=10 else 1)' && ok "skills per-IP cap holds" || bad "skills flood: $res"
  res=$(ip netns exec ph6 python3 /tmp/hbp.py flood missing 60 10); info "missing flood, one phone: $res"
  echo "$res" | python3 -c 'import json,sys;d=json.load(sys.stdin);sys.exit(0 if d.get("200",0)<=10 else 1)' && ok "missing per-IP cap holds" || bad "missing flood: $res"
  # oversized raw bodies through nginx (no member needed to be refused early)
  head -c $((30 * 1024 * 1024)) /dev/zero > /tmp/30m.bin
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -H 'X-Badger-Upload: 1' -H 'Content-Type: application/pdf' --data-binary @/tmp/30m.bin http://badger.lan/api/vault/upload/x)
  [ "$code" = 413 ] && ok "30 MB raw upload stopped by nginx (413)" || bad "30 MB raw upload answered $code"
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -H 'X-Badger-Upload: 1' -H 'Content-Type: image/jpeg' --data-binary @/tmp/30m.bin http://badger.lan/api/missing/photo/abcdefghij)
  [ "$code" = 413 ] && ok "30 MB missing photo stopped (413)" || bad "30 MB missing photo answered $code"
  head -c $((20 * 1024 * 1024)) /dev/zero > /tmp/20m.bin
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -H 'X-Badger-Upload: 1' -H 'Content-Type: image/jpeg' --data-binary @/tmp/20m.bin http://badger.lan/api/missing/photo/abcdefghij)
  [ "$code" = 413 ] || [ "$code" = 404 ] && ok "20 MB missing photo to a bad id refused ($code)" || bad "20 MB missing photo answered $code"
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 -H 'Content-Type: application/json' --data-binary @/tmp/20m.bin http://badger.lan/api/board)
  [ "$code" = 413 ] && ok "20 MB JSON body refused (413)" || bad "20 MB JSON body answered $code"
  rm -f /tmp/30m.bin /tmp/20m.bin
  # slow clients
  local t0 lat
  ip netns exec ph7 python3 /tmp/hbp.py slow 300 40 headers badger.lan > "$OUT/slow-h.json" &
  ip netns exec ph8 python3 /tmp/hbp.py slow 150 70 body badger.lan > "$OUT/slow-b.json" &
  sleep 20
  lat=$(ip netns exec ph9 curl -s -o /dev/null -w '%{http_code} %{time_total}' --max-time 15 http://badger.lan/api/status)
  info "during 300 slow-header + 150 slow-body clients: /api/status → $lat; portal threads $(threads "$pid"); nginx conns $(ss -Htn state established '( sport = :80 )' | wc -l)"
  [ "${lat%% *}" = 200 ] && ok "portal answers others while slow clients hang on" || bad "portal unreachable during slow clients: $lat"
  wait
  info "slow-header clients: $(cat "$OUT/slow-h.json") (nginx client_header_timeout should cut them)"
  info "slow-body clients: $(cat "$OUT/slow-b.json") (each holds a portal thread while alive)"
  python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));sys.exit(0 if d["alive_at_end"]==0 else 1)' "$OUT/slow-h.json" && ok "slow header senders are dropped" || bad "slow header senders kept their connections: $(cat "$OUT/slow-h.json")"
  python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));sys.exit(0 if d["alive_at_end"]==0 else 1)' "$OUT/slow-b.json" && ok "slow body senders are dropped" || bad "slow body trickle (1 byte / 5 s) keeps connections + portal threads for 70 s+: $(cat "$OUT/slow-b.json")"
  sleep 3; info "portal threads after slow clients: $(threads "$pid")"
  # parallel wrong PINs from eight phones at once (ph3, which logged in during the feature smoke, stays out)
  for n in "${PHONES[@]:1}"; do ip netns exec "ph$n" python3 /tmp/hbp.py logins 12 "$PIN" > "$OUT/login-$n.json" & done; wait
  info "12 parallel wrong PINs from each of $(( ${#PHONES[@]} - 1 )) phones: $(cat "$OUT"/login-*.json | tr '\n' ' ')"
  python3 - "$OUT"/login-*.json <<'PY' && ok "lockout holds: ≤5 guesses per phone in a 12-wide burst (3 + the 1 s/2 s back-off windows), right PIN refused while locked" || bad "PIN lockout leaked under parallel load"
import json, sys
okk = True
for f in sys.argv[1:]:
    d = json.load(open(f)); c = d["codes"]
    if c.get("403", 0) > 5 or d["right_pin"] == 200:
        okk = False; print("   ", f, d)
sys.exit(0 if okk else 1)
PY
  local n502; n502=$(cat "$OUT"/login-*.json | python3 -c 'import json,sys;print(sum(json.loads(l)["codes"].get("502",0) for l in sys.stdin))')
  [ "$n502" = 0 ] && ok "no 502s during the PIN burst" || bad "$n502 of $(( (${#PHONES[@]} - 1) * 12 )) parallel logins got 502 Bad Gateway (portal reset the upstream connection)"
  code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -H "X-Badger-Panel: $(cat /etc/prepper/panel.key)" -d "{\"pin\":\"$PIN\"}" http://127.0.0.1:8000/api/admin/login)
  [ "$code" = 200 ] && ok "the front panel (panel key) can still log in during the global brake" || bad "panel-key login during the brake answered $code"
  code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d "{\"pin\":\"$PIN\"}" http://127.0.0.1:8000/api/admin/login)
  info "a root shell on the Badger without the panel key, during the brake: $code"
  res=$(ip netns exec ph3 python3 /tmp/hbp.py req POST /api/admin/login "{\"pin\":\"$PIN\"}")
  case "$res" in 200*) ok "a phone that logged in before (trusted) skips the global brake" ;; *) bad "trusted phone refused during the brake: $res" ;; esac
  # many more slow bodies: what does a single attacker cost?
  local p0 r0; p0=$(portal_pid); r0=$(rss_kb "$p0")
  for n in 4 5 6 7; do ip netns exec "ph$n" python3 /tmp/hbp.py slow 250 45 body badger.lan > "$OUT/slow-b-$n.json" & done
  sleep 25
  lat=$(ip netns exec ph9 curl -s -o /dev/null -w '%{http_code} %{time_total}' --max-time 15 http://badger.lan/api/status)
  info "1000 slow-body clients: portal threads $(threads "$p0"), RSS $(rss_kb "$p0") kB (was $r0), /api/status → $lat, nginx conns $(ss -Htn state established '( sport = :80 )' | wc -l)"
  wait
  info "1000 slow-body results: $(cat "$OUT"/slow-b-*.json | tr '\n' ' ')"
  # malformed requests through nginx and straight to the portal
  J malformed > "$OUT/malformed-nginx.json"
  python3 - "$OUT/malformed-nginx.json" <<'PY'
import json, sys
for raw, line in json.load(open(sys.argv[1])):
    print(f"INFO  nginx: {raw[:38]!r:44} → {line}")
PY
  python3 -c "
import sys; sys.argv=['x']; exec(open('/tmp/hbp.py').read().split('if __name__')[0]); malformed('127.0.0.1', 8000)" > "$OUT/malformed-portal.json"
  python3 - "$OUT/malformed-portal.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1])); bad = 0
for raw, line in d:
    flag = " 5" in line[8:12] or line.startswith("EXC")
    bad += flag
    print(f"{'WARN' if flag else 'INFO'}  portal: {raw[:38]!r:44} → {line}")
PY
  sleep 2
  [ "$(portal_pid)" = "$pid" ] && ok "portal survived all abuse without a restart" || bad "portal restarted during abuse"
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://badger.lan/api/status)" = 200 ] && ok "portal healthy after abuse" || bad "portal unhealthy after abuse"
  local tb; tb=$(tracebacks "$since"); [ "$tb" = 0 ] && ok "no portal tracebacks during abuse" || bad "$tb portal tracebacks during abuse: $(journalctl -u prepper-portal --since "$since" --no-pager | grep -A1 'error handling' | head -6 | tr '\n' '|')"
  info "portal RSS after abuse $(rss_kb "$pid") kB, threads $(threads "$pid")"
}

# ───────────────────────────── mesh ─────────────────────────────
sec_mesh() {
  echo "== mesh"
  local py; py=$(command -v python3); info "python: $($py --version)"
  if (cd /opt/prepper && timeout 600 "$py" tests/test_mesh_courtesy.py > "$OUT/mesh-courtesy.txt" 2>&1); then
    ok "tests/test_mesh_courtesy.py: $(grep -E '^Ran|^OK' "$OUT/mesh-courtesy.txt" | tr '\n' ' ')"
  else
    bad "tests/test_mesh_courtesy.py: $(tail -5 "$OUT/mesh-courtesy.txt" | tr '\n' ' ')"
  fi
  if (cd /opt/prepper && timeout 600 /opt/prepper/venv/bin/python tests/test_mesh_courtesy.py > "$OUT/mesh-courtesy-venv.txt" 2>&1); then
    ok "test_mesh_courtesy.py with the venv python (real meshtastic package): $(grep -E '^Ran' "$OUT/mesh-courtesy-venv.txt")"
  else
    bad "test_mesh_courtesy.py with the venv python: $(tail -5 "$OUT/mesh-courtesy-venv.txt" | tr '\n' ' ')"
  fi
  local since n0 n1; since=$(date '+%Y-%m-%d %H:%M:%S')
  n0=$(systemctl show -p NRestarts --value prepper-meshbridge)
  systemctl restart prepper-meshbridge; sleep 40
  n1=$(systemctl show -p NRestarts --value prepper-meshbridge)
  info "meshbridge after restart+40 s: ActiveState=$(systemctl show -p ActiveState --value prepper-meshbridge) SubState=$(systemctl show -p SubState --value prepper-meshbridge) Result=$(systemctl show -p Result --value prepper-meshbridge) NRestarts $n0→$n1"
  [ "$((n1 - n0))" -le 1 ] && ok "meshbridge not restart-looping with no radio" || bad "meshbridge restarted $((n1 - n0)) times in 40 s with no radio (Restart=always)"
  journalctl -u prepper-meshbridge --since "$since" --no-pager | grep -q Traceback && bad "meshbridge traceback without radio" || ok "meshbridge: no traceback without radio"
  if ! compgen -G "/dev/ttyACM*" >/dev/null && ! compgen -G "/dev/ttyUSB*" >/dev/null; then
    if journalctl -u prepper-meshbridge --since "$since" --no-pager | grep -q "meshbridge: connected"; then
      bad "meshbridge claims 'connected' with no radio plugged in: $(journalctl -u prepper-meshbridge --since "$since" --no-pager -o cat | grep -m1 'object has no attribute')"
    else ok "meshbridge waits for a radio instead of claiming a connection"; fi
    curl -s http://127.0.0.1/api/status | python3 -c 'import json,sys;sys.exit(0 if json.load(sys.stdin)["services"].get("mesh") else 1)' \
      && bad "/api/status reports the mesh as up with no radio" || ok "/api/status reports the mesh down with no radio"
  fi
  local t0; t0=$(date +%s%N); systemctl stop prepper-meshbridge
  info "meshbridge stop took $(( ($(date +%s%N) - t0) / 1000000 )) ms; Result=$(systemctl show -p Result --value prepper-meshbridge)"
  [ "$(systemctl show -p Result --value prepper-meshbridge)" = success ] && ok "meshbridge stops cleanly" || bad "meshbridge stop result: $(systemctl show -p Result --value prepper-meshbridge)"
  journalctl -u prepper-meshbridge --since "$since" --no-pager | tail -5 | sed 's/^/      /'
  systemctl start prepper-meshbridge
}

# ───────────────────────────── power ─────────────────────────────
svc_back() {  # svc_back UNIT SECONDS — active again with a new PID
  local i; for i in $(seq 1 "$2"); do
    systemctl is-active --quiet "$1" && [ "$(systemctl show -p MainPID --value "$1")" != 0 ] && return 0; sleep 1
  done; return 1
}
sec_power() {
  echo "== power events"
  local u pid t0
  for u in prepper-portal nginx dnsmasq prepper-hostapd prepper-powerd prepper-sensord prepper-sdr prepper-meshbridge chrony gpsd; do
    systemctl is-active --quiet "$u" || { info "$u not active before kill ($(systemctl show -p ActiveState,SubState,Result --value "$u" | tr '\n' ' ')) — skipped"; continue; }
    pid=$(systemctl show -p MainPID --value "$u"); [ "$pid" != 0 ] || { info "$u has no main pid"; continue; }
    t0=$(date +%s); kill -9 "$pid"; sleep 1
    if svc_back "$u" 45 && [ "$(systemctl show -p MainPID --value "$u")" != "$pid" ]; then
      ok "kill -9 $u → back in $(( $(date +%s) - t0 )) s"
    else
      bad "kill -9 $u → not back after 45 s ($(systemctl show -p ActiveState,SubState,Result --value "$u" | tr '\n' ' ')) — no Restart= on the unit"
      systemctl reset-failed "$u"; systemctl start "$u"          # put it back so the next checks mean something
    fi
  done
  wait_http http://badger.lan/api/status 30 && ok "launcher answers after the kills" || bad "launcher down after the kills"
  pgrep -x hostapd >/dev/null && ok "hostapd running after kills" || bad "hostapd gone after kill"
  # crash storm: kill the portal each time it comes back

  for _ in $(seq 1 8); do
    svc_back prepper-portal 20 || break
    kill -9 "$(portal_pid)" 2>/dev/null; sleep 0.5
  done
  sleep 1
  if svc_back prepper-portal 60 && wait_http http://127.0.0.1:8000/api/status 30 && wait_http http://badger.lan/api/status 30; then ok "portal recovers from a crash storm (8 kills, restart counter $(systemctl show -p NRestarts --value prepper-portal))"
  else bad "portal did NOT recover from a crash storm of 8 kill -9 ($(systemctl show -p ActiveState,SubState,Result --value prepper-portal | tr '\n' ' ')): $(journalctl -u prepper-portal -n 3 --no-pager | tr '\n' '|')"
    systemctl reset-failed prepper-portal; systemctl start prepper-portal; sleep 3
  fi
  # restart storm: an operator (or the admin page) restarting quickly
  for _ in $(seq 1 10); do systemctl restart prepper-portal nginx 2>/dev/null; done
  sleep 3
  if systemctl is-active --quiet prepper-portal && systemctl is-active --quiet nginx && wait_http http://badger.lan/api/status 20; then ok "portal+nginx survive 10 quick restarts"
  else bad "after 10 quick restarts: portal $(systemctl is-active prepper-portal) nginx $(systemctl is-active nginx)"
    systemctl reset-failed prepper-portal nginx; systemctl start prepper-portal nginx
  fi
  for _ in $(seq 1 6); do systemctl restart prepper-net 2>/dev/null; done; sleep 10
  for _ in $(seq 1 30); do pgrep -x hostapd >/dev/null && break; sleep 1; done
  pgrep -x hostapd >/dev/null && ok "hostapd back after 6 prepper-net restarts" || bad "hostapd not running after prepper-net restarts ($(systemctl is-active prepper-net))"
  sleep 10; local back=0
  for n in "${PHONES[@]}"; do ip netns exec "ph$n" $IW dev "wlan$n" link 2>/dev/null | grep -q Connected && back=$((back + 1)); done
  info "$back/${#PHONES[@]} phones re-associated by themselves after the AP restarts"
  [ -z "$(systemctl --failed --no-legend)" ] && ok "no failed units after power events" || bad "failed after power events: $(systemctl --failed --no-legend | awk '{print $2}' | tr '\n' ' ')"
}

# ───────────────────────────── disk full ─────────────────────────────
sec_diskfull() {
  echo "== disk full: portal data folder"
  local P=/var/lib/prepper/portal IMG=/var/tmp/hb-full.img since
  since=$(date '+%Y-%m-%d %H:%M:%S')
  mountpoint -q "$P" && umount "$P"
  systemctl stop prepper-portal prepper-sensord prepper-meshbridge
  rm -f "$IMG"; truncate -s 96M "$IMG"; mkfs.ext4 -q -F "$IMG"
  mkdir -p /mnt/hbfull; mount -o loop "$IMG" /mnt/hbfull; rsync -a "$P/" /mnt/hbfull/; umount /mnt/hbfull
  mount -o loop "$IMG" "$P"
  systemctl start prepper-portal prepper-sensord prepper-meshbridge
  wait_http http://badger.lan/api/status 30 || bad "portal didn't start on the loop data dir"
  # member key from the feature smoke (members.json was copied along)
  local MK; MK=$(python3 -c 'import json;print(json.load(open("'"$OUT"'/feature-state.json"))["member_key"])' 2>/dev/null)
  dd if=/dev/zero of="$P/zz-filler" bs=1M 2>/dev/null; sync
  for i in $(seq 1 2000); do echo x > "$P/zz-f$i" 2>/dev/null || break; done
  info "data dir: $(df -h "$P" | tail -1 | awk '{print $3" used, "$4" free"}')"
  local r
  r=$(ip netns exec ph10 python3 /tmp/hbp.py req POST /api/board '{"name":"full","text":"disk full post"}'); info "board post on a full disk: $r"
  case "$r" in
    5*|4*) ok "board post on a full disk refused cleanly (${r%% *})" ;;
    200*) curl -s http://badger.lan/api/board | grep -q "disk full post" && ok "board post on a full disk really stored (fit in the file's last block)" || bad "board post claims success on a full disk but is lost: $r" ;;
    *) bad "board post on a full disk: $r" ;;
  esac
  r=$(ip netns exec ph10 python3 /tmp/hbp.py req POST /api/skills/add '{"kind":"offer","category":"other","title":"full","name":"x","contact":"y"}'); info "skills add on a full disk: $r"
  case "$r" in 200*) bad "skills add claims success on a full disk" ;; *) ok "skills add refused on a full disk (${r%% *})" ;; esac
  r=$(ip netns exec ph11 python3 /tmp/hbp.py req POST /api/missing/add '{"kind":"person","name":"F","description":"d","where":"w","when":"n","contact":"c"}'); info "missing add on a full disk: $r"
  case "$r" in 200*) bad "missing add claims success on a full disk" ;; *) ok "missing add refused on a full disk (${r%% *})" ;; esac
  if [ -n "$MK" ]; then
    r=$(python3 /tmp/hbp.py req POST /api/supplies/item '{"name":"Rice","category":"food","qty":5,"unit":"kg"}' "X-Member-Token=$MK"); info "supplies add on a full disk: $r"
    case "$r" in 200*) bad "supplies add claims success on a full disk" ;; *) ok "supplies add refused on a full disk (${r%% *})" ;; esac
    VP=$(python3 -c 'import json;print(json.load(open("'"$OUT"'/feature-state.json")).get("vault_pw") or "")' 2>/dev/null)
    if [ -n "$VP" ]; then
      python3 /tmp/hbp.py req POST /api/vault/unlock "{\"passphrase\":\"$VP\"}" "X-Member-Token=$MK" >/dev/null
      r=$(MK=$MK python3 -c '
import os, sys; exec(open("/tmp/hbp.py").read().split("if __name__")[0])
M={"X-Member-Token": os.environ["MK"]}
d=b"%PDF-1.4\n"+os.urandom(4*1024*1024)
s,j,_=req("POST","/api/vault/upload-start",{"size":len(d),"name":"full.pdf"},headers=M)
if s!=200: print("start",s,j); raise SystemExit
s,j,_=req("POST","/api/vault/upload/"+j["upload"],raw=d,headers=dict(M,**{"Content-Type":"application/pdf","X-Badger-Upload":"1"}),timeout=60)
print(s,j)')
      info "vault 4 MB upload on a full disk: $r"
      case "$r" in 200*) bad "vault upload claims success on a full disk" ;; *) ok "vault upload refused on a full disk (${r%% *})" ;; esac
      compgen -G "$P/features/vault/blobs/.up-*" >/dev/null && bad "vault left a partial .up- temp file" || ok "vault left no partial temp file"
    fi
  fi
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://badger.lan/api/status)" = 200 ] && ok "launcher still answers on a full disk" || bad "launcher down on a full disk"
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://badger.lan/api/board)" = 200 ] && ok "board still readable on a full disk" || bad "board unreadable on a full disk"
  systemctl is-active --quiet prepper-portal && ok "portal still running on a full disk" || bad "portal died on a full disk"
  # recovery
  rm -f "$P"/zz-*; sync
  r=$(ip netns exec ph9 python3 /tmp/hbp.py req POST /api/board '{"name":"back","text":"space again"}'); info "board post after freeing space: $r"
  case "$r" in 200*) ok "board posts work again after freeing space" ;; *) bad "board post after freeing space: $r" ;; esac
  r=$(ip netns exec ph9 python3 /tmp/hbp.py req POST /api/skills/add '{"kind":"offer","category":"other","title":"after","name":"x","contact":"y"}')
  case "$r" in 200*) ok "skills add works again" ;; *) bad "skills add after freeing space: $r" ;; esac
  if [ -n "$MK" ]; then
    r=$(python3 /tmp/hbp.py req POST /api/supplies/item '{"name":"Beans","category":"food","qty":5,"unit":"cans"}' "X-Member-Token=$MK")
    case "$r" in 200*) ok "supplies add works again" ;; *) bad "supplies add after freeing space: $r" ;; esac
    [ -n "${VP:-}" ] && python3 /tmp/hbp.py req POST /api/vault/unlock "{\"passphrase\":\"$VP\"}" "X-Member-Token=$MK" >/dev/null
    r=$(python3 /tmp/hbp.py req POST /api/vault/list '{}' "X-Member-Token=$MK"); case "$r" in 200*|423*) ok "vault index readable after the full disk" ;; *) bad "vault list after full disk: ${r:0:200}" ;; esac
  fi
  local badj; badj=$(python3 - "$P" <<'PY'
import json, os, sys
bad = []
for root, _, files in os.walk(sys.argv[1]):
    for f in files:
        p = os.path.join(root, f)
        try:
            if f.endswith(".json"):
                json.load(open(p))
            elif f.endswith(".jsonl"):
                for ln in open(p):
                    ln.strip() and json.loads(ln)
        except Exception as e:
            bad.append(f"{p}: {type(e).__name__}")
print(" ".join(bad))
PY
)
  [ -z "$badj" ] && ok "every JSON/JSONL file in the data dir still parses" || bad "damaged files after disk-full: $badj"
  local tb; tb=$(tracebacks "$since"); info "portal tracebacks during disk-full: $tb"
  journalctl -u prepper-portal --since "$since" --no-pager | grep -A2 "error handling" | grep -v Traceback | head -8 | sed 's/^/      /'
  systemctl stop prepper-portal prepper-sensord prepper-meshbridge
  # keep what was written meanwhile: copy back, then unmount
  rsync -a "$P/" /mnt/hbfull.keep/ 2>/dev/null || { mkdir -p /mnt/hbfull.keep; rsync -a "$P/" /mnt/hbfull.keep/; }
  umount "$P"; rsync -a /mnt/hbfull.keep/ "$P/"; rm -rf /mnt/hbfull.keep "$IMG"
  systemctl start prepper-portal prepper-sensord prepper-meshbridge
  wait_http http://badger.lan/api/status 30 && ok "portal back on the real data dir" || bad "portal not back after disk-full test"

  echo "== disk full: updater report folder"
  local R=/var/lib/prepper/reports
  mkdir -p "$R"; truncate -s 8M "$IMG"; mkfs.ext4 -q -F "$IMG"
  mkdir -p /mnt/hbr; mount -o loop "$IMG" /mnt/hbr; rsync -a "$R/" /mnt/hbr/; umount /mnt/hbr
  local st0; st0=$(sha256sum /var/lib/prepper/upd-state.json 2>/dev/null | cut -c1-12)
  mount -o loop "$IMG" "$R"; dd if=/dev/zero of="$R/zz-filler" bs=1M 2>/dev/null
  timeout 300 python3 /opt/prepper/software/upd/upd.py check > "$OUT/upd-full.txt" 2>&1; local rc=$?
  info "upd.py check with a full report dir: exit $rc; $(tail -3 "$OUT/upd-full.txt" | tr '\n' '|' | cut -c1-300)"
  grep -q Traceback "$OUT/upd-full.txt" && bad "updater traceback on a full disk: $(grep -A3 Traceback "$OUT/upd-full.txt" | tail -2 | tr '\n' ' ')" || ok "updater fails without a traceback on a full disk"
  rm -f "$R/zz-filler"
  timeout 300 python3 /opt/prepper/software/upd/upd.py check > "$OUT/upd-ok.txt" 2>&1; rc=$?
  info "upd.py check after freeing space: exit $rc"
  [ "$rc" = 0 ] || [ "$rc" = 75 ] && ok "updater check works again after freeing space (exit $rc)" || bad "updater check after freeing space: exit $rc $(tail -2 "$OUT/upd-ok.txt" | tr '\n' ' ')"
  python3 -c 'import json;json.load(open("/var/lib/prepper/reports/last-check.json"))' 2>/dev/null && ok "last-check.json valid" || bad "last-check.json damaged or missing"
  python3 -c 'import json;json.load(open("/var/lib/prepper/upd-state.json"))' 2>/dev/null && ok "upd-state.json valid ($st0)" || info "upd-state.json absent or damaged"
  rsync -a "$R/" /var/tmp/hbr.keep/; umount "$R"; rsync -a /var/tmp/hbr.keep/ "$R/"; rm -rf /var/tmp/hbr.keep "$IMG"
}

# ───────────────────────────── clock ─────────────────────────────
sec_clock() {
  echo "== clock jumps"
  local T0 S0 r tok code MK
  systemctl stop chrony 2>/dev/null; systemctl stop systemd-timesyncd 2>/dev/null
  S0=$(date +%s); T0=$S0
  MK=$(python3 -c 'import json;print(json.load(open("'"$OUT"'/feature-state.json"))["member_key"])' 2>/dev/null)
  VP=$(python3 -c 'import json;print(json.load(open("'"$OUT"'/feature-state.json")).get("vault_pw") or "")' 2>/dev/null)
  restore() { date -s "@$(( T0 + $(cut -d. -f1 /proc/uptime) - U0 ))" >/dev/null; }
  U0=$(cut -d. -f1 /proc/uptime)
  tok=$(curl -s -H 'Content-Type: application/json' -d "{\"pin\":\"$PIN\"}" http://127.0.0.1:8000/api/admin/login | python3 -c 'import json,sys;print(json.load(sys.stdin).get("token",""))')
  [ -n "$tok" ] || bad "clock: could not log in locally"
  # 1. forward 2 h: admin session (12 h) must survive; vault (15 min idle) must lock
  [ -n "$VP" ] && python3 /tmp/hbp.py req POST /api/vault/unlock "{\"passphrase\":\"$VP\"}" "X-Member-Token=$MK" >/dev/null
  date -s "@$((S0 + 7200))" >/dev/null
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "X-Admin-Token: $tok" http://127.0.0.1:8000/api/sensors/admin)
  [ "$code" = 200 ] && ok "admin session survives +2 h" || bad "admin session after +2 h: $code"
  r=$(python3 /tmp/hbp.py req GET /api/vault/status "" "X-Member-Token=$MK"); info "vault after +2 h: ${r:0:200}"
  echo "$r" | grep -q '"unlocked": true' && bad "vault still unlocked after +2 h idle" || ok "vault locked itself after +2 h"
  [ "$(python3 /tmp/hbp.py req GET /api/members/me "" "X-Member-Token=$MK" | cut -d' ' -f1)" = 200 ] && ok "member key valid after +2 h" || bad "member key invalid after +2 h"
  # 2. forward 13 h: admin session should end
  date -s "@$((S0 + 13 * 3600))" >/dev/null
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "X-Admin-Token: $tok" http://127.0.0.1:8000/api/sensors/admin)
  info "admin session after +13 h (TTL 12 h): $code"
  restore
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "X-Admin-Token: $tok" http://127.0.0.1:8000/api/sensors/admin)
  info "same admin session after the clock comes back: $code (pruned tokens don't come back)"
  # 3. backward jump while a PIN lockout runs (ph11: 4 misses → ~2 s lockout)
  for _ in 1 2 3 4; do ip netns exec ph11 python3 /tmp/hbp.py req POST /api/admin/login '{"pin":"000001"}' >/dev/null; done
  r=$(ip netns exec ph11 python3 /tmp/hbp.py req POST /api/admin/login '{"pin":"000001"}'); info "lockout before the jump: $r"
  S0=$(date +%s); date -s "@$((S0 - 3600))" >/dev/null
  sleep 3
  r=$(ip netns exec ph11 python3 /tmp/hbp.py req POST /api/admin/login "{\"pin\":\"$PIN\"}"); info "right PIN 3 s later after the clock went back 1 h: $r"
  local ra; ra=$(echo "$r" | awk '{print $NF}')
  case "$r" in
    429*) [ "${ra:-0}" -gt 600 ] && bad "a 1 h backward clock jump stretches a ${ra}s PIN lockout (expected a few seconds; wall-clock _now in server.login_wait)" || ok "lockout after backward jump stays short ($ra s)" ;;
    200*) ok "lockout after backward jump expired normally" ;;
    *) info "login after backward jump: $r" ;;
  esac
  # 4. vault idle timer with the clock going back
  if [ -n "$VP" ]; then
    python3 /tmp/hbp.py req POST /api/vault/unlock "{\"passphrase\":\"$VP\"}" "X-Member-Token=$MK" >/dev/null
    S0=$(date +%s); date -s "@$((S0 - 3600))" >/dev/null
    r=$(python3 /tmp/hbp.py req GET /api/vault/status "" "X-Member-Token=$MK")
    il=$(echo "$r" | python3 -c 'import json,sys;l=sys.stdin.read();print(json.loads(l.split(" ",1)[1].rsplit(" ",1)[0] if l.count(" ")>1 else "{}").get("idle_left",""))' 2>/dev/null)
    info "vault idle_left after the clock went back 1 h: ${il:-?} s (idle limit 900 s)"
    [ -n "$il" ] && [ "$il" -gt 1000 ] && bad "vault stays unlocked ${il}s after a 1 h backward clock jump (idle timer on wall clock)" || ok "vault idle timer unaffected by a backward jump"
    python3 /tmp/hbp.py req POST /api/vault/lock '{}' "X-Member-Token=$MK" >/dev/null
  fi
  restore
  # 5. far-future then back (e.g. a GPS week-rollover or a dead RTC): portal must keep working
  S0=$(date +%s); date -s "@$((S0 + 400 * 86400))" >/dev/null; sleep 2
  r=$(ip netns exec ph9 python3 /tmp/hbp.py req POST /api/board '{"name":"future","text":"from the future"}'); info "board post at +400 days: ${r:0:120}"
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://badger.lan/api/status)" = 200 ] && ok "portal fine at +400 days" || bad "portal broken at +400 days"
  restore; sleep 2
  r=$(ip netns exec ph10 python3 /tmp/hbp.py req POST /api/board '{"name":"now","text":"back to now"}'); info "board post after coming back: ${r:0:120}"
  sleep 6
  r=$(ip netns exec ph9 python3 /tmp/hbp.py req POST /api/board '{"name":"again","text":"posted in the future, now again"}')
  case "$r" in
    200*) ok "a phone that posted while the clock was ahead can post again after the clock is corrected" ;;
    *) bad "a phone that posted while the clock was 400 days ahead is still rate-limited after the correction: ${r:0:120} (server._last_post uses time.time)" ;;
  esac
  r=$(curl -s 'http://badger.lan/api/board' | python3 -c 'import json,sys;m=json.load(sys.stdin)["messages"];print(len(m), [x.get("text") for x in m[-3:]])' 2>/dev/null); info "board after the jumps: $r"
  systemctl is-active --quiet prepper-portal && ok "portal running after clock jumps" || bad "portal not running after clock jumps"
  systemctl start chrony 2>/dev/null; chronyc -a makestep >/dev/null 2>&1
  info "clock restored: $(date -u)"
  local f; f=$(systemctl --failed --no-legend | awk '{print $2}' | tr '\n' ' ')
  [ -z "$f" ] && ok "no failed units after clock jumps" || bad "failed after clock jumps: $f"
}

# ───────────────────────────── calm ─────────────────────────────
sec_calm() {
  echo "== calm: watching for $MINUTES minutes"
  local units since e
  units=$(systemctl list-unit-files 'prepper-*' 'upd-*' kiwix-serve.service llama-server.service --no-legend | awk '{print $1}' | grep -v '@')
  since=$(date '+%Y-%m-%d %H:%M:%S')
  declare -A r0
  for u in $units; do r0[$u]=$(systemctl show -p NRestarts --value "$u" 2>/dev/null); done
  sleep $((MINUTES * 60))
  for u in $units; do
    local r1; r1=$(systemctl show -p NRestarts --value "$u" 2>/dev/null)
    local st; st=$(systemctl show -p ActiveState,SubState,Result --value "$u" | tr '\n' '/')
    if [ -n "${r0[$u]}" ] && [ -n "$r1" ] && [ "$((r1 - r0[$u]))" -gt 2 ]; then bad "$u restarted $((r1 - r0[$u])) times in $MINUTES min ($st)"
    else echo "      $u: $st restarts ${r0[$u]:-?}→${r1:-?}"; fi
  done
  e=$(journalctl -p err --since "$since" --no-pager -q | wc -l)
  info "journal: $e err-level lines in $MINUTES min ($(python3 -c "print(round($e/$MINUTES,2))")/min)"
  journalctl -p err --since "$since" --no-pager -q | sed -E 's/^.{16}//' | sort | uniq -c | sort -rn | head -8 | sed 's/^/      /'
  local tot; tot=$(journalctl --since "$since" --no-pager -q | wc -l); info "journal: $tot lines total in $MINUTES min"
  journalctl --since "$since" --no-pager -q -o cat | sed -E 's/[0-9]+/N/g' | sort | uniq -c | sort -rn | head -6 | sed 's/^/      /'
  [ -z "$(systemctl --failed --no-legend)" ] && ok "no failed units after $MINUTES min" || bad "failed: $(systemctl --failed --no-legend | awk '{print $2}' | tr '\n' ' ')"
  [ "$e" -le $((MINUTES * 2)) ] && ok "journal error rate is calm" || bad "journal error rate high: $e lines in $MINUTES min"
}

# ───────────────────────────── reboot ─────────────────────────────
sec_prereboot() {
  echo "== before the reboot: arm a Wi-Fi password change"
  local out new
  out=$(WIFI_GRACE=900 /opt/prepper/os/bin/prepperctl wifi-rotate 2>&1)
  new=$(echo "$out" | sed -n 's/^passphrase: //p')
  [ -n "$new" ] && ok "wifi-rotate made a new password" || { bad "wifi-rotate: $out"; return; }
  echo "$new" > "$OUT/new-psk"
  sleep 20
  systemctl is-active --quiet prepper-wifi-revert.timer && ok "switch-back timer armed" || bad "switch-back timer not armed"
  info "wifi-status: $(/opt/prepper/os/bin/prepperctl wifi-status | tail -1)"
  PSK=$new; phone_up 3; sleep 10; phone_dhcp 3
  [ -n "$(phone_ip 3)" ] && ok "a phone joins with the new password" || bad "phone can't join with the new password"
  date +%s > "$OUT/reboot-at"
  stat -c '%a %U:%G %n' /etc/prepper/panel.key /etc/prepper/panel.key.sha256 /etc/prepper/prepper.toml > "$OUT/perms-before"
  echo "now: sudo systemctl reboot, then: sudo bash tests/vm/vm_pressure.sh postreboot"
}
sec_postreboot() {
  echo "== after the reboot"
  local up new
  up=$(( $(date +%s) - $(cat "$OUT/reboot-at" 2>/dev/null || date +%s) )); info "seconds since the reboot was ordered: $up"
  # (ssh answers a few seconds before prepper-net has finished: wait for the boot to settle)
  for _ in $(seq 1 120); do
    systemctl is-active --quiet prepper-net && systemctl is-active --quiet nginx && systemctl is-active --quiet prepper-portal && break; sleep 1
  done
  info "boot settled at $(cut -d. -f1 /proc/uptime) s of uptime ($(systemctl is-system-running))"
  for u in prepper-net prepper-portal nginx dnsmasq prepper-powerd chrony prepper-sensord prepper-sdr; do
    systemctl is-active --quiet "$u" && ok "$u active after reboot" || bad "$u after reboot: $(systemctl show -p ActiveState,SubState,Result --value "$u" | tr '\n' ' ')"
  done
  for t in upd-check.timer upd-apply.timer prepper-tle.timer prepper-forecast.timer prepper-speech-forecast-now.path; do
    systemctl is-enabled --quiet "$t" 2>/dev/null || { info "$t not enabled"; continue; }
    systemctl is-active --quiet "$t" && ok "$t active after reboot" || bad "$t not active after reboot"
  done
  pgrep -x hostapd >/dev/null && ok "hostapd up after reboot" || bad "hostapd down after reboot"
  if [ -e /var/lib/prepper/wifi-rotate-pending.json ]; then
    systemctl is-active --quiet prepper-wifi-revert.timer && ok "Wi-Fi switch-back re-armed after reboot ($(systemctl show -p NextElapseUSecMonotonic --value prepper-wifi-revert.timer 2>/dev/null))" || bad "Wi-Fi switch-back NOT re-armed after reboot"
    info "wifi-status: $(/opt/prepper/os/bin/prepperctl wifi-status | tail -1)"
    new=$(cat "$OUT/new-psk" 2>/dev/null)
    PSK=$new; PHONES=(3); phone_up 3; sleep 10; phone_dhcp 3
    [ -n "$(phone_ip 3)" ] && ok "phone joins with the (still pending) new password after reboot" || bad "phone can't join with the new password after reboot"
    /opt/prepper/os/bin/prepperctl wifi-revert >/dev/null 2>&1
    sleep 15
    grep -q '^passphrase = "badgertest123"' /etc/prepper/prepper.toml && ok "wifi-revert restored the old password" || bad "old password not restored: $(grep '^passphrase' /etc/prepper/prepper.toml | cut -c1-20)…"
    PSK=badgertest123; phone_up 3; sleep 12; phone_dhcp 3
    [ -n "$(phone_ip 3)" ] && ok "phone joins with the old password again" || bad "phone can't join with the old password after revert"
  else
    bad "no pending Wi-Fi change found after the reboot (was prereboot run?)"
  fi
  stat -c '%a %U:%G %n' /etc/prepper/panel.key /etc/prepper/panel.key.sha256 /etc/prepper/prepper.toml > "$OUT/perms-after"
  diff -q "$OUT/perms-before" "$OUT/perms-after" >/dev/null && ok "panel key, its hash and the config keep their permissions ($(tr '\n' ';' < "$OUT/perms-after"))" || bad "permissions changed: $(diff "$OUT/perms-before" "$OUT/perms-after" | tr '\n' ' ')"
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -H "X-Badger-Panel: $(cat /etc/prepper/panel.key)" -d '{}' http://127.0.0.1:8000/api/local/invite)
  [ "$code" = 200 ] && ok "panel key still works after reboot" || bad "panel invite after reboot: $code"
  stat -c '%a %U:%G' /run/prepper | grep -q '1775 root:prepper' && ok "/run/prepper 1775 root:prepper after reboot" || bad "/run/prepper is $(stat -c '%a %U:%G' /run/prepper)"
  [ "$(stat -c '%a %U:%G' /var/lib/prepper/portal)" = "750 prepper:prepper" ] && ok "portal data dir 750 prepper after reboot" || bad "portal data dir $(stat -c '%a %U:%G' /var/lib/prepper/portal)"
  [ -z "$(systemctl --failed --no-legend)" ] && ok "no failed units after reboot" || bad "failed units after reboot: $(systemctl --failed --no-legend | awk '{print $2}' | tr '\n' ' ')"
  info "boot time: $(systemd-analyze 2>/dev/null | head -1)"
  systemd-analyze blame 2>/dev/null | head -5 | sed 's/^/      /'
}

for s in "${SECTIONS[@]}"; do
  case "$s" in
    phones|load|features|abuse|mesh|power|diskfull|clock|calm|prereboot|postreboot) "sec_$s" ;;
    *) echo "unknown section $s"; exit 2 ;;
  esac
  echo
done
echo "RESULT: $pass passed, $fail failed, $infos info"
[ "$fail" = 0 ]
