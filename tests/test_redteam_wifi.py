#!/usr/bin/env python3
"""
Red team — the Wi-Fi side. Adversaries: a visitor on the hotspot (later rogue), anyone sniffing the
default OPEN hotspot from the street, an insider who knows the admin PIN.

Each failing test is a finding (see its `# FINDING:` comment). Passing tests pin down protections
that already hold, so a regression shows up here too.
Run: python3.11 tests/test_redteam_wifi.py -v
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "redteam"))
from harness import OPENSSL, PHONE_HTTP, ROGUE_HTTP, PortalCase, server  # noqa: E402

HOME = {"lat": 39.739212, "lon": -104.990251, "alt": 1609.3, "time": "x", "mode": 3}


def km(a_lat, a_lon, b_lat, b_lon):
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))


class VisitorLocation(PortalCase):
    """The household's location is the single most valuable fact for a roving band."""

    def setUp(self):
        super().setUp()
        self._gps = server.gps_fix
        server.gps_fix = lambda: dict(HOME)
        server._cache.clear()

    def tearDown(self):
        server.gps_fix = self._gps
        server.CFG.time_lat = server.CFG.time_lon = None

    def test_status_is_coarse_for_visitors(self):
        # Holds today: /api/status rounds to 0.1° (~11 km).
        gps = self.req("GET", "/api/status", headers=PHONE_HTTP)[1]["gps"]
        self.assertEqual((gps["lat"], gps["lon"]), (39.7, -105.0))

    def test_time_api_solar_mode_leaks_exact_longitude(self):
        # FINDING: /api/time?mode=solar returns notes.longitudeUsed = the GPS longitude to full precision
        # (server.py nu_time_now → nutime.nu_time(..., quantize_longitude=False)); route_get only coarsens
        # rec["location"]. Any visitor gets the exact longitude of the household.
        st, r = self.req("GET", "/api/time?mode=solar", headers=PHONE_HTTP)
        self.assertEqual(st, 200, r)
        used = (r.get("notes") or {}).get("longitudeUsed")
        self.assertTrue(used is None or abs(used - HOME["lon"]) > 0.05,
                        f"visitor learned longitude {used} (true {HOME['lon']})")

    def test_time_api_sun_events_leak_precise_position(self):
        # FINDING: sunEvents are computed from the precise GPS fix before the position is coarsened.
        # Sunrise/sunset to the second pin longitude to ~0.4 km and latitude to a few km; with the
        # coarse lat/lon in the same reply the visitor can solve for the exact place.
        st, r = self.req("GET", "/api/time", headers=PHONE_HTTP)
        self.assertEqual(st, 200, r)
        import datetime as dt
        sys.path.insert(0, str(Path(server.__file__).resolve().parents[1] / "nutime"))
        import nutime
        now = dt.datetime.now(dt.timezone.utc)
        precise = nutime.sun_events_at(now, HOME["lat"], HOME["lon"], server.CFG.time_tz)
        coarse = nutime.sun_events_at(now, round(HOME["lat"], 1), round(HOME["lon"], 1), server.CFG.time_tz)
        self.assertNotEqual(precise, coarse, "test premise: sun events differ between coarse and precise")
        self.assertNotEqual(r.get("sunEvents"), precise, "visitor got sun events for the exact position")

    def test_time_api_config_location_is_never_coarsened(self):
        # FINDING: with no GPS fix and [time] latitude/longitude set (the documented way to give the Badger
        # its home location), locationSource = "config" and route_get coarsens only the "gps" source:
        # the home coordinates go to every visitor verbatim.
        server.gps_fix = lambda: None
        server._cache.clear()
        server.CFG.time_lat, server.CFG.time_lon = HOME["lat"], HOME["lon"]
        st, r = self.req("GET", "/api/time", headers=PHONE_HTTP)
        self.assertEqual(st, 200, r)
        loc = r.get("location") or {}
        self.assertEqual(r.get("locationSource"), "config")
        self.assertNotEqual((loc.get("latitude"), loc.get("longitude")), (HOME["lat"], HOME["lon"]),
                            "home coordinates from prepper.toml handed to a visitor")


