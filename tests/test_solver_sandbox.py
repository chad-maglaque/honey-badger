"""
Problem solver: the sandboxed Python runner. Security-critical (the Badger must never be turned
against its owners: an article that talks the model into running code must not reach family data,
keys or the network).

Layers tested here:
  1. The portal never runs code unless a probe proves OS isolation (verify_probe + SandboxClient):
     on this laptop (no systemd sandbox) the runner MUST come out disabled.
  2. The runner's own inner layer (audit hook + rlimits + empty env + own session), which is
     testable anywhere: file reads of family data, network, subprocess, fork bombs, CPU, output floods.
  3. The real OS layer (systemd unit), when this runs on a Badger with the socket installed.
Run: python3.11 tests/test_solver_sandbox.py
"""
from __future__ import annotations

import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "software"))

from solver import sandbox, sandbox_child, tools  # noqa: E402

CHILD = REPO / "software" / "solver" / "sandbox_child.py"


def run(code, timeout=10):
    return sandbox_child.run_code(code, timeout)


def blocked(res) -> bool:
    return res["ok"] and res["exit"] != 0 and ("not allowed in the sandbox" in res["stderr"] or "PermissionError" in res["stderr"])


class InnerLayer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fam = Path(tempfile.mkdtemp(prefix="famdata-")).resolve()
        (cls.fam / "prepper.toml").write_text('admin_pin = "SECRET-PIN-HASH"\n')
        (cls.fam / "members.json").write_text('{"keys": "SECRET-MEMBER-KEYS"}')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.fam, ignore_errors=True)

    def test_normal_computation_works(self):
        r = run("from fractions import Fraction\nimport math, statistics, itertools, json, re, decimal, random\n"
                "print(sum(p for p in range(2, 1000) if all(p % d for d in range(2, int(p ** 0.5) + 1))))\n"
                "print(Fraction(1, 3) + Fraction(1, 6), statistics.mean([1, 2, 3]), math.isqrt(99))")
        self.assertEqual(r["exit"], 0, r["stderr"])
        self.assertEqual(r["stdout"].split(), ["76127", "1/2", "2", "9"])
        r = run("open('scratch.txt', 'w').write('ok')\nprint(open('scratch.txt').read())")   # its own folder is fine
        self.assertEqual(r["stdout"].strip(), "ok", r["stderr"])

    def test_family_files_are_unreadable(self):
        f = self.fam / "prepper.toml"
        attempts = [f"print(open({str(f)!r}).read())",
                    f"import io\nprint(io.open({str(f)!r}).read())",
                    f"import pathlib\nprint(pathlib.Path({str(f)!r}).read_text())",
                    f"import os\nfd = os.open({str(f)!r}, os.O_RDONLY)\nprint(os.read(fd, 100))",
                    f"import io\nprint(io.FileIO({str(f)!r}).read())",
                    f"import os\nprint(os.listdir({str(self.fam)!r}))",
                    f"import os\nprint(list(os.scandir({str(self.fam)!r})))",
                    f"import shutil\nshutil.copyfile({str(f)!r}, 'x')",
                    "print(open('/etc/prepper/prepper.toml').read())",
                    "print(open('/var/lib/prepper/portal/members.json').read())",
                    "import os\nprint(os.listdir('/var/lib/prepper'))",
                    "print(open('/etc/passwd').read())",
                    f"print(open({str(f)!r}, 'rb', buffering=0).read())",
                    f"import os\nos.symlink({str(f)!r}, 'link')\nprint(open('link').read())",
                    f"import os\nos.link({str(f)!r}, 'hard')\nprint(open('hard').read())",
                    f"import os\nos.chdir({str(self.fam)!r})\nprint(open('prepper.toml').read())",
                    f"print(open('../../../../../../../..{f}').read())"]
        for code in attempts:
            with self.subTest(code=code.splitlines()[-1][:60]):
                r = run(code)
                self.assertNotIn("SECRET", r["stdout"] + r["stderr"])
                self.assertTrue(blocked(r), r)

    def test_no_writes_outside_its_folder(self):
        target = self.fam / "planted.txt"
        for code in (f"open({str(target)!r}, 'w').write('x')", f"import os\nos.remove({str(self.fam / 'members.json')!r})",
                     f"import os\nos.mkdir({str(self.fam / 'd')!r})", f"import os\nos.rename('snippet.py', {str(target)!r})",
                     f"import os\nos.chmod({str(self.fam / 'members.json')!r}, 0o777)"):
            with self.subTest(code=code[:50]):
                self.assertTrue(blocked(run(code)))
        self.assertFalse(target.exists())
        self.assertTrue((self.fam / "members.json").exists())

    def test_no_network(self):
        for code in ("import socket\nsocket.create_connection(('127.0.0.1', 8000))",
                     "import _socket\n_socket.socket()",
                     "import importlib\nimportlib.import_module('socket')",
                     "import urllib.request\nurllib.request.urlopen('http://127.0.0.1:8000/api/members')",
                     "import http.client\nhttp.client.HTTPConnection('1.1.1.1').request('GET', '/')",
                     "import asyncio",
                     "import ssl",
                     "import smtplib",
                     "import sys, importlib.machinery as m, importlib.util as u, glob\n"
                     "p = [x for x in sys.path if 'dynload' in x][0]\nimport os\n"
                     "f = [n for n in os.listdir(p) if n.startswith('_socket')][0]\n"
                     "spec = u.spec_from_file_location('_socket', os.path.join(p, f))\nmod = u.module_from_spec(spec)\nprint(mod)"):
            with self.subTest(code=code.splitlines()[-1][:50]):
                r = run(code)
                self.assertNotEqual(r["exit"], 0, r)
                self.assertIn("not allowed", r["stderr"])

    def test_no_processes(self):
        for code in ("import os\nos.system('cat /etc/passwd')", "import subprocess\nsubprocess.run(['ls'])",
                     "import os\nos.execv('/bin/sh', ['sh'])", "import os\nos.posix_spawn('/bin/sh', ['sh'], {})",
                     "import os\nos.fork()", "import pty\npty.spawn('/bin/sh')", "import multiprocessing",
                     "import os\nos.kill(1, 9)", "import os\nos.killpg(0, 9)", "import ctypes\nctypes.CDLL(None)",
                     "import os\nos.popen('id').read()"):
            with self.subTest(code=code.splitlines()[-1][:50]):
                self.assertTrue(blocked(run(code)), code)

    def test_fork_bomb(self):
        t = time.monotonic()
        r = run("import os\nwhile True:\n    os.fork()")
        self.assertTrue(blocked(r))
        self.assertLess(time.monotonic() - t, 5)
        r = run("import threading\nts = [threading.Thread(target=lambda: None) for _ in range(5)]\n[t.start() for t in ts]\nprint('threads ok')")
        self.assertEqual(r["stdout"].strip(), "threads ok", r["stderr"])

    def test_cpu_limit(self):
        t = time.monotonic()
        r = run("while True:\n    pass", timeout=2)
        self.assertTrue(r["timed_out"])
        self.assertLess(time.monotonic() - t, 6)
        r = run("x = 0\nwhile True:\n    x += 1", timeout=30)                     # capped at 10 s whatever is asked
        self.assertTrue(r["timed_out"])
        self.assertLess(r["seconds"], 12)

    @unittest.skipIf(sys.platform == "darwin", "macOS does not enforce RLIMIT_AS; the Badger (Linux) does")
    def test_memory_limit(self):
        r = run("x = bytearray(2 * 1024 ** 3)\nprint('allocated')")
        self.assertNotIn("allocated", r["stdout"])
        self.assertIn("MemoryError", r["stderr"])

    def test_output_flood_is_capped(self):
        r = run("import sys\nfor _ in range(10 ** 6):\n    sys.stdout.write('x' * 1000)")
        self.assertLessEqual(len(r["stdout"]), sandbox_child.OUT_CAP)
        self.assertTrue(r["truncated"] or r["exit"] != 0)

    def test_crafted_bytecode_and_introspection(self):
        for code in ("import types\ntypes.CodeType(0,0,0,0,0,0,b'',(),(),(),'','',0,b'',b'',(),())",
                     "import marshal\nmarshal.loads(b'\\xe3')", "import gc\ngc.get_objects()",
                     "import sys\nsys.addaudithook(lambda *a: None)\nopen('/etc/passwd')"):
            with self.subTest(code=code.splitlines()[-1][:40]):
                r = run(code)
                self.assertNotEqual(r["exit"], 0)

    def test_empty_environment(self):
        os.environ["PORTAL_SECRET_TEST"] = "SECRET-ENV"
        try:
            r = run("import os\nprint(dict(os.environ))")
        finally:
            del os.environ["PORTAL_SECRET_TEST"]
        self.assertNotIn("SECRET-ENV", r["stdout"])
        self.assertNotIn("HOME", r["stdout"])

    def test_request_validation(self):
        self.assertFalse(sandbox_child.run_request({"op": "run", "code": "x" * (sandbox_child.MAX_CODE + 1)})["ok"])
        self.assertFalse(sandbox_child.run_request({"op": "run", "code": ""})["ok"])
        self.assertFalse(sandbox_child.run_request({"op": "rm -rf"})["ok"])
        self.assertFalse(sandbox_child.run_request(["op"])["ok"])
        self.assertTrue(sandbox_child.run_request({"op": "run", "code": "print(1)", "timeout": True})["ok"])


