#!/usr/bin/env python3
"""
SDN File-Sharing Demo Console - web GUI.

    sudo python3 gui/server.py            # then open http://localhost:8080

One process does everything: starts the SDN controller (if it is not already
running), builds the Mininet topology, starts the file server on `srv`, and
serves a dashboard where every deliverable can be demonstrated with one click.
Quick experiment results are written to results/quick/ (the full results from
experiments/run_experiments.py in results/ are left untouched).
"""
import argparse
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, os.path.join(ROOT, "topology"), os.path.join(ROOT, "controller"),
          os.path.join(ROOT, "experiments")):
    sys.path.insert(0, p)

from mininet.log import setLogLevel                      # noqa: E402
from common import netconfig as NC                       # noqa: E402
import topo                                              # noqa: E402
from ctl import call as ctl_call                         # noqa: E402
import run_experiments as X                              # noqa: E402

STATIC = os.path.join(HERE, "static")
RESULTS = os.path.join(ROOT, "results")
QUICK = os.path.join(RESULTS, "quick")
X.RESULTS = QUICK                       # helpers in run_experiments save here

NET = None
CTRL_PROC = None
NET_LOCK = threading.RLock()            # Mininet node.cmd() is not thread-safe
JOB_LOCK = threading.Lock()             # one scenario at a time
JOBS = {}
CURRENT = {"job": None}


# ============================================================================ jobs
class Job:
    def __init__(self, scenario, title):
        self.id = uuid.uuid4().hex[:8]
        self.scenario, self.title = scenario, title
        self.status = "running"
        self.lines, self.checks, self.metrics = [], [], {}
        self.started, self.ended = time.time(), None

    def _add(self, kind, text):
        self.lines.append({"t": round(time.time() - self.started, 2), "kind": kind, "text": str(text)})

    def log(self, text):
        self._add("info", text)

    def cmd(self, host, text):
        self._add("cmd", "%s $ %s" % (host, text))

    def out(self, text):
        for line in str(text).rstrip().splitlines():
            self._add("out", line)

    def check(self, name, passed, detail=""):
        self.checks.append({"name": name, "passed": bool(passed), "detail": str(detail)})
        self._add("pass" if passed else "fail", ("PASS  " if passed else "FAIL  ") + name +
                  (" - %s" % detail if detail else ""))
        return passed

    def view(self, since=0):
        return {"id": self.id, "scenario": self.scenario, "title": self.title, "status": self.status,
                "lines": self.lines[since:], "next": len(self.lines), "checks": self.checks,
                "metrics": self.metrics, "started": self.started, "ended": self.ended,
                "elapsed": round((self.ended or time.time()) - self.started, 1)}


def run_job(job, fn):
    try:
        fn(job)
        job.status = "passed" if all(c["passed"] for c in job.checks) else "failed"
    except Exception as e:                     # report, never kill the server
        job._add("fail", "ERROR %s: %s" % (type(e).__name__, e))
        job.status = "error"
    finally:
        job.ended = time.time()
        try:
            X.set_mode("dynamic", True)        # leave the network in its default state
        except OSError:
            pass
        JOB_LOCK.release()


# ============================================================================ helpers
def hcmd(host, cmd):
    with NET_LOCK:
        return NET.get(host).cmd(cmd)


def show_args(user, args):
    return "python3 client/client.py --user %s %s" % (user, " ".join(str(a) for a in args))


def client(job, host, args, user=None, timeout=30, retries=5, quiet=False):
    u = user or X.USERS[host]
    if not quiet:
        job.cmd(host, show_args(u[0], args))
    res = X.run_parallel(NET, [(host, X.client_cmd(host, args, user=u, timeout=timeout, retries=retries))])[0]
    if not quiet:
        if res.get("ok"):
            if "files" in res:
                job.out("%d file(s): %s" % (len(res["files"]), ", ".join(f["name"] for f in res["files"])))
            elif "bytes" in res and "start" in res:
                dt = res["end"] - res["start"]
                job.out("OK %s: %d bytes in %.2fs = %.2f Mbps%s" % (
                    res.get("op", ""), res["bytes"], dt, res["bytes"] * 8 / max(dt, 1e-6) / 1e6,
                    "  (SHA-256 verified)" if res.get("sha256_ok") else ""))
            else:
                job.out("OK " + json.dumps({k: v for k, v in res.items()
                                            if k not in ("client", "user", "command", "host")})[:300])
        else:
            job.out("ERROR: %s" % res.get("error"))
    return res


