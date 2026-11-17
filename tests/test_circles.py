"""
Trusted circles: the sealed circle protocol and its sharing rules (meshbridge.CircleService), channel links,
circle operations on the radio (create / join / rotate / leave / rename) with the request failsafes, a
two-Badger exchange over a fake channel, and the portal plugin (features/circles.py) over HTTP.
Run: python3.11 tests/test_circles.py
"""
from __future__ import annotations

import base64
import contextlib
import io
import json
import random
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "software" / "meshbridge"))
import meshbridge as mb  # noqa: E402
from test_posture import PHONE, PHONE_TLS, PostureCase, P, server  # noqa: E402,F401
from test_posture_mesh import FakeNode, ns  # noqa: E402

KEY = mb.circle_key(b"K" * 32)
OTHER = mb.circle_key(b"Z" * 32)


def conf(share=("skills", "resources", "aid", "missing"), offers=None, blocked=(), name="Elm St"):
    return mb.circle_conf({"name": name, "share": {c: True for c in share}, "blocked": list(blocked),
                           "offers": offers if offers is not None else [
                               {"category": "skills", "topic": "power", "text": "can fix generators", "at": 1},
                               {"category": "resources", "topic": "water", "text": "", "at": 2},
                               {"category": "missing", "topic": "missing", "text": "reports of two people", "at": 3}]})


class Proto(unittest.TestCase):
    def test_seal_open_roundtrip_and_padding(self):
        rng = random.Random(1)
        lens = set()
        for _ in range(200):
            q = mb.circle_query(b"abcd", "power", KEY, rng)
            lens.add(len(q))
            self.assertLessEqual(len(q), mb.CIRCLE_MAX)
            self.assertGreaterEqual(len(q), mb.CIRCLE_MIN)
            self.assertEqual(mb.circle_open(q, KEY), {"type": "query", "qid": b"abcd", "topic": "power"})
        self.assertGreater(len(lens), 15)                           # random lengths, no fixed signature
        a = mb.circle_answer(b"wxyz", "power: can fix generators — ask Elm St", KEY, rng)
        self.assertEqual(mb.circle_open(a, KEY)["text"], "power: can fix generators — ask Elm St")
        self.assertEqual(mb.circle_open(base64.b64encode(a).decode(), KEY)["type"], "answer")

    def test_wrong_key_tampered_garbage(self):
        q = mb.circle_query(b"abcd", "food", KEY)
        self.assertIsNone(mb.circle_open(q, OTHER))
        bad = bytearray(q)
        bad[16] ^= 1
        self.assertIsNone(mb.circle_open(bytes(bad), KEY))
        for junk in (b"", b"x" * 10, b"\x00" * 500, "!!!", None, 5, mb.encode_beacon(mb.ctrl_key(b"K" * 32))):
            self.assertIsNone(mb.circle_open(junk, KEY))
        # a courtesy packet sealed with the courtesy key is not a circle packet, and vice versa
        self.assertIsNone(mb.decode_control(q, mb.ctrl_key(b"K" * 32)))

    def test_no_marker_in_cleartext(self):
        q = mb.circle_query(b"abcd", "medical", KEY)
        self.assertNotIn(b"medical", q)
        self.assertNotIn(b"HB", q[:2])

    def test_channel_link_roundtrip_and_hostile(self):
        url = mb.channel_link("Circle", b"S" * 32)
        self.assertTrue(url.startswith("https://meshtastic.org/e/#"))
        self.assertEqual(mb.parse_channel_link(url), [{"name": "Circle", "psk": b"S" * 32}])
        self.assertEqual(mb.parse_channel_link(url.replace("/e/#", "/e/?add=true#"))[0]["psk"], b"S" * 32)
        for bad in ("", "https://evil.example/e/#AAAA", "https://meshtastic.org/e/#" + "A" * 2000, None, 5,
                    "https://meshtastic.org/e/#////", "https://meshtastic.org/e/#CgA",
                    "https://meshtastic.org/e/#" + base64.urlsafe_b64encode(b"\x0a\xff\xff\xff\xff\x0f").decode(),
                    "https://meshtastic.org/e/#" + base64.urlsafe_b64encode(b"\x0b\x01").decode()):
            with self.assertRaises(ValueError):
                mb.parse_channel_link(bad)


