#!/usr/bin/env python3
"""
Concurrent TCP file server.

  * thread-per-connection with a bounded number of concurrent sessions
    (extra clients get BUSY and retry -> server-overload scenario)
  * authenticated sessions (PBKDF2 hashed passwords, roles admin/user/readonly)
  * upload / download (with resume offset) / list / info / delete
  * SHA-256 integrity, atomic uploads, persistent JSON metadata
  * reports auth and transfer events to the SDN controller

Run inside Mininet on host `srv`:
    python3 server/file_server.py
"""
import argparse
import hashlib
import logging
import os
import signal
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from common import netconfig as NC
from common.protocol import send_msg, recv_msg, stream_in, stream_out, ProtocolError
from auth import UserStore
from metadata import MetadataStore, MetadataError
from controller_link import ControllerNotifier

log = logging.getLogger("server")
MAX_FILE_BYTES = 512 * 1024 * 1024


class FileServer:
    def __init__(self, host, port, storage, users_file, max_clients, notifier, idle_timeout=30):
        self.addr = (host, port)
        self.store = MetadataStore(storage)
        self.users = UserStore(users_file)
        self.slots = threading.BoundedSemaphore(max_clients)
        self.max_clients = max_clients
        self.notifier = notifier
        self.idle_timeout = idle_timeout
        self.active = 0
        self.stats = {"sessions": 0, "busy_rejects": 0, "auth_fail": 0, "bytes_in": 0, "bytes_out": 0}
        self.stats_lock = threading.Lock()
        self.sock = None
        self.running = threading.Event()

    # ------------------------------------------------------------ lifecycle
    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(self.addr)
        self.sock.listen(128)
        self.addr = self.sock.getsockname()
        self.running.set()
        log.info("file server listening on %s:%d (max %d concurrent sessions)",
                 self.addr[0], self.addr[1], self.max_clients)

    def serve_forever(self):
        while self.running.is_set():
            try:
                conn, peer = self.sock.accept()
            except OSError:
                break
            if not self.slots.acquire(blocking=False):
                self._bump("busy_rejects")
                log.warning("BUSY: rejecting %s:%d (%d sessions active)", peer[0], peer[1], self.active)
                threading.Thread(target=self._reject_busy, args=(conn,), daemon=True).start()
                continue
            threading.Thread(target=self._session, args=(conn, peer), daemon=True).start()

    @staticmethod
    def _reject_busy(conn):
        # Read the client's AUTH frame first: closing a socket with unread data
        # sends a TCP RST, which could destroy the BUSY reply before it is read.
        try:
            conn.settimeout(2)
            recv_msg(conn)
            send_msg(conn, {"status": "BUSY", "retry_after": 1.0})
            conn.shutdown(socket.SHUT_WR)
            conn.recv(1)
        except (OSError, ProtocolError):
            pass
        finally:
            conn.close()

    def stop(self):
        self.running.clear()
        try:
            self.sock.close()
        except OSError:
            pass

    def _bump(self, key, n=1):
        with self.stats_lock:
            self.stats[key] += n

    # ------------------------------------------------------------ session
    def _session(self, conn, peer):
        with self.stats_lock:
            self.active += 1
            self.stats["sessions"] += 1
        conn.settimeout(self.idle_timeout)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        ip, port = peer[0], peer[1]
        user = role = None
        try:
            req = recv_msg(conn)
            if req.get("op") != "AUTH":
                send_msg(conn, {"status": "ERR", "error": "authenticate first"})
                return
            role = self.users.verify(str(req.get("user", "")), str(req.get("password", "")))
            if role is None:
                self._bump("auth_fail")
                log.warning("AUTH FAIL user=%r from %s", req.get("user"), ip)
                self.notifier.event("auth_fail", client_ip=ip, client_port=port, user=req.get("user"))
                send_msg(conn, {"status": "ERR", "error": "authentication failed"})
                return
            user = req["user"]
            self.notifier.event("auth_ok", client_ip=ip, client_port=port, user=user)
            send_msg(conn, {"status": "OK", "role": role, "user": user})
            log.info("session start user=%s role=%s from %s:%d", user, role, ip, port)

            while True:
                req = recv_msg(conn)
                op = req.get("op")
                handler = getattr(self, "_op_" + str(op).lower(), None)
                if op == "QUIT":
                    send_msg(conn, {"status": "OK"})
                    break
                if handler is None:
                    send_msg(conn, {"status": "ERR", "error": "unknown op %r" % op})
                    continue
                handler(conn, req, user, role, ip, port)
        except (ConnectionError, socket.timeout, ProtocolError, OSError) as e:
            log.info("session %s:%d ended: %s", ip, port, e)
        finally:
            conn.close()
            with self.stats_lock:
                self.active -= 1
            self.slots.release()

    # ------------------------------------------------------------ operations
    def _op_ping(self, conn, req, user, role, ip, port):
        send_msg(conn, {"status": "OK", "pong": True, "active_sessions": self.active})

    def _op_list(self, conn, req, user, role, ip, port):
        send_msg(conn, {"status": "OK", "files": self.store.list()})

    def _op_info(self, conn, req, user, role, ip, port):
        m = self.store.get(req.get("name"))
        send_msg(conn, {"status": "OK", "file": m} if m else {"status": "ERR", "error": "no such file"})

    def _op_delete(self, conn, req, user, role, ip, port):
        if role == "readonly":
            return send_msg(conn, {"status": "ERR", "error": "permission denied (readonly)"})
        try:
            self.store.delete(req.get("name"), user, role)
            log.info("DELETE %s by %s", req.get("name"), user)
            send_msg(conn, {"status": "OK"})
        except (MetadataError, OSError) as e:
            send_msg(conn, {"status": "ERR", "error": str(e)})

    def _op_upload(self, conn, req, user, role, ip, port):
        name, size = req.get("name"), req.get("size")
        if role == "readonly":
            return send_msg(conn, {"status": "ERR", "error": "permission denied (readonly)"})
        if not isinstance(size, int) or size < 0 or size > MAX_FILE_BYTES:
            return send_msg(conn, {"status": "ERR", "error": "invalid size"})
        try:
            tmp = self.store.begin_upload(name, user, role)
        except MetadataError as e:
            return send_msg(conn, {"status": "ERR", "error": str(e)})
        hasher = hashlib.sha256()
        t0 = time.time()
        ok = False
        try:
            send_msg(conn, {"status": "READY"})
            self.notifier.event("transfer_start", client_ip=ip, client_port=port, user=user,
                                direction="upload", file=name, size=size)
            with open(tmp, "wb") as f:
                stream_in(conn, f, size, hasher, on_chunk=lambda n: self._bump("bytes_in", n))
            digest = hasher.hexdigest()
            expected = req.get("sha256")
            if expected and expected != digest:
                self.store.abort_upload(name, tmp)
                return send_msg(conn, {"status": "ERR", "error": "checksum mismatch"})
            try:
                self.store.commit_upload(name, tmp, size, digest, user)
            except MetadataError as e:          # a newer attempt of this upload took over
                log.info("UPLOAD %s by %s discarded: %s", name, user, e)
                return send_msg(conn, {"status": "ERR", "error": str(e)})
            ok = True
            dt = time.time() - t0
            log.info("UPLOAD %s %d bytes by %s in %.2fs (%.2f Mbps)", name, size, user, dt,
                     size * 8 / max(dt, 1e-6) / 1e6)
            send_msg(conn, {"status": "OK", "sha256": digest, "size": size})
        finally:
            if not ok:
                self.store.abort_upload(name, tmp)
            self.notifier.event("transfer_end", client_ip=ip, client_port=port, file=name,
                                direction="upload", bytes=size if ok else 0, ok=ok)

    def _op_download(self, conn, req, user, role, ip, port):
        name = req.get("name")
        m = self.store.get(name)
        if not m:
            return send_msg(conn, {"status": "ERR", "error": "no such file"})
        offset = req.get("offset", 0)
        if not isinstance(offset, int) or offset < 0 or offset > m["size"]:
            return send_msg(conn, {"status": "ERR", "error": "invalid offset"})
        try:
            f = open(self.store.path(name), "rb")
        except OSError as e:
            return send_msg(conn, {"status": "ERR", "error": str(e)})
        length = m["size"] - offset
        t0 = time.time()
        ok = False
        with f:
            f.seek(offset)
            send_msg(conn, {"status": "OK", "size": m["size"], "sha256": m["sha256"],
                            "offset": offset, "length": length})
            self.notifier.event("transfer_start", client_ip=ip, client_port=port, user=user,
                                direction="download", file=name, size=length)
            try:
                stream_out(conn, f, length, on_chunk=lambda n: self._bump("bytes_out", n))
                ok = True
            finally:
                self.notifier.event("transfer_end", client_ip=ip, client_port=port, file=name,
                                    direction="download", bytes=length if ok else 0, ok=ok)
        self.store.record_download(name)
        dt = time.time() - t0
        log.info("DOWNLOAD %s [%d..] %d bytes to %s in %.2fs (%.2f Mbps)", name, offset, length, user,
                 dt, length * 8 / max(dt, 1e-6) / 1e6)


def main():
    ap = argparse.ArgumentParser(description="Concurrent TCP file-sharing server")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=NC.SERVER_PORT)
    ap.add_argument("--storage", default=os.path.join(ROOT, "storage"))
    ap.add_argument("--users", default=os.path.join(ROOT, "config", "users.json"))
    ap.add_argument("--max-clients", type=int, default=8)
    ap.add_argument("--idle-timeout", type=float, default=30)
    ap.add_argument("--controller", default=NC.MGMT_ROOT_IP,
                    help="controller event socket address (mgmt network)")
    ap.add_argument("--controller-port", type=int, default=NC.CONTROLLER_EVENT_PORT)
    ap.add_argument("--no-controller", action="store_true", help="do not report events to SDN")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not os.path.exists(a.users):
        sys.exit("users file %s missing - run: python3 tools/manage_users.py init-demo" % a.users)
    notifier = ControllerNotifier(a.controller, a.controller_port, enabled=not a.no_controller)
    srv = FileServer(a.host, a.port, a.storage, a.users, a.max_clients, notifier, a.idle_timeout)
    srv.start()
    signal.signal(signal.SIGTERM, lambda *_: srv.stop())
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
        log.info("server stopped. stats=%s", srv.stats)


if __name__ == "__main__":
    main()