def ensure_file(job, name, mb):
    path = X.make_file(name, mb)
    job.log("Preparing %s (%d MB) on the server ..." % (name, mb))
    res = client(job, "h1", ["upload", path], user=("admin", "admin123"), timeout=60, quiet=True)
    if not res.get("ok"):
        raise RuntimeError("could not upload %s: %s" % (name, res.get("error")))


def stats():
    try:
        return ctl_call({"cmd": "stats"})
    except (OSError, ValueError):
        return None


def wait_transfer(client_ip, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = stats()
        for c in (st or {}).get("connections", []):
            if c["client"].startswith(client_ip + ":") and c["transferring"]:
                return c
        time.sleep(0.25)
    return None


def link_status(a, b, state):
    with NET_LOCK:
        NET.configLinkStatus(a, b, state)


def save_quick(name, data):
    os.makedirs(QUICK, exist_ok=True)
    with open(os.path.join(QUICK, name + ".json"), "w") as f:
        json.dump(data, f, indent=2)


# ============================================================================ scenarios
def sc_pingall(job):
    job.log("Every host pings every other host. The controller answers ARP itself (proxy-ARP) "
            "and installs a path for each IP pair on the first packet-in.")
    job.cmd("mininet", "pingall")
    with NET_LOCK:
        loss = NET.pingAll(timeout=1)
    job.out("Results: %s%% dropped" % loss)
    st = stats()
    job.check("all hosts reachable (0% loss)", loss == 0, "%s%% loss" % loss)
    job.check("all 4 switches connected to the controller",
              st and sorted(st["switches"]) == [1, 2, 3, 4], st and st["switches"])


def sc_flows(job):
    job.log("Dumping the OpenFlow tables the controller installed on every switch.")
    names = {300: "BLOCK", 250: "ACL drop", 100: "per-connection path", 50: "host-pair (ICMP etc.)",
             1: "noise drop (IPv6/LLDP)", 0: "table-miss -> controller"}
    total = 0
    for s in ("s1", "s2", "s3", "s4"):
        job.cmd("root", "ovs-ofctl -O OpenFlow13 dump-flows %s" % s)
        with NET_LOCK:
            raw = NET.get(s).cmd("ovs-ofctl -O OpenFlow13 dump-flows %s" % s)
        counts = {}
        for line in raw.splitlines():
            m = re.search(r"priority=(\d+)", line)
            if m:
                counts[int(m.group(1))] = counts.get(int(m.group(1)), 0) + 1
        total += sum(counts.values())
        job.out("  %s: %s" % (s, ", ".join("%d x %s" % (n, names.get(p, "prio %d" % p))
                                           for p, n in sorted(counts.items(), reverse=True))))
        job.check("%s has a table-miss rule to the controller" % s, 0 in counts)
    job.metrics["flows_total"] = total


def sc_basic(job):
    job.log("Raw TCP sockets: each message is a 4-byte length + JSON, file bytes follow as a stream.")
    job.check("authenticated PING", client(job, "h1", ["ping"]).get("ok"))
    job.check("alice uploads README.md", client(job, "h1", ["upload", "README.md"]).get("ok"))
    r = client(job, "h2", ["list"])
    job.check("bob lists files and sees README.md", r.get("ok") and any(f["name"] == "README.md"
                                                                        for f in r.get("files", [])))
    r = client(job, "h2", ["info", "README.md"])
    job.check("metadata includes owner + SHA-256", r.get("ok") and r.get("sha256") and r.get("owner"))
    r = client(job, "h2", ["download", "README.md", "/tmp/fsdl/readme.md"])
    job.check("bob downloads README.md, SHA-256 verified", r.get("ok") and r.get("sha256_ok"))


def sc_roles(job):
    job.log("Server-side authentication and authorisation (PBKDF2 passwords, roles admin/user/readonly).")
    small = X.make_file("roles_test.bin", 1)
    client(job, "h1", ["upload", small], quiet=True)
    r = client(job, "h2", ["ping"], user=("bob", "wrong"), retries=0)
    job.check("wrong password rejected", not r.get("ok") and "authentication" in str(r.get("error")))
    # the readonly 'guest' account is used from h1 (h5 itself is blocked by the network policy)
    r = client(job, "h1", ["upload", small, "ro.bin"], user=("guest", "guest123"))
    job.check("readonly user cannot upload", not r.get("ok") and "readonly" in str(r.get("error")))
    r = client(job, "h2", ["delete", "roles_test.bin"])
    job.check("non-owner cannot delete", not r.get("ok") and "permission" in str(r.get("error")))
    r = client(job, "h1", ["upload", small, "../etc/passwd"])
    job.check("path-traversal file name rejected", not r.get("ok") and "invalid" in str(r.get("error")))
    job.check("owner can delete", client(job, "h1", ["delete", "roles_test.bin"]).get("ok"))


def sc_concurrent(job):
    X.set_mode("dynamic", True)
    fname = "bench_3MB.bin"
    ensure_file(job, fname, 3)
    job.log("Four clients (h1-h4) download at the same time. With dynamic routing the controller "
            "places each new transfer on the least-loaded path.")
    for h in X.CLIENT_HOSTS:
        job.cmd(h, show_args(X.USERS[h][0], ["download", fname, "&"]))
    res, snap = X.concurrent_downloads(NET, 4, fname, timeout=30)
    for r in res:
        job.out("%s: %s" % (r["host"], ("%.2f Mbps" % (r["bytes"] * 8 / max(r["end"] - r["start"], 1e-6) / 1e6))
                            if r.get("ok") else r.get("error")))
    s = X.summarise(res)
    job.out("paths in use during the transfer: %s" % snap)
    job.metrics.update(aggregate_mbps=s.get("aggregate_mbps"), paths=snap, fairness=s.get("jain_fairness"))
    job.check("all 4 downloads completed and verified", s.get("ok") == 4)
    job.check("transfers spread over both paths", len(snap) == 2, snap)


def bench(job, mode, n, fname, timeout=30):
    X.set_mode(mode, True)
    time.sleep(1.5)
    res, snap = X.concurrent_downloads(NET, n, fname, timeout=timeout)
    s = X.summarise(res)
    job.out("  %-7s n=%d  aggregate %.2f Mbps  avg %.1fs  paths %s" % (
        mode, n, s.get("aggregate_mbps", 0), s.get("avg_completion_s", 0), snap))
    return s


def sc_compare(job):
    fname = "bench_3MB.bin"
    ensure_file(job, fname, 3)
    job.log("Quick E1: static shortest-path routing (baseline) vs dynamic load-aware routing, "
            "1/2/4 concurrent downloads of 3 MB.")
    rows = []
    for n in (1, 2, 4):
        for mode in ("static", "dynamic"):
            s = bench(job, mode, n, fname)
            rows.append({"clients": n, "mode": mode, "aggregate_mbps": s.get("aggregate_mbps", 0),
                         "avg_completion_s": s.get("avg_completion_s", 0)})
    job.metrics["chart"] = {"title": "Aggregate throughput (Mbps)", "groups": ["1", "2", "4"],
                            "series": {m: [r["aggregate_mbps"] for r in rows if r["mode"] == m]
                                       for m in ("static", "dynamic")}, "xlabel": "concurrent clients"}
    save_quick("e1_quick", rows)
    st4 = next(r for r in rows if r["clients"] == 4 and r["mode"] == "static")["aggregate_mbps"]
    dy4 = next(r for r in rows if r["clients"] == 4 and r["mode"] == "dynamic")["aggregate_mbps"]
    job.check("dynamic beats static with 4 clients", dy4 > st4, "%.2f vs %.2f Mbps" % (dy4, st4))


def sc_loss(job):
    fname = "bench_3MB.bin"
    ensure_file(job, fname, 3)
    job.log("Quick E2: 4 concurrent downloads with 0%% and 2%% packet loss on every core link.")
    rows = []
    try:
        for loss in (0, 2):
            job.cmd("root", "tc: core links loss=%d%%" % loss)
            with NET_LOCK:
                topo.set_core_link_params(NET, loss=loss)
            for mode in ("static", "dynamic"):
                s = bench(job, mode, 4, fname, timeout=40)
                rows.append({"loss_pct": loss, "mode": mode, "aggregate_mbps": s.get("aggregate_mbps", 0)})
    finally:
        with NET_LOCK:
            topo.set_core_link_params(NET, loss=0)
    job.metrics["chart"] = {"title": "Aggregate throughput under loss (Mbps)", "groups": ["0%", "2%"],
                            "series": {m: [r["aggregate_mbps"] for r in rows if r["mode"] == m]
                                       for m in ("static", "dynamic")}, "xlabel": "packet loss"}
    save_quick("e2_quick", rows)
    lossy = {r["mode"]: r["aggregate_mbps"] for r in rows if r["loss_pct"] == 2}
    job.check("all transfers survive 2% loss", all(v > 0 for v in lossy.values()), lossy)
    job.check("dynamic still beats static under loss", lossy["dynamic"] > lossy["static"],
              "%.2f vs %.2f Mbps" % (lossy["dynamic"], lossy["static"]))


def sc_reroute(job):
    X.set_mode("dynamic", True)
    fname = "fail_8MB.bin"
    ensure_file(job, fname, 8)
    ip = NC.HOSTS["h1"]["ip"]
    job.log("h1 starts an 8 MB download. Once it is transferring we cut the core link it is using.")
    job.cmd("h1", show_args("alice", ["-v", "download", fname, "/tmp/fsdl/reroute.bin"]))
    proc = NET.get("h1").popen(X.client_cmd("h1", ["download", fname, "/tmp/fsdl/reroute.bin"], timeout=10),
                               cwd=ROOT, stdout=-1, stderr=-1)
    conn = wait_transfer(ip)
    if not conn:
        proc.kill()
        job.check("transfer started", False)
        return
    before = conn["path"]
    sw = before.split("-")
    job.out("transfer running on path %s" % before)
    time.sleep(1.5)
    job.cmd("mininet", "link %s %s down" % (sw[0], sw[1]))
    link_status(sw[0], sw[1], "down")
    time.sleep(3)
    st = stats() or {}
    after = next((c["path"] for c in st.get("connections", []) if c["client"] == conn["client"]), None)
    ev = [e for e in st.get("recent_events", []) if e.get("event") == "reroute"]
    if ev:
        job.out("controller event: %s" % json.dumps(ev[-1]))
    job.out("path after failure: %s" % after)
    stdout, _ = proc.communicate(timeout=180)
    try:
        res = json.loads(stdout.decode().strip().splitlines()[-1])
    except (ValueError, IndexError):
        res = {"ok": False}
    job.cmd("mininet", "link %s %s up" % (sw[0], sw[1]))
    link_status(sw[0], sw[1], "up")
    job.out("download finished: ok=%s, longest stall %.2fs, reconnects=%s" % (
        res.get("ok"), res.get("max_gap_s") or 0, res.get("reconnects")))
    job.metrics.update(path_before=before, path_after=after, max_gap_s=res.get("max_gap_s"),
                       controller_ms=ev[-1].get("controller_ms") if ev else None)
    job.check("live connection moved to the other path", after and after != before,
              "%s -> %s" % (before, after))
    job.check("controller logged a reroute moving >= 1 connection",
              ev and ev[-1].get("connections_moved", 0) >= 1)
    job.check("download completed with SHA-256 verified", res.get("ok") and res.get("sha256_ok"))


def sc_failover(job):
    fname = "fail_8MB.bin"
    ensure_file(job, fname, 8)
    job.log("Quick E3: s1-s2 fails 3 s into a download (static routing, so it starts on s1-s2). "
            "Compare SDN failover OFF vs ON.")
    rows = []
    for scenario, fo in (("failover_off", False), ("failover_on", True)):
        X.set_mode("static", fo)
        time.sleep(1.5)
        job.cmd("h1", show_args("alice", ["download", fname]) + "    # failover %s" % ("on" if fo else "off"))
        p = NET.get("h1").popen(X.client_cmd("h1", ["download", fname, "/tmp/fsdl/fo.bin"], timeout=5),
                                cwd=ROOT, stdout=-1, stderr=-1)
        time.sleep(3)
        link_status("s1", "s2", "down")
        stdout, _ = p.communicate(timeout=240)
        link_status("s1", "s2", "up")
        try:
            res = json.loads(stdout.decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            res = {"ok": False}
        row = {"scenario": scenario, "ok": res.get("ok"), "max_gap_s": res.get("max_gap_s") or 0,
               "reconnects": res.get("reconnects"),
               "completion_s": round(res["end"] - res["start"], 2) if res.get("ok") else None}
        rows.append(row)
        job.out("  %-13s ok=%s  longest stall %.2fs  reconnects=%s  total %ss" % (
            scenario, row["ok"], row["max_gap_s"], row["reconnects"], row["completion_s"]))
        time.sleep(2)
    job.metrics["chart"] = {"title": "Longest stall during link failure (s)", "groups": ["failover"],
                            "series": {"off": [rows[0]["max_gap_s"]], "on": [rows[1]["max_gap_s"]]},
                            "xlabel": ""}
    save_quick("e3_quick", rows)
    job.check("both downloads complete", all(r["ok"] for r in rows))
    job.check("failover ON stalls less than OFF", rows[1]["max_gap_s"] < rows[0]["max_gap_s"],
              "%.2fs vs %.2fs" % (rows[1]["max_gap_s"], rows[0]["max_gap_s"]))


def tcp_ok(host, port):
    with NET_LOCK:
        return X.tcp_reachable(NET, host, port)


def sc_policy(job):
    X.set_mode("dynamic", True)
    job.log("Access policy (config/policy.json): h5 is a denied host; only TCP 5000 is open on the server.")
    job.cmd("h5", "ping -c2 %s" % NC.SERVER_IP)
    out = hcmd("h5", "ping -c2 -W1 %s" % NC.SERVER_IP)
    job.out(out.strip().splitlines()[-2] if out.strip() else out)
    job.check("h5 can still ping the server (ICMP allowed)", "0% packet loss" in out)
    job.cmd("h5", "connect %s:%d   # right after the ping" % (NC.SERVER_IP, NC.SERVER_PORT))
    job.check("h5 cannot reach the file service (dropped by SDN)", not tcp_ok("h5", NC.SERVER_PORT))
    job.cmd("h1", "connect %s:%d" % (NC.SERVER_IP, NC.SERVER_PORT))
    job.check("h1 reaches the file service", tcp_ok("h1", NC.SERVER_PORT))
    job.cmd("h1", "connect %s:22" % NC.SERVER_IP)
    job.check("firewall drops other server ports (h1 -> :22)", not tcp_ok("h1", 22))


def sc_bruteforce(job):
    X.set_mode("dynamic", True)
    ip = NC.HOSTS["h3"]["ip"]
    ctl_call({"cmd": "unblock", "ip": ip})
    job.log("The server reports every failed login to the controller; after 3 failures in 60 s "
            "the controller installs a drop rule for that host at its switch.")
    for pw in ("x", "y", "z"):
        r = client(job, "h3", ["ping"], user=("carol", pw), timeout=3, retries=0)
    time.sleep(0.5)
    st = stats() or {}
    job.out("controller blocked list: %s" % st.get("blocked"))
    job.check("controller blocked h3 after 3 failed logins", ip in (st.get("blocked") or {}))
    r = client(job, "h3", ["ping"], user=("carol", "carol123"), timeout=3, retries=0)
    job.check("correct password from h3 is now dropped by the network", not r.get("ok"))
    job.check("other users (h1) unaffected", client(job, "h1", ["ping"]).get("ok"))
    job.cmd("admin", "python3 controller/ctl.py unblock %s" % ip)
    ctl_call({"cmd": "unblock", "ip": ip})
    time.sleep(1)
    job.check("h3 works again after admin unblock", client(job, "h3", ["ping"]).get("ok"))


def sc_overload(job):
    fname = "bench_2MB.bin"
    ensure_file(job, fname, 2)
    job.log("Server restarted with only 3 session slots; 10 clients download at once. Extra clients get "
            "BUSY + retry_after and retry with backoff.")
    rows = []
    try:
        with NET_LOCK:
            topo.start_server(NET, max_clients=3)
        for retries in (0, 8):
            jobs = [(X.CLIENT_HOSTS[i % 4], X.client_cmd(X.CLIENT_HOSTS[i % 4],
                     ["download", fname, "/tmp/fsdl/ov_%d.bin" % i], retries=retries)) for i in range(10)]
            job.cmd("h1-h4", "10 x download %s  (--retries %d)" % (fname, retries))
            s = X.summarise(X.run_parallel(NET, jobs))
            rows.append({"retries": retries, "succeeded": s.get("ok", 0), "busy": s.get("busy", 0)})
            job.out("  retries=%d: %d/10 succeeded, %d BUSY replies" % (retries, s.get("ok", 0), s.get("busy", 0)))
            time.sleep(2)
    finally:
        with NET_LOCK:
            topo.start_server(NET)
    job.metrics["chart"] = {"title": "Clients served (of 10)", "groups": ["retries=0", "retries=8"],
                            "series": {"succeeded": [r["succeeded"] for r in rows]}, "xlabel": ""}
    save_quick("e5_quick", rows)
    job.check("overloaded server answers BUSY instead of failing", rows[0]["busy"] > 0)
    job.check("with retries almost every client is served", rows[1]["succeeded"] >= 9,
              "%d/10" % rows[1]["succeeded"])


def sc_unittests(job):
    job.cmd("root", "python3 -m unittest discover -s tests -v")
    p = subprocess.Popen([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    ran = ""
    for line in p.stdout:
        line = line.rstrip()
        if " ... " in line or line.startswith(("Ran ", "OK", "FAILED")):
            job.out(line)
        if line.startswith("Ran "):
            ran = line
    p.wait()
    job.metrics["summary"] = ran
    job.check("all unit tests pass", p.returncode == 0, ran)


SCENARIOS = {
    "pingall": ("Connectivity (pingall)", sc_pingall),
    "flows": ("OpenFlow rules on the switches", sc_flows),
    "basic": ("Upload / list / download over TCP", sc_basic),
    "roles": ("Authentication and roles", sc_roles),
    "unittests": ("Automated unit tests", sc_unittests),
    "concurrent": ("Concurrent downloads, load balanced", sc_concurrent),
    "bruteforce": ("Brute-force login -> SDN block", sc_bruteforce),
    "policy": ("Access policy and firewall", sc_policy),
    "reroute": ("Live link failure and reroute", sc_reroute),
    "failover": ("Failover ON vs OFF", sc_failover),
    "loss": ("Packet loss", sc_loss),
    "overload": ("Server overload", sc_overload),
    "compare": ("Static vs dynamic routing", sc_compare),
}


# ============================================================================ http
def results_index():
    def imgs(d):
        return sorted(f for f in os.listdir(d) if f.endswith(".png")) if os.path.isdir(d) else []
    summary = ""
    p = os.path.join(RESULTS, "SUMMARY.md")
    if os.path.exists(p):
        with open(p) as f:
            summary = f.read()
    quick = {}
    if os.path.isdir(QUICK):
        for f in sorted(os.listdir(QUICK)):
            if f.endswith(".json"):
                with open(os.path.join(QUICK, f)) as fh:
                    quick[f[:-5]] = json.load(fh)
    return {"summary_md": summary, "images": imgs(RESULTS), "quick": quick}


def tail(path, n=12):
    try:
        with open(path, errors="replace") as f:
            return f.readlines()[-n:]
    except OSError:
        return []


class Handler(BaseHTTPRequestHandler):
    server_version = "SDNDemo/1.0"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path):
        if not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        qs = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&") if "=" in p) \
            if "?" in self.path else {}
        if path in ("/", "/index.html"):
            return self._file(os.path.join(STATIC, "index.html"))
        if path.startswith("/static/"):
            name = os.path.basename(path)
            return self._file(os.path.join(STATIC, name))
        if path.startswith("/results/"):
            name = os.path.basename(path)
            if not re.match(r"^[\w.-]+\.(png|json|md)$", name):
                return self._json({"error": "bad name"}, 400)
            return self._file(os.path.join(RESULTS, name))
        if path == "/api/state":
            job = CURRENT["job"]
            return self._json({"stats": stats(), "net": NET is not None,
                               "job": job.view(len(job.lines)) if job else None,
                               "server_log": tail(os.path.join(ROOT, "logs", "server.log")),
                               "scenarios": {k: v[0] for k, v in SCENARIOS.items()}})
        if path.startswith("/api/job/"):
            job = JOBS.get(path.rsplit("/", 1)[1])
            if not job:
                return self._json({"error": "no such job"}, 404)
            return self._json(job.view(int(qs.get("since", 0))))
        if path == "/api/results":
            return self._json(results_index())
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        path, body = self.path.split("?", 1)[0], self._body()
        if path.startswith("/api/run/"):
            sid = path.rsplit("/", 1)[1]
            if sid not in SCENARIOS:
                return self._json({"error": "unknown scenario"}, 404)
            if not JOB_LOCK.acquire(blocking=False):
                return self._json({"error": "another demo is still running",
                                   "job": CURRENT["job"].id if CURRENT["job"] else None}, 409)
            title, fn = SCENARIOS[sid]
            job = Job(sid, title)
            JOBS[job.id] = job
            CURRENT["job"] = job
            threading.Thread(target=run_job, args=(job, fn), daemon=True).start()
            return self._json(job.view())
        if path == "/api/admin":
            action = body.get("action")
            try:
                if action == "mode":
                    return self._json(X.set_mode(routing=body["value"]))
                if action == "failover":
                    return self._json(X.set_mode(failover=bool(body["value"])))
                if action == "block":
                    return self._json(ctl_call({"cmd": "block", "ip": body["ip"],
                                                "duration": int(body.get("duration", 60))}))
                if action == "unblock":
                    return self._json(ctl_call({"cmd": "unblock", "ip": body["ip"]}))
                if action == "link":
                    a, b, st = body["a"], body["b"], body["state"]
                    if (a, b) not in [(x[0], x[2]) for x in NC.CORE_LINKS] or st not in ("up", "down"):
                        return self._json({"error": "bad link"}, 400)
                    link_status(a, b, st)
                    return self._json({"status": "OK"})
            except (KeyError, OSError, ValueError) as e:
                return self._json({"error": str(e)}, 400)
            return self._json({"error": "unknown action"}, 400)
        return self._json({"error": "not found"}, 404)


# ============================================================================ main
def controller_up():
    return stats() is not None


def start_controller():
    global CTRL_PROC
    if controller_up():
        print("[gui] using the SDN controller that is already running")
        return
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    log = open(os.path.join(ROOT, "logs", "controller_gui.log"), "w")
    CTRL_PROC = subprocess.Popen([sys.executable, os.path.join(ROOT, "controller", "run_controller.py")],
                                 cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(60):
        if controller_up():
            print("[gui] SDN controller started (pid %d)" % CTRL_PROC.pid)
            return
        time.sleep(0.5)
    sys.exit("controller did not start - see logs/controller_gui.log")


def shutdown(*_):
    print("\n[gui] shutting down ...")
    try:
        if NET is not None:
            topo.stop(NET)
    except Exception:          # mininet can raise while deleting the mgmt link twice
        pass
    os.system("mn -c > /dev/null 2>&1")
    if CTRL_PROC:
        CTRL_PROC.terminate()
    os._exit(0)


def main():
    global NET
    ap = argparse.ArgumentParser(description="SDN file-sharing demo console")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("run with sudo:  sudo python3 gui/server.py")
    setLogLevel("warning")
    os.system("mn -c > /dev/null 2>&1")
    start_controller()
    print("[gui] building Mininet topology ...")
    NET = topo.build()
    topo.start_server(NET)
    os.makedirs("/tmp/fsdl", exist_ok=True)
    time.sleep(3)
    X.set_mode("dynamic", True)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    httpd = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    httpd.daemon_threads = True
    print("[gui] ready:  open http://localhost:%d   (Ctrl+C to stop)" % a.port)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