class Rules(unittest.TestCase):
    def setUp(self):
        self.now = [10_000.0]
        self.texts, self.data = [], []
        self.sharing = [True]
        self.conf = conf()
        self.svc = mb.CircleService(lambda: 3, lambda: KEY, lambda t, d, c: self.texts.append((t, d, c)) or True,
                                    lambda p, c, d: self.data.append((p, c, d)) or True, lambda: self.now[0],
                                    rng=random.Random(2), conf=lambda: self.conf, sharing=lambda: self.sharing[0],
                                    my_id="!0badfeed")

    def text(self, t, sender="!11111111", ch=3):
        return self.svc.heard_text(t, {"fromId": sender, "channel": ch, "decoded": {"text": t}})

    def flush(self):
        self.now[0] += 11
        self.svc.tick()

    def test_pointer_answer_to_requester_only_after_a_wait(self):
        self.assertTrue(self.text("!help power"))
        self.svc.tick()
        self.assertEqual(self.texts, [])                            # never at once
        self.flush()
        self.assertEqual(self.texts, [("power: can fix generators — ask Elm St", "!11111111", 3)])
        t = self.texts[0][0].lower()
        self.assertNotIn("badger", t)
        self.assertEqual(self.svc.log[-1]["result"], "answered")

    def test_only_opted_in_categories_and_no_answer_means_silence(self):
        self.conf = conf(share=("resources",))
        self.text("!help power")
        self.text("!help water", sender="!22222222")
        self.flush()
        self.assertEqual([d for _, d, _ in self.texts], ["!22222222"])
        self.assertEqual(self.texts[0][0], "Someone can help with water — ask Elm St")
        self.assertEqual(self.svc.log[0]["result"], "nothing to offer")

    def test_not_on_other_channels_and_not_plain_text(self):
        self.assertFalse(self.text("!help power", ch=0))
        self.assertFalse(self.text("!help power", ch=1))
        self.assertFalse(self.text("hello there"))
        self.assertTrue(self.text("!help"))                         # nothing (no menu of what we have)
        self.flush()
        self.assertEqual(self.texts, [])

    def test_sweeps_refused_and_flagged(self):
        for q in ("!help all", "!help *", "!help power water", "!help inventory", "!help everything"):
            self.text(q, sender="!33333333")
        self.flush()
        self.assertEqual(self.texts, [])
        self.assertEqual(len(self.svc.flags), 5)
        self.assertTrue(all(e["result"].startswith("refused: sweep") for e in self.svc.log))

    def test_many_topics_in_an_hour_is_a_sweep(self):
        for t in ("power", "water", "food", "medical", "shelter", "tools"):
            self.text("!help " + t, sender="!44444444")
            self.now[0] += 60
        self.assertIn("refused: sweep (flagged)", [e["result"] for e in self.svc.log])
        self.assertTrue(any("different" in f["what"] for f in self.svc.flags))

    def test_budgets(self):
        self.text("!help power")
        self.text("!help power")                                    # same topic again: too soon
        self.assertEqual(self.svc.log[-1]["result"], "refused: asked again too soon")
        self.conf = conf(offers=[{"category": "skills", "topic": t, "text": "", "at": 1}
                                 for t in ("water", "food", "medical", "tools")])
        for t in ("water", "food", "medical"):
            self.text("!help " + t)
        self.assertEqual(self.svc.log[-1]["result"], "refused: budget")     # 3 an hour per radio
        # forged ids: the total holds
        for i in range(20):
            self.text("!help tools", sender=f"!5{i:07x}")
        self.assertEqual(sum(1 for e in self.svc.log if e["result"] == "answered"), 10)

    def test_blocked_lockdown_unknown(self):
        self.conf = conf(blocked=["!66666666"])
        self.text("!help power", sender="!66666666")
        self.assertEqual(self.svc.log[-1]["result"], "refused: blocked")
        self.sharing[0] = False
        self.text("!help power", sender="!77777777")
        self.assertEqual(self.svc.log[-1]["result"], "refused: Lockdown")
        self.sharing[0] = True
        self.text("!help weapons", sender="!77777777")
        self.assertEqual(self.svc.log[-1]["result"], "unknown topic")
        self.flush()
        self.assertEqual(self.texts, [])

    def test_lockdown_after_scheduling_cancels(self):
        self.text("!help power")
        self.sharing[0] = False
        self.flush()
        self.assertEqual(self.texts, [])

    def test_packet_query_answered_by_unicast_and_our_asks(self):
        q = mb.circle_query(b"q123", "water", KEY)
        self.assertTrue(self.svc.heard_data({"fromId": "!88888888", "channel": 3,
                                             "decoded": {"portnum": "PRIVATE_APP", "payload": q}}))
        self.flush()
        payload, ch, dest = self.data[0]
        self.assertEqual((ch, dest), (3, "!88888888"))
        self.assertEqual(mb.circle_open(payload, KEY)["text"], "Someone can help with water — ask Elm St")
        # our own question: broadcast; only answers with its qid count, once per radio
        self.data.clear()
        self.assertIsNone(self.svc.ask("medical"))
        qpay = self.data[0][0]
        qid = mb.circle_open(qpay, KEY)["qid"]
        ans = mb.circle_answer(qid, "medical: nurse — ask Oak", KEY)
        for sender in ("!99999999", "!99999999"):
            self.svc.heard_data({"fromId": sender, "channel": 3, "decoded": {"portnum": "PRIVATE_APP", "payload": ans}})
        self.svc.heard_data({"fromId": "!aaaaaaaa", "channel": 3, "decoded": {"portnum": "PRIVATE_APP",
                                                                              "payload": mb.circle_answer(b"nope", "x", KEY)}})
        self.assertEqual([a["text"] for a in self.svc.answers], ["medical: nurse — ask Oak"])
        self.assertEqual(self.svc.ask("bogus"), "unknown topic")
        self.sharing[0] = False
        self.assertIn("Lockdown", self.svc.ask("food"))

    def test_ask_budget_and_other_channel(self):
        for _ in range(mb.CIRCLE_ASKS[0]):
            self.assertIsNone(self.svc.ask("food"))
        self.assertEqual(self.svc.ask("food"), "too many questions this hour")
        self.assertFalse(self.svc.heard_data({"fromId": "!88888888", "channel": 0, "decoded": {
            "portnum": "PRIVATE_APP", "payload": mb.circle_query(b"q", "food", KEY)}}))

    def test_status_doc_is_json(self):
        self.text("!help power")
        json.dumps(self.svc.status())

    def test_conf_validation(self):
        c = mb.circle_conf({"name": "x" * 99, "share": {"skills": "yes", "aid": True},
                            "offers": [{"category": "weapons", "topic": "power"}, {"category": "skills", "topic": "power",
                                       "text": "a‮b", "expires": 1}, "junk"],
                            "blocked": ["!ABCDEF12", "evil", 5]})
        self.assertEqual(len(c["name"]), 24)
        self.assertEqual(c["share"], {"skills": False, "resources": False, "aid": True, "missing": False})
        self.assertEqual(c["offers"], [])                           # bad category, expired
        self.assertEqual(c["blocked"], {"!abcdef12"})