GOOD = {"uid": 61234, "euid": 61234, "etc_prepper": "blocked", "prepper_toml": "blocked", "var_lib_prepper": "blocked",
        "members": "blocked", "data_root": "blocked", "inet": "blocked", "inet6": "blocked", "portal_reachable": False,
        "no_new_privs": True}


class ProbeVerdict(unittest.TestCase):
    def test_good_probe_passes(self):
        self.assertEqual(sandbox.verify_probe(GOOD, portal_uid=1001), (True, "isolated"))

    def test_every_weakness_fails_closed(self):
        bad = [({"etc_prepper": "absent"}, "/etc/prepper"), ({"etc_prepper": "readable"}, "/etc/prepper"),
               ({"var_lib_prepper": "error 5"}, "/var/lib/prepper"), ({"members": "readable"}, "members"),
               ({"prepper_toml": "readable"}, "prepper.toml"), ({"data_root": "readable"}, "/srv"),
               ({"inet": "allowed"}, "network"), ({"inet6": "allowed"}, "network"), ({"portal_reachable": True}, "portal"),
               ({"uid": 0, "euid": 0}, "unprivileged"), ({"uid": 1001}, "unprivileged"), ({"uid": "61234"}, "unprivileged"),
               ({"euid": 0}, "unprivileged"), ({"no_new_privs": None}, "privileges"), ({"no_new_privs": False}, "privileges")]
        for change, frag in bad:
            with self.subTest(change=change):
                ok, why = sandbox.verify_probe({**GOOD, **change}, portal_uid=1001)
                self.assertFalse(ok)
                self.assertIn(frag, why)
        for junk in (None, [], "isolated", {}):
            self.assertFalse(sandbox.verify_probe(junk, portal_uid=1001)[0])

    def test_unsandboxed_host_probe_fails(self):
        """The runner's probe, run directly on this machine (no OS sandbox), must never pass."""
        self.assertFalse(sandbox.verify_probe(sandbox_child.probe())[0])


class AcceptServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Emulates the systemd socket unit (Accept=yes): one sandbox_child process per connection —
    but WITHOUT the OS isolation, like a misconfigured or non-Linux host."""
    daemon_threads = True


class Handler(socketserver.BaseRequestHandler):
    reply = None

    def handle(self):
        if Handler.reply is not None:
            try:
                self.request.recv(65536)
                self.request.sendall(Handler.reply)
            except OSError:
                pass                                  # the client hung up on an oversized reply: expected
            return
        subprocess.run([sys.executable, "-I", "-S", "-B", str(CHILD)], stdin=self.request.fileno(),
                       stdout=self.request.fileno(), timeout=30, check=False)


class Client(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="sbx"))
        cls.sock = str(cls.dir / "s.sock")
        cls.srv = AcceptServer(cls.sock, Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        Handler.reply = None

    def test_unisolated_host_disables_the_runner(self):
        c = sandbox.SandboxClient(self.sock, verify=True)
        ok, why = c.status(refresh=True)
        self.assertFalse(ok)
        with self.assertRaises(sandbox.SandboxUnavailable):
            c.run("print('should never run')")
        ctx = tools.ToolContext(sandbox=c if c.available() else None)
        self.assertNotIn("python", tools.available(ctx))
        ok, text = tools.run_tool(ctx, "python", {"code": "print(1)"})
        self.assertFalse(ok)

    def test_missing_socket(self):
        c = sandbox.SandboxClient(str(self.dir / "nope.sock"))
        self.assertEqual(c.status(), (False, "the sandbox service isn't installed"))

    def test_status_is_cached_and_refreshable(self):
        now = [1000.0]
        c = sandbox.SandboxClient(self.sock, verify=True, clock=lambda: now[0], probe_ttl=600)
        self.assertFalse(c.status()[0])
        Handler.reply = b'{"ok": true, "probe": ' + __import__("json").dumps(GOOD).encode() + b"}\n"
        self.assertFalse(c.status()[0])                       # cached
        now[0] += 601
        ok, why = c.status()
        self.assertEqual(ok, os.getuid() != 61234, why)      # GOOD probe now passes (its uid differs from ours)

    def test_bad_replies(self):
        c = sandbox.SandboxClient(self.sock, verify=True, timeout=2)
        for reply in (b"", b"not json\n", b"[1,2]\n", b"x" * 300000):
            with self.subTest(reply=reply[:10]):
                Handler.reply = reply
                self.assertFalse(c.status(refresh=True)[0])

    def test_run_through_the_socket_inner_layer(self):
        # verify=False exists only for this: the inner layer through the real socket protocol.
        c = sandbox.SandboxClient(self.sock, verify=False)
        r = c.run("print(6 * 7)")
        self.assertEqual(r["stdout"].strip(), "42")
        r = c.run("print(open('/etc/passwd').read())")
        self.assertIn("not allowed", r["stderr"])
        ctx = tools.ToolContext(sandbox=c)
        ok, text = tools.run_tool(ctx, "python", {"code": "print(sum(range(10)))"})
        self.assertTrue(ok)
        self.assertIn('"stdout":"45\\n"', text)
        with self.assertRaises(sandbox.SandboxUnavailable):
            c.run("x" * 40000)

    def test_feature_always_verifies(self):
        src = (REPO / "software" / "portal" / "features" / "solver.py").read_text()
        self.assertIn("SandboxClient(c[\"sandbox_socket\"], verify=True)", src)
        self.assertNotIn("verify=False", src)


REAL_SOCKET = Path(sandbox.DEFAULT_SOCKET)


@unittest.skipUnless(sys.platform.startswith("linux") and REAL_SOCKET.exists(),
                     "only on a Badger with prepper-solver-sandbox.socket installed")
class RealOSLayer(unittest.TestCase):
    """On the Pi: the systemd sandbox itself (run as the portal's user, prepper)."""
    def setUp(self):
        self.c = sandbox.SandboxClient(str(REAL_SOCKET), verify=True)

    def test_probe_proves_isolation(self):
        ok, why = self.c.status(refresh=True)
        self.assertTrue(ok, why)

    def test_os_layer_blocks_files_and_network_even_without_the_audit_hook(self):
        p = self.c._request({"op": "probe"})["probe"]
        self.assertEqual(p["etc_prepper"], "blocked")
        self.assertEqual(p["var_lib_prepper"], "blocked")
        self.assertEqual(p["inet"], "blocked")
        self.assertFalse(p["portal_reachable"])
        self.assertNotEqual(p["uid"], os.getuid())

    def test_fork_bomb_and_memory_are_contained(self):
        r = self.c.run("x = bytearray(2 * 1024 ** 3)\nprint('allocated')")
        self.assertNotIn("allocated", r.get("stdout", ""))
        t = time.monotonic()
        self.c.run("while True: pass")
        self.assertLess(time.monotonic() - t, 30)
        self.assertTrue(self.c.status(refresh=True)[0])          # the service is fine afterwards


if __name__ == "__main__":
    unittest.main()