class VisitorRecon(PortalCase):
    """What a visitor learns about the household without any account."""

    def test_status_tells_visitors_power_and_occupancy(self):
        # FINDING (low): /api/status gives every visitor the battery/mains state, solar input, runtime,
        # how many devices are on the Wi-Fi and the uplink. "Grid down, 3 h of battery, 2 people home".
        p = self.tmp / "power.json"
        p.write_text('{"percent": 41, "external": false, "runtime_min": 190, "input_w": 0, "mode": "endurance"}')
        old = server.CFG.power_file
        server.CFG.power_file = p
        try:
            st = self.req("GET", "/api/status", headers=PHONE_HTTP)[1]
        finally:
            server.CFG.power_file = old
        self.assertIsNone(st.get("power"), f"visitor sees power state {st.get('power')}")

    def test_usb_drives_are_listed_to_visitors(self):
        # FINDING: /api/usb lists every mounted USB drive with a /files/… link, and nginx serves /files/
        # (alias /media/, autoindex on) to anyone on the Wi-Fi. A family member who plugs in a stick with
        # personal files (or a vault export) publishes it to every visitor.
        mnt = self.tmp / "media" / "usb-sda1"
        mnt.mkdir()
        import unittest.mock as m
        with m.patch.object(server.os.path, "ismount", lambda p: str(p) == str(mnt)), \
                m.patch.object(server, "on_usb_mount", lambda p, r: True):
            st, r = self.req("GET", "/api/usb", headers=PHONE_HTTP)
        self.assertEqual(st, 200)
        self.assertEqual(r, [], f"visitor sees USB drives: {r}")


@unittest.skipUnless(OPENSSL, "needs the openssl program")
class VaultOverPlainHTTP(PortalCase):
    FEATURES = ("vault.py", "_vault_crypto.py")
    PW = "purple tractor sings loudly"

    def test_vault_passphrase_and_documents_cross_the_air_in_clear(self):
        # FINDING: the vault unlock (passphrase) and every decrypted document are accepted/served over plain
        # http:// (X-Forwarded-Proto: http). The default hotspot is OPEN (passphrase = ""), so anyone within
        # Wi-Fi range — no association needed — captures the family passphrase, the member cookie and the
        # decrypted passport scans. docs/features/vault.md says visitors on the Wi-Fi get "Nothing".
        # The portal already refuses to send the Wi-Fi password / radio key over http (secure_transport);
        # the vault doesn't use that check.
        ana = self.join("Ana")
        # (CORRECTED with the owner's approval: setup also carries the passphrase, so it is now refused over plain
        # HTTP too — checked here; the vault is then set up over https, and the attack under test follows.)
        https = dict(PHONE_HTTP, **{"X-Forwarded-Proto": "https"})
        st, j = self.req("POST", "/api/vault/setup", {"passphrase": self.PW}, member=ana, headers=PHONE_HTTP)
        self.assertNotEqual(st, 200, "vault setup accepted the passphrase over plain HTTP")
        st, j = self.req("POST", "/api/vault/setup", {"passphrase": self.PW}, member=ana, headers=https)
        self.assertEqual(st, 200, j)
        st, j = self.req("POST", "/api/vault/note", {"text": "Passport X1234567 for Ana", "name": "passport"},
                         member=ana, headers=https)
        st, j = self.req("POST", "/api/vault/lock", {}, member=ana, headers=https)
        st, j = self.req("POST", "/api/vault/unlock", {"passphrase": self.PW}, member=ana, headers=PHONE_HTTP)
        self.assertNotEqual(st, 200, "vault unlocked with the passphrase sent over plain HTTP")