class RadioOps(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="circle-ops-"))
        self.slot, self.link = self.tmp / "slot.json", self.tmp / "circle.json"
        self.node = FakeNode()
        self.iface = ns(localNode=self.node)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def op(self, **req):
        return mb.circle_op(self.iface, req, cfg={}, family_ch=1, public_ch=0, slot_path=self.slot, link_path=self.link,
                            rng=lambda n: b"R" * n)

    def test_create_rotate_leave(self):
        r = self.op(op="create", name="Oak Circle")
        self.assertEqual(r, {"op": "create", "channel": 2, "name": "Oak Circle"})
        c = self.node.channels[2]
        self.assertEqual((c.role, c.settings.psk, c.settings.module_settings.position_precision), (2, b"R" * 32, 0))
        self.assertFalse(c.settings.uplink_enabled or c.settings.downlink_enabled)
        self.assertEqual(json.loads(self.slot.read_text())["ch"], 2)
        link = json.loads(self.link.read_text())["url"]
        self.assertEqual(stat.S_IMODE(self.link.stat().st_mode), 0o600)
        self.assertEqual(mb.parse_channel_link(link), [{"name": "Oak Circle", "psk": b"R" * 32}])
        self.assertNotIn(b"F" * 32, base64.urlsafe_b64decode(link.split("#")[1] + "=="))   # never the family key
        self.assertNotIn("psk", json.dumps(r))
        r = mb.circle_op(self.iface, {"op": "rotate"}, cfg={}, family_ch=1, public_ch=0, slot_path=self.slot,
                         link_path=self.link, rng=lambda n: b"Q" * n)
        self.assertEqual(self.node.channels[2].settings.psk, b"Q" * 32)
        self.assertIn(base64.urlsafe_b64encode(b"Q" * 32).decode()[:20], json.loads(self.link.read_text())["url"])
        self.op(op="leave")
        self.assertEqual((self.node.channels[2].role, self.node.channels[2].settings.psk), (0, b""))
        self.assertFalse(self.slot.exists() or self.link.exists())
        with self.assertRaises(ValueError):
            self.op(op="rotate")

    def test_join_rules(self):
        good = mb.channel_link("Theirs", b"T" * 32)
        r = self.op(op="join", url=good)
        self.assertEqual((r["channel"], self.node.channels[2].settings.psk), (2, b"T" * 32))
        self.op(op="leave")
        for url, why in ((mb.channel_link("weak", b"\x01"), "default"), (mb.channel_link("fam", b"F" * 32), "already"),
                         ("https://evil.example/e/#x", "Meshtastic")):
            with self.assertRaises(ValueError) as cm:
                self.op(op="join", url=url)
            self.assertIn(why, str(cm.exception))

    def test_names_and_full_radio(self):
        with self.assertRaises(ValueError):
            self.op(op="create", name="bad/name!!")
        for c in self.node.channels[1:]:
            c.role = 2
        with self.assertRaises(ValueError) as cm:
            self.op(op="create", name="x")
        self.assertIn("no free channel", str(cm.exception))

    def test_configured_channel_wins_and_public_never(self):
        r = mb.circle_op(self.iface, {"op": "create", "name": "c"}, cfg={"mesh_circle_channel": 5}, family_ch=1,
                         public_ch=0, slot_path=self.slot, link_path=self.link)
        self.assertEqual(r["channel"], 5)
        self.assertEqual(mb.circle_slot({"mesh_circle_channel": 0}, 1, 0, None), -1)
        self.slot.write_text(json.dumps({"ch": 1}))                  # a forged slot pointing at the family channel
        self.assertEqual(mb.circle_slot({}, 1, 0, self.slot), -1)

    def test_rename(self):
        r = self.op(op="rename", my_id="!0badfeed")
        self.assertEqual(self.node.owner, ("Meshtastic feed", "feed"))
        self.assertEqual(r["short_name"], "feed")

    def test_request_failsafes(self):
        req, res = self.tmp / "req.json", self.tmp / "res.json"
        calls = []

        def run(r):
            calls.append(r["op"])
            return {"op": r["op"]}
        req.write_text(json.dumps({"id": "a", "at": time.time() - 500, "op": "create"}))
        with contextlib.redirect_stderr(io.StringIO()):
            out = mb.handle_radio_request(self.iface, req, res, run)
        self.assertEqual((out["ok"], calls), (False, []))
        self.assertIn("too old", out["error"])
        self.assertFalse(req.exists())
        req.write_text(json.dumps({"id": "b", "at": time.time(), "op": "create"}))
        with contextlib.redirect_stderr(io.StringIO()):
            mb.handle_radio_request(self.iface, req, res, run)
            mb.handle_radio_request(self.iface, req, res, run)
        self.assertEqual(calls, ["create"])
        self.assertEqual(stat.S_IMODE(res.stat().st_mode), 0o600)
        req.write_text("junk")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(mb.handle_radio_request(self.iface, req, res, run))
        req.write_text(json.dumps({"id": "c", "at": time.time(), "op": "x"}))

        def boom(r):
            raise ValueError("no way")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mb.handle_radio_request(self.iface, req, res, boom)["error"], "no way")

    def test_read_asks(self):
        p = self.tmp / "ask.json"
        seen = __import__("collections").OrderedDict()
        now = time.time()
        p.write_text(json.dumps({"asks": [{"id": "1", "topic": "food", "at": now}, {"id": "2", "topic": "guns", "at": now},
                                          {"id": "3", "topic": "water", "at": now - 999}, "x"]}))
        self.assertEqual(mb.read_asks(p, seen, now), ["food"])
        self.assertEqual(mb.read_asks(p, seen, now), [])


