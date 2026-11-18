#!/usr/bin/env python3
"""
File-sharing client.

  python3 client/client.py --user alice --password alice123 list
  python3 client/client.py --user alice --password alice123 upload ./report.pdf
  python3 client/client.py --user alice --password alice123 download report.pdf -o /tmp/r.pdf
  python3 client/client.py --user alice --password alice123 info report.pdf
  python3 client/client.py --user alice --password alice123 delete report.pdf
  python3 client/client.py --user alice --password alice123 shell

Downloads resume automatically (from the bytes already received) when the
connection drops, the server is busy, or the network stalls; the result is
verified with SHA-256. `--json` prints a machine-readable result line used by
the experiment scripts.
"""
import argparse
import hashlib
import json
import os
import shlex
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import netconfig as NC
from common.protocol import send_msg, recv_msg, stream_in, stream_out, CHUNK


class ServerBusy(Exception):
    pass


class RequestFailed(Exception):
    """Server answered with an application error (not retryable)."""


RETRYABLE = (ServerBusy, ConnectionError, socket.timeout, TimeoutError, OSError)


def sha256_file(path, upto=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            data = f.read(CHUNK)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


class FileClient:
    def __init__(self, host, port, user, password, timeout=10.0, retries=5, verbose=False):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.timeout = timeout
        self.retries = retries
        self.verbose = verbose
        self.sock = None
        self.role = None
        self.reconnects = 0
        self.busy_count = 0

    def _say(self, *a):
        if self.verbose:
            print("[client]", *a, file=sys.stderr)

    # ------------------------------------------------------------ connection
    def connect(self):
        self.close()
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = s
        send_msg(s, {"op": "AUTH", "user": self.user, "password": self.password})
        r = recv_msg(s)
        if r.get("status") == "BUSY":
            self.busy_count += 1
            self.close()
            raise ServerBusy(r.get("retry_after", 1.0))
        if r.get("status") != "OK":
            self.close()
            raise RequestFailed(r.get("error", "authentication failed"))
        self.role = r.get("role")
        return self

    def close(self):
        if self.sock:
            try:
                send_msg(self.sock, {"op": "QUIT"})
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _with_retries(self, fn):
        delay = 0.5
        for attempt in range(self.retries + 1):
            try:
                if self.sock is None:
                    self.connect()
                return fn()
            except RequestFailed:
                raise
            except RETRYABLE as e:
                if self.sock is not None:
                    try:
                        self.sock.close()
                    except OSError:
                        pass
                self.sock = None
                if attempt == self.retries:
                    raise
                wait = e.args[0] if isinstance(e, ServerBusy) and e.args else delay
                self._say("attempt %d failed (%s: %s) - retrying in %.1fs"
                          % (attempt + 1, type(e).__name__, e, wait))
                self.reconnects += 1
                time.sleep(wait)
                delay = min(delay * 2, 5.0)

    def _request(self, msg):
        def go():
            send_msg(self.sock, msg)
            r = recv_msg(self.sock)
            if r.get("status") != "OK":
                raise RequestFailed(r.get("error", "request failed"))
            return r
        return self._with_retries(go)

    # ------------------------------------------------------------ operations
    def ping(self):
        return self._request({"op": "PING"})

    def list(self):
        return self._request({"op": "LIST"})["files"]

    def info(self, name):
        return self._request({"op": "INFO", "name": name})["file"]

    def delete(self, name):
        return self._request({"op": "DELETE", "name": name})

    def upload(self, path, name=None):
        name = name or os.path.basename(path)
        size = os.path.getsize(path)
        digest = sha256_file(path)

        def go():
            send_msg(self.sock, {"op": "UPLOAD", "name": name, "size": size, "sha256": digest})
            r = recv_msg(self.sock)
            if r.get("status") != "READY":
                raise RequestFailed(r.get("error", "upload refused"))
            with open(path, "rb") as f:
                stream_out(self.sock, f, size)
            # sendall() returns once the data is in kernel/queue buffers; on a slow,
            # shaped link several MB can still be in flight. Allow time for them to
            # drain (assume >= 1 Mbps) before treating the missing ack as a stall.
            self.sock.settimeout(self.timeout + size * 8 / 1e6)
            r = recv_msg(self.sock)
            self.sock.settimeout(self.timeout)
            if r.get("status") != "OK":
                raise RequestFailed(r.get("error", "upload failed"))
            return r

        t0 = time.time()
        self._with_retries(go)
        return {"op": "upload", "file": name, "bytes": size, "start": t0, "end": time.time()}

    def download(self, name, out_path):
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        part = out_path + ".part"
        if os.path.exists(part):
            os.remove(part)
        state = {"total": None, "sha": None, "max_gap": 0.0, "last": None}

        def mark(_n):
            now = time.time()
            if state["last"] is not None:
                state["max_gap"] = max(state["max_gap"], now - state["last"])
            state["last"] = now

        def go():
            offset = os.path.getsize(part) if os.path.exists(part) else 0
            if offset:
                self._say("resuming %s at byte %d" % (name, offset))
            send_msg(self.sock, {"op": "DOWNLOAD", "name": name, "offset": offset})
            r = recv_msg(self.sock)
            if r.get("status") != "OK":
                raise RequestFailed(r.get("error", "download failed"))
            if state["sha"] and state["sha"] != r["sha256"]:
                raise RequestFailed("file changed on server during resume")
            state["total"], state["sha"] = r["size"], r["sha256"]
            with open(part, "ab") as f:
                stream_in(self.sock, f, r["length"], on_chunk=mark)

        t0 = time.time()
        state["last"] = t0
        self._with_retries(go)
        if sha256_file(part) != state["sha"]:
            os.remove(part)
            raise RequestFailed("SHA-256 mismatch after download")
        os.replace(part, out_path)
        return {"op": "download", "file": name, "bytes": state["total"], "start": t0,
                "end": time.time(), "max_gap_s": round(state["max_gap"], 3), "sha256_ok": True}


def local_ip(host, port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, port))
            return s.getsockname()[0]
    except OSError:
        return "local"