class AdminPinIsTheMasterKey(PortalCase):
    """Insider (or a coerced admin, or a raider who cracked the PIN hash off a seized disk)."""

    def test_admin_pin_can_impersonate_an_existing_member(self):
        # FINDING: op=invite with id=<member> creates an "extra device" invite for that member. Whoever holds
        # the admin PIN joins AS "Mom" and posts on the family board with the ✓ (verified) mark. FEATURES.md
        # says "the PIN alone does not open family-only pages"; it does, one step removed, and silently.
        mom = self.join("Mom")
        mid = self.member_id("Mom")
        fake = self.join("ignored", member_id=mid, headers=ROGUE_HTTP)          # the rogue admin's own phone
        st, me = self.req("GET", "/api/members/me", member=fake, headers=ROGUE_HTTP)
        # (FIXED: the extra device now waits for approval from one of Mom's own devices or the front panel,
        # so the attack can no longer assume it is "Mom" — this line used to assertEqual(..., "Mom"))
        self.assertNotEqual((me.get("member") or {}).get("name"), "Mom", "admin PIN alone made a device 'Mom'")
        self.req("POST", "/api/board", {"board": "family", "text": "Meet at the old mill at 9, bring the kids"},
                 member=fake, headers=ROGUE_HTTP)
        msgs = self.req("GET", "/api/board?board=family", member=mom, headers=PHONE_HTTP)[1]["messages"]
        forged = [m for m in msgs if "old mill" in m["text"]]
        self.assertFalse(any(m["name"] == "Mom" and m.get("verified") for m in forged),
                         "admin PIN produced a ✓ verified family post as Mom")

    def test_admin_pin_can_bind_any_radio_to_a_member(self):
        # FINDING: op=add_radio lets the admin attach any radio ID to a member; meshbridge then labels every
        # family-channel post from that ID with the member's name. With the PIN (and the family PSK, which
        # the admin can also read over https), a rogue insider speaks on the radio as any family member.
        self.join("Dad")
        mid = self.member_id("Dad")
        st, r = self.admin_op({"op": "add_radio", "id": mid, "radio": "!deadbeef"})
        self.assertNotEqual(st, 200, "admin attached an arbitrary radio to Dad without Dad's consent")

    def test_admin_reads_family_channel_key_over_any_https(self):
        # Holds partly: over plain http the key is withheld. FINDING (design): over https it is shown to
        # any admin session — the self-signed certificate means an attacker just clicks through, so the
        # protection is against passive sniffers only, not against whoever has the PIN.
        (self.tmp / "mesh-channel.json").write_text('{"url": "https://meshtastic.org/e/#SECRETKEY", "at": 1}')
        st, r = self.admin_op({"op": "mesh_link"}, headers={"X-Real-IP": "10.10.10.66", "X-Forwarded-Proto": "https"})
        self.assertIsNone(r.get("url"), "admin PIN alone yields the family radio key")


class VisitorUsesTheBadgersRadio(PortalCase):
    def test_anonymous_visitor_transmits_on_the_mesh_as_anyone(self):
        # FINDING: a visitor (no account) posts on the community board with mesh=true and any name — even
        # "Badger" or a family member's name — and the Badger's own radio transmits it on the public
        # channel. The household's node becomes the sender of a lure ("Free water at the quarry, 9 pm") and
        # a transmitter a direction-finding team can trigger at will (1 post / 5 s per IP; IPs are free).
        st, r = self.req("POST", "/api/board", {"text": "Water handout at the quarry 21:00, come alone", "name": "Badger",
                                                "mesh": True}, headers=ROGUE_HTTP)
        self.assertEqual(st, 200, r)
        p = self.tmp / "mesh-outbox.jsonl"
        out = p.read_text() if p.exists() else ""          # (CORRECTED with the owner's approval: nothing queued → no file)
        self.assertNotIn("Badger: Water handout", out, "anonymous visitor text queued for the Badger's radio")


if __name__ == "__main__":
    unittest.main()