class TwoBadgers(unittest.TestCase):
    """Two CircleServices on one fake circle channel: B asks, A answers B only."""

    def test_exchange(self):
        now = [0.0]
        air = []
        mk = lambda me, c: mb.CircleService(lambda: 2, lambda: KEY, lambda t, d, ch: True,  # noqa: E731
                                            lambda p, ch, d, me=me: air.append((me, p, d)) or True, lambda: now[0],
                                            rng=random.Random(me), conf=lambda: c, my_id=me)
        a = mk("!aaaaaaaa", conf(name="Elm St"))
        b = mk("!bbbbbbbb", conf(share=(), offers=[], name="Oak"))
        b.ask("power")
        frm, payload, dest = air.pop()
        self.assertIsNone(dest)
        for s in (a, b):
            s.heard_data({"fromId": frm, "channel": 2, "decoded": {"portnum": "PRIVATE_APP", "payload": payload}})
        now[0] += 20
        a.tick()
        b.tick()
        frm, payload, dest = air.pop()
        self.assertEqual((frm, dest), ("!aaaaaaaa", "!bbbbbbbb"))
        self.assertEqual(air, [])                                   # B had nothing to offer: silence
        b.heard_data({"fromId": frm, "channel": 2, "decoded": {"portnum": "PRIVATE_APP", "payload": payload}})
        self.assertEqual(b.answers[0]["text"], "power: can fix generators — ask Elm St")