def run_command(c, cmd, args, out_dir):
    if cmd == "ping":
        return c.ping()
    if cmd == "list":
        return {"files": c.list()}
    if cmd == "info":
        return c.info(args[0])
    if cmd == "delete":
        return c.delete(args[0])
    if cmd == "upload":
        return c.upload(args[0], args[1] if len(args) > 1 else None)
    if cmd == "download":
        out = args[1] if len(args) > 1 else os.path.join(out_dir, args[0])
        return c.download(args[0], out)
    raise RequestFailed("unknown command %s" % cmd)


def print_human(cmd, res):
    if cmd == "list":
        files = res["files"]
        print("%-28s %12s  %-8s %-19s %4s %5s" % ("NAME", "SIZE", "OWNER", "UPLOADED", "VER", "DLS"))
        for f in files:
            print("%-28s %12d  %-8s %-19s %4d %5d" % (f["name"], f["size"], f["owner"], f["uploaded_at"],
                                                    f["version"], f["downloads"]))
        print("%d file(s)" % len(files))
    elif cmd in ("upload", "download"):
        dt = res["end"] - res["start"]
        print("%s %s: %d bytes in %.2fs = %.2f Mbps" % (cmd, res["file"], res["bytes"], dt,
                                                       res["bytes"] * 8 / max(dt, 1e-6) / 1e6))
    else:
        print(json.dumps(res, indent=2))


def shell(c, out_dir):
    print("connected as %s (%s). commands: list | info N | upload PATH [N] | download N [OUT] | delete N | quit"
          % (c.user, c.role))
    while True:
        try:
            line = input("fs> ").strip()
        except EOFError:
            break
        if not line:
            continue
        parts = shlex.split(line)
        if parts[0] in ("quit", "exit"):
            break
        try:
            print_human(parts[0], run_command(c, parts[0], parts[1:], out_dir))
        except (RequestFailed, IndexError) as e:
            print("error:", e)
        except RETRYABLE as e:
            print("network error:", e)


def main():
    ap = argparse.ArgumentParser(description="SDN file-sharing client")
    ap.add_argument("--host", default=NC.SERVER_IP)
    ap.add_argument("--port", type=int, default=NC.SERVER_PORT)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--timeout", type=float, default=10.0, help="socket timeout / stall detection (s)")
    ap.add_argument("--retries", type=int, default=5)
    ap.add_argument("--out-dir", default=None, help="download directory (default downloads/<client-ip>)")
    ap.add_argument("--json", action="store_true", help="print one JSON result line")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("command", choices=["ping", "list", "info", "upload", "download", "delete", "shell"])
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = a.out_dir or os.path.join(root, "downloads", local_ip(a.host, a.port))
    c = FileClient(a.host, a.port, a.user, a.password, a.timeout, a.retries, a.verbose)
    result = {"ok": False, "command": a.command, "client": local_ip(a.host, a.port), "user": a.user}
    t0 = time.time()
    try:
        if a.command == "shell":
            c._with_retries(lambda: None)
            shell(c, out_dir)
            return
        res = run_command(c, a.command, a.args, out_dir)
        result.update(res if isinstance(res, dict) else {"result": res})
        result["ok"] = True
        if not a.json:
            print_human(a.command, res)
    except RequestFailed as e:
        result["error"] = str(e)
        if not a.json:
            print("error:", e, file=sys.stderr)
    except RETRYABLE as e:
        result["error"] = "%s: %s" % (type(e).__name__, e)
        if not a.json:
            print("network error:", result["error"], file=sys.stderr)
    finally:
        c.close()
        result.update(reconnects=c.reconnects, busy=c.busy_count,
                      elapsed_s=round(time.time() - t0, 3))
        if a.json:
            print(json.dumps(result))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
