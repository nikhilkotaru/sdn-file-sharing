"""
Socket-level tests of the file server + client (no Mininet needed).

    python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.join(ROOT, "client"))

from auth import UserStore
from file_server import FileServer
from controller_link import ControllerNotifier
from client import FileClient, RequestFailed, ServerBusy


class FakeController:
    """Captures JSON-line events like the real SDN controller's control socket."""

    def __init__(self):
        self.events = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _client(self, c):
        buf = b""
        while True:
            d = c.recv(4096)
            if not d:
                return
            buf += d
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self.events.append(json.loads(line))
                c.sendall(b'{"status":"OK"}\n')

    def types(self):
        return [e["type"] for e in self.events]


class AppTest(unittest.TestCase):
    max_clients = 8

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        users = os.path.join(self.tmp, "users.json")
        us = UserStore(users)
        us.add("alice", "alice123", "user")
        us.add("bob", "bob123", "user")
        us.add("admin", "admin123", "admin")
        us.add("guest", "guest123", "readonly")
        self.ctrl = FakeController()
        notifier = ControllerNotifier("127.0.0.1", self.ctrl.port)
        self.server = FileServer("127.0.0.1", 0, os.path.join(self.tmp, "storage"), users,
                                 self.max_clients, notifier, idle_timeout=5)
        self.server.start()
        self.port = self.server.addr[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.stop()
        self.ctrl.sock.close()
        time.sleep(0.1)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self, user="alice", pw=None, **kw):
        return FileClient("127.0.0.1", self.port, user, pw or user + "123", timeout=5, **kw)

    def make(self, name, size):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(os.urandom(size))
        return p

    def wait_events(self, n, timeout=3):
        end = time.time() + timeout
        while len(self.ctrl.events) < n and time.time() < end:
            time.sleep(0.05)


class TestBasic(AppTest):
    def test_auth_fail_reported_to_controller(self):
        with self.assertRaises(RequestFailed):
            self.client(pw="wrong", retries=0).connect()
        self.wait_events(1)
        self.assertIn("auth_fail", self.ctrl.types())
        self.assertEqual(self.ctrl.events[0]["client_ip"], "127.0.0.1")

    def test_upload_download_list_integrity(self):
        src = self.make("a.bin", 3 * 1024 * 1024 + 17)
        c = self.client()
        c.upload(src, "a.bin")
        files = c.list()
        self.assertEqual(files[0]["name"], "a.bin")
        self.assertEqual(files[0]["owner"], "alice")
        out = os.path.join(self.tmp, "out", "a.bin")
        r = self.client("bob").download("a.bin", out)
        self.assertTrue(r["sha256_ok"])
        with open(src, "rb") as f1, open(out, "rb") as f2:
            self.assertEqual(f1.read(), f2.read())
        self.assertEqual(self.client().info("a.bin")["downloads"], 1)
        self.wait_events(6)
        self.assertIn("transfer_start", self.ctrl.types())
        self.assertIn("transfer_end", self.ctrl.types())

    def test_roles_and_ownership(self):
        src = self.make("b.bin", 1000)
        self.client().upload(src, "b.bin")
        with self.assertRaises(RequestFailed):
            self.client("guest").upload(src, "g.bin")
        with self.assertRaises(RequestFailed):
            self.client("bob").delete("b.bin")
        with self.assertRaises(RequestFailed):
            self.client("bob").upload(src, "b.bin")      # cannot overwrite alice's file
        self.client("admin").delete("b.bin")
        self.assertEqual(self.client().list(), [])

    def test_bad_names_rejected(self):
        src = self.make("c.bin", 10)
        for bad in ("../x", "a/b", ".hidden", "a" * 200):
            with self.assertRaises(RequestFailed):
                self.client().upload(src, bad)

    def test_unknown_file(self):
        with self.assertRaises(RequestFailed):
            self.client().download("nope", os.path.join(self.tmp, "nope"))

    def test_metadata_persists_across_restart(self):
        self.client().upload(self.make("p.bin", 500), "p.bin")
        from metadata import MetadataStore
        st = MetadataStore(os.path.join(self.tmp, "storage"))
        self.assertEqual(st.get("p.bin")["size"], 500)

    def test_resume_offset(self):
        src = self.make("r.bin", 200000)
        self.client().upload(src, "r.bin")
        out = os.path.join(self.tmp, "r.out")
        # simulate a previous partial download then resume via raw protocol
        from common.protocol import send_msg, recv_msg, recv_exact
        c = self.client().connect()
        send_msg(c.sock, {"op": "DOWNLOAD", "name": "r.bin", "offset": 150000})
        r = recv_msg(c.sock)
        self.assertEqual(r["length"], 50000)
        data = recv_exact(c.sock, 50000)
        with open(src, "rb") as f:
            f.seek(150000)
            self.assertEqual(f.read(), data)


class TestConcurrency(AppTest):
    def test_parallel_uploads_and_downloads(self):
        srcs = [self.make("f%d.bin" % i, 512 * 1024 + i) for i in range(6)]
        errors = []

        def up(i):
            try:
                self.client(["alice", "bob"][i % 2]).upload(srcs[i], "f%d.bin" % i)
            except Exception as e:
                errors.append(e)
        ts = [threading.Thread(target=up, args=(i,)) for i in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(len(self.client().list()), 6)

        def down(i):
            try:
                out = os.path.join(self.tmp, "dl", "%d.bin" % i)
                self.client("bob").download("f%d.bin" % (i % 6), out)
            except Exception as e:
                errors.append(e)
        ts = [threading.Thread(target=down, args=(i,)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])

    def test_same_name_upload_conflict(self):
        """Two simultaneous uploads of the same name: one must be refused, data never mixed."""
        big = self.make("big.bin", 4 * 1024 * 1024)
        results = []

        def up(user):
            try:
                self.client(user).upload(big, "shared.bin")
                results.append("ok")
            except RequestFailed as e:
                results.append(str(e))
        ts = [threading.Thread(target=up, args=("admin",)) for _ in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertIn("ok", results)
        out = os.path.join(self.tmp, "shared.out")
        self.assertTrue(self.client().download("shared.bin", out)["sha256_ok"])


class TestOverload(AppTest):
    max_clients = 2

    def test_busy_then_retry(self):
        held = [self.client().connect(), self.client().connect()]
        with self.assertRaises(ServerBusy):
            self.client(retries=0).connect()
        c = self.client(retries=6)
        threading.Timer(0.7, lambda: [h.close() for h in held]).start()
        self.assertTrue(c.ping()["pong"])
        self.assertGreaterEqual(c.busy_count, 1)


class TestNoController(unittest.TestCase):
    def test_server_works_without_controller(self):
        tmp = tempfile.mkdtemp()
        try:
            users = os.path.join(tmp, "u.json")
            UserStore(users).add("alice", "alice123")
            srv = FileServer("127.0.0.1", 0, os.path.join(tmp, "s"), users, 4,
                             ControllerNotifier("127.0.0.1", 1))   # nothing listening
            srv.start()
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            c = FileClient("127.0.0.1", srv.addr[1], "alice", "alice123", timeout=5)
            self.assertTrue(c.ping()["pong"])
            srv.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