# ───────────────────────────── the portal plugin ─────────────────────────────

class CirclesHTTP(PostureCase):
    def setUp(self):
        super().setUp()
        d = self.tmp / "features-data" / "circles"
        if d.exists():
            shutil.rmtree(d)
        for f in ("mesh-radio-req.json", "mesh-circle-slot.json"):
            (self.tmp / f).unlink(missing_ok=True)
        for f in ("circle-ask.json", "mesh-circle.json", "circle-status.json"):
            (self.tmp / "run" / f).unlink(missing_ok=True)

    def in_circle(self):
        (self.tmp / "mesh-circle-slot.json").write_text(json.dumps({"ch": 2}))

    def test_access(self):
        self.assertEqual(self.req("GET", "/api/circles", headers=PHONE)[0], 401)
        st, d = self.req("GET", "/api/circles", member=self.member, headers=PHONE)
        self.assertEqual((st, d["in_circle"]), (200, False))
        self.assertNotIn("log", d)
        st, d = self.req("GET", "/api/circles", admin=self.admin, headers=PHONE)
        self.assertIn("log", d)
        for path, body in (("/api/circles/op", {"op": "create"}), ("/api/circles/settings", {"name": "x"}),
                           ("/api/circles/offer", {"op": "add"}), ("/api/circles/block", {"radio": "!12345678", "block": True})):
            self.assertEqual(self.req("POST", path, body, member=self.member, headers=PHONE)[0], 401, path)
            self.assertEqual(self.req("POST", path, body, headers=PHONE)[0], 401, path)

    def test_create_request_and_result(self):
        st, r = self.req("POST", "/api/circles/op", {"op": "create", "name": "Oak"}, admin=self.admin, headers=PHONE)
        self.assertEqual(st, 200)
        req = self.tmp / "mesh-radio-req.json"
        d = json.loads(req.read_text())
        self.assertEqual((d["op"], d["name"], d["id"]), ("create", "Oak", r["id"]))
        self.assertEqual(stat.S_IMODE(req.stat().st_mode), 0o600)
        self.assertFalse(self.req("GET", "/api/circles/op?id=" + r["id"], admin=self.admin, headers=PHONE)[1]["done"])
        (self.tmp / "mesh-radio-res.json").write_text(json.dumps({"id": r["id"], "ok": True, "op": "create", "channel": 2,
                                                                 "req": "sig"}))
        res = self.req("GET", "/api/circles/op?id=" + r["id"], admin=self.admin, headers=PHONE)[1]
        self.assertEqual(res["result"], {"ok": True, "op": "create", "channel": 2})
        for name in ("Badger Net", "a" * 12, "x;rm"):
            self.assertEqual(self.req("POST", "/api/circles/op", {"op": "create", "name": name}, admin=self.admin,
                                      headers=PHONE)[0], 400, name)
        self.assertEqual(self.req("POST", "/api/circles/op", {"op": "rotate"}, admin=self.admin, headers=PHONE)[0], 409)

    def test_join_needs_https(self):
        url = mb.channel_link("Theirs", b"T" * 32)
        self.assertEqual(self.req("POST", "/api/circles/op", {"op": "join", "url": url}, admin=self.admin, headers=PHONE)[0], 403)
        self.assertFalse((self.tmp / "mesh-radio-req.json").exists())
        self.assertEqual(self.req("POST", "/api/circles/op", {"op": "join", "url": "javascript:1"}, admin=self.admin,
                                  headers=PHONE_TLS)[0], 400)
        st, _ = self.req("POST", "/api/circles/op", {"op": "join", "url": url}, admin=self.admin, headers=PHONE_TLS)
        self.assertEqual(st, 200)
        self.in_circle()
        self.assertEqual(self.req("POST", "/api/circles/op", {"op": "join", "url": url}, admin=self.admin,
                                  headers=PHONE_TLS)[0], 409)

    def test_settings_and_offers_rules(self):
        A = dict(admin=self.admin, headers=PHONE)
        self.assertEqual(self.req("POST", "/api/circles/settings", {"name": "The Prepper House"}, **A)[0], 400)
        st, r = self.req("POST", "/api/circles/settings", {"name": "12 Elm Street"}, **A)
        self.assertEqual((st, r["needs_confirm"]), (409, True))
        self.assertEqual(self.req("POST", "/api/circles/settings", {"name": "12 Elm Street", "confirm": True}, **A)[0], 200)
        self.assertEqual(self.req("POST", "/api/circles/settings", {"name": "Elm St"}, **A)[0], 200)
        self.assertEqual(self.req("POST", "/api/circles/settings", {"share": {"guns": True}}, **A)[0], 400)
        self.assertEqual(self.req("POST", "/api/circles/settings", {"share": {"skills": "yes"}}, **A)[0], 400)
        add = lambda **kw: self.req("POST", "/api/circles/offer", dict({"op": "add"}, **kw), **A)  # noqa: E731
        for text in ("20 gallons of water", "5 generators", "plenty of food", "3 days of food", "100 rounds"):
            self.assertEqual(add(category="resources", topic="water", text=text)[0], 400, text)
        self.assertEqual(add(category="aid", text="open, 2 beds")[0], 400)
        self.assertEqual(add(category="skills", topic="weapons", text="x")[0], 400)
        self.assertEqual(add(category="guns", topic="power")[0], 400)
        self.assertEqual(add(category="skills", topic="power", text="x" * 81)[0], 400)
        self.assertEqual(add(category="skills", topic="power", text="x", days=999)[0], 400)
        st, r = add(category="skills", topic="medical", text="nurse, ask at 39.7392, -104.9903")
        self.assertEqual((st, r.get("needs_confirm")), (409, True))
        st, r = add(category="skills", topic="power", text="can fix generators")
        self.assertEqual((st, r["shared"]), (200, False))            # not shared until opted in
        st, r = add(category="aid", text="open · water, first aid")
        self.assertEqual(r["offer"]["topic"], "aid")
        self.req("POST", "/api/circles/settings", {"share": {"skills": True}}, **A)
        b = json.loads((self.tmp / "features-data" / "circles" / "bridge.json").read_text())
        self.assertEqual(b["share"]["skills"], True)
        self.assertEqual({o["topic"] for o in b["offers"]}, {"power", "aid"})
        self.assertTrue(all("id" not in o for o in b["offers"]))       # the bridge gets only what it needs
        self.assertEqual(b["name"], "Elm St")
        conf_ = mb.circle_conf(b)                                     # and the bridge accepts it
        self.assertEqual(len(conf_["offers"]), 2)
        oid = r["offer"]["id"]
        self.assertEqual(self.req("POST", "/api/circles/offer", {"op": "remove", "id": oid}, **A)[0], 200)
        self.assertEqual(self.req("POST", "/api/circles/offer", {"op": "remove", "id": oid}, **A)[0], 404)

    def test_offer_cap(self):
        A = dict(admin=self.admin, headers=PHONE)
        for i in range(50):
            self.assertEqual(self.req("POST", "/api/circles/offer", {"op": "add", "category": "skills", "topic": "other",
                                                                      "text": f"skill {chr(97 + i % 26)}"}, **A)[0], 200)
        self.assertEqual(self.req("POST", "/api/circles/offer", {"op": "add", "category": "skills", "topic": "other"}, **A)[0], 409)

    def test_block(self):
        A = dict(admin=self.admin, headers=PHONE)
        for bad in ({"radio": "evil", "block": True}, {"radio": "!12345678", "block": "yes"}, {}):
            self.assertEqual(self.req("POST", "/api/circles/block", bad, **A)[0], 400)
        st, r = self.req("POST", "/api/circles/block", {"radio": "!ABCDEF12", "block": True, "label": "stolen"}, **A)
        self.assertEqual(r["blocked"][0]["radio"], "!abcdef12")
        b = json.loads((self.tmp / "features-data" / "circles" / "bridge.json").read_text())
        self.assertEqual(b["blocked"], ["!abcdef12"])
        st, r = self.req("POST", "/api/circles/block", {"radio": "!abcdef12", "block": False}, **A)
        self.assertEqual(r["blocked"], [])

    def test_ask(self):
        M = dict(member=self.member, headers=PHONE)
        self.assertEqual(self.req("POST", "/api/circles/ask", {"topic": "power"}, **M)[0], 409)     # no circle
        self.in_circle()
        self.assertEqual(self.req("POST", "/api/circles/ask", {"topic": "everything"}, **M)[0], 400)
        self.assertEqual(self.req("POST", "/api/circles/ask", {"topic": "power"}, headers=PHONE)[0], 401)
        st, r = self.req("POST", "/api/circles/ask", {"topic": "power"}, **M)
        self.assertEqual(st, 200)
        asks = json.loads((self.tmp / "run" / "circle-ask.json").read_text())["asks"]
        self.assertEqual([a["topic"] for a in asks], ["power"])
        self.assertEqual(mb.read_asks(self.tmp / "run" / "circle-ask.json", __import__("collections").OrderedDict(),
                                      time.time()), ["power"])
        self.assertEqual(self.req("POST", "/api/circles/ask", {"topic": "water"}, **M)[0], 429)     # one a minute
        self.set_level("lockdown")
        self.assertEqual(self.req("POST", "/api/circles/ask", {"topic": "water"}, admin=self.admin, headers=PHONE)[0], 409)

    def test_link_members_only_https(self):
        (self.tmp / "run" / "mesh-circle.json").write_text(json.dumps({"url": "https://meshtastic.org/e/#SECRET", "at": 1}))
        self.assertEqual(self.req("GET", "/api/circles/link", admin=self.admin, headers=PHONE_TLS)[0], 401)
        self.assertEqual(self.req("GET", "/api/circles/link", headers=PHONE_TLS)[0], 401)
        st, r = self.req("GET", "/api/circles/link", member=self.member, headers=PHONE)
        self.assertEqual((st, r["url"], r["secret_hidden"]), (200, None, True))
        st, r = self.req("GET", "/api/circles/link", member=self.member, headers=PHONE_TLS)
        self.assertEqual(r["url"], "https://meshtastic.org/e/#SECRET")
        self.assertEqual(self.req("GET", "/api/circles/link", panel=True)[1]["url"], "https://meshtastic.org/e/#SECRET")

    def test_bridge_status_shown(self):
        self.in_circle()
        (self.tmp / "run" / "circle-status.json").write_text(json.dumps({
            "updated": time.time(), "channel": 2, "log": [{"at": 1, "from": "!11111111", "topic": "power",
                                                            "result": "answered", "via": "text"}],
            "flags": [{"at": 1, "from": "!22222222", "what": "asked for everything", "via": "text"}],
            "answers": [{"at": 1, "topic": "water", "from": "!33333333", "text": "<b>water</b> — ask Oak"}]}))
        st, d = self.req("GET", "/api/circles", admin=self.admin, headers=PHONE)
        self.assertEqual((d["in_circle"], d["bridge_running"], len(d["log"]), len(d["flags"])), (True, True, 1, 1))
        self.assertEqual(d["answers"][0]["text"], "<b>water</b> — ask Oak")   # (escaped by the page script)
        st, d = self.req("GET", "/api/circles", member=self.member, headers=PHONE)
        self.assertNotIn("flags", d)


if __name__ == "__main__":
    unittest.main()
