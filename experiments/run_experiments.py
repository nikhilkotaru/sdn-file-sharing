#!/usr/bin/env python3
"""
Automated test + performance evaluation.

    sudo python3 experiments/run_experiments.py            # all experiments
    sudo python3 experiments/run_experiments.py --only e1 e3 --reps 3

Requires the SDN controller to be running already. Results go to results/.

  E0  functional tests  : auth, list, upload, download, integrity, roles, delete
  E1  concurrency       : N concurrent downloads, static vs dynamic routing
  E2  packet loss       : 0/1/3 % loss on core links, static vs dynamic
  E3  link failure      : link s1-s2 fails mid-transfer, failover on vs off
  E4  security          : policy-denied host, firewall, brute-force -> SDN block
  E5  server overload   : more clients than server slots, BUSY + retry
"""
import argparse
import json
import os
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "topology"))
sys.path.insert(0, os.path.join(ROOT, "controller"))

from mininet.log import setLogLevel
from common import netconfig as NC
import topo
from ctl import call as ctl_call

CLIENT = os.path.join(ROOT, "client", "client.py")
RESULTS = os.path.join(ROOT, "results")
DATA = os.path.join(ROOT, "data")
USERS = {"h1": ("alice", "alice123"), "h2": ("bob", "bob123"), "h3": ("carol", "carol123"),
         "h4": ("dave", "dave123"), "h5": ("guest", "guest123")}
CLIENT_HOSTS = ["h1", "h2", "h3", "h4"]


def log(*a):
    print("[exp]", *a, flush=True)


def save(name, rows):
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, name + ".json"), "w") as f:
        json.dump(rows, f, indent=2)
    log("saved results/%s.json" % name)


def make_file(name, mb):
    os.makedirs(DATA, exist_ok=True)
    p = os.path.join(DATA, name)
    if not os.path.exists(p) or os.path.getsize(p) != mb * 1024 * 1024:
        with open(p, "wb") as f:
            f.write(os.urandom(mb * 1024 * 1024))
    return p


def client_cmd(host, args, user=None, timeout=10, retries=5):
    u, p = user or USERS[host]
    return ["python3", CLIENT, "--user", u, "--password", p, "--json", "--timeout", str(timeout),
            "--retries", str(retries)] + args


def run_parallel(net, jobs):
    """jobs: [(hostname, argv)] -> list of parsed JSON results (same order)."""
    procs = [(h, net.get(h).popen(argv, cwd=ROOT, stdout=-1, stderr=-1)) for h, argv in jobs]
    out = []
    for h, p in procs:
        stdout, stderr = p.communicate()
        line = (stdout.decode().strip().splitlines() or ["{}"])[-1]
        try:
            res = json.loads(line)
        except ValueError:
            res = {"ok": False, "error": "no JSON output: " + stderr.decode()[-300:]}
        res["host"] = h
        out.append(res)
    return out


def run_one(net, host, args, **kw):
    return run_parallel(net, [(host, client_cmd(host, args, **kw))])[0]


def summarise(results):
    ok = [r for r in results if r.get("ok")]
    if not ok:
        return {"ok": 0, "n": len(results)}
    start = min(r["start"] for r in ok)
    end = max(r["end"] for r in ok)
    total = sum(r["bytes"] for r in ok)
    per = [r["bytes"] * 8 / max(r["end"] - r["start"], 1e-6) / 1e6 for r in ok]
    fairness = sum(per) ** 2 / (len(per) * sum(x * x for x in per)) if per else 0
    return {"n": len(results), "ok": len(ok),
            "aggregate_mbps": round(total * 8 / max(end - start, 1e-6) / 1e6, 3),
            "avg_completion_s": round(sum(r["end"] - r["start"] for r in ok) / len(ok), 3),
            "max_completion_s": round(max(r["end"] - r["start"] for r in ok), 3),
            "per_client_mbps": [round(x, 3) for x in per],
            "jain_fairness": round(fairness, 3),
            "reconnects": sum(r.get("reconnects", 0) for r in results),
            "busy": sum(r.get("busy", 0) for r in results)}


def set_mode(routing=None, failover=None):
    req = {"cmd": "set_mode"}
    if routing:
        req["routing"] = routing
    if failover is not None:
        req["failover"] = failover
    return ctl_call(req)


def path_snapshot():
    try:
        st = ctl_call({"cmd": "stats"})
        paths = {}
        for c in st["connections"]:
            if c["transferring"]:
                paths[c["path"]] = paths.get(c["path"], 0) + 1
        return paths
    except OSError:
        return {}


def concurrent_downloads(net, n, fname, timeout=20):
    jobs = []
    for i in range(n):
        h = CLIENT_HOSTS[i % len(CLIENT_HOSTS)]
        out = "/tmp/fsdl/%s_%d_%s" % (h, i, fname)
        jobs.append((h, client_cmd(h, ["download", fname, out], timeout=timeout)))
    # snapshot path usage while transfers run
    import threading
    snap = {}
    t = threading.Timer(1.5, lambda: snap.update(path_snapshot()))
    t.start()
    res = run_parallel(net, jobs)
    t.cancel()
    return res, snap


# ===================================================================== experiments
def e0_functional(net):
    log("E0 functional tests")
    small = make_file("func_1MB.bin", 1)
    tests = []

    def check(name, res, expect_ok=True, contains=None):
        passed = bool(res.get("ok")) == expect_ok and (contains is None or contains in json.dumps(res))
        tests.append({"test": name, "passed": passed, "result": res})
        log("  %-45s %s" % (name, "PASS" if passed else "FAIL  " + json.dumps(res)[:200]))

    check("ping with valid credentials", run_one(net, "h1", ["ping"]))
    check("wrong password rejected", run_one(net, "h2", ["ping"], user=("bob", "nope"), retries=0),
          expect_ok=False, contains="authentication failed")
    check("upload 1MB (alice)", run_one(net, "h1", ["upload", small]))
    check("list shows uploaded file", run_one(net, "h2", ["list"]), contains="func_1MB.bin")
    check("info has sha256 metadata", run_one(net, "h2", ["info", "func_1MB.bin"]), contains="sha256")
    check("download + SHA-256 verify (bob)", run_one(net, "h2", ["download", "func_1MB.bin", "/tmp/fsdl/f.bin"]),
          contains='"sha256_ok": true')
    check("readonly user cannot upload", run_one(net, "h1", ["upload", small, "ro.bin"], user=("guest", "guest123")),
          expect_ok=False, contains="readonly")
    check("non-owner cannot delete", run_one(net, "h2", ["delete", "func_1MB.bin"]),
          expect_ok=False, contains="permission denied")
    check("path traversal name rejected", run_one(net, "h1", ["upload", small, "../evil"]),
          expect_ok=False, contains="invalid file name")
    check("owner can delete", run_one(net, "h1", ["delete", "func_1MB.bin"]))
    save("e0_functional", tests)
    return tests


def e1_concurrency(net, reps, sizes_mb):
    log("E1 concurrency: static vs dynamic")
    fname = "bench_%dMB.bin" % sizes_mb
    run_one(net, "h1", ["upload", make_file(fname, sizes_mb)], user=("admin", "admin123"))
    rows = []
    for mode in ("static", "dynamic"):
        set_mode(mode, True)
        for n in (1, 2, 4, 8):
            for rep in range(reps):
                time.sleep(2)
                res, snap = concurrent_downloads(net, n, fname)
                s = summarise(res)
                s.update(mode=mode, clients=n, rep=rep, paths=snap)
                rows.append(s)
                log("  mode=%-7s n=%d rep=%d agg=%.2f Mbps avg=%.2fs fair=%.2f paths=%s"
                    % (mode, n, rep, s.get("aggregate_mbps", 0), s.get("avg_completion_s", 0),
                       s.get("jain_fairness", 0), snap))
    save("e1_concurrency", rows)
    return rows


def e2_loss(net, reps, sizes_mb):
    log("E2 packet loss")
    fname = "bench_%dMB.bin" % sizes_mb
    rows = []
    for loss in (0, 1, 3):
        topo.set_core_link_params(net, loss=loss)
        for mode in ("static", "dynamic"):
            set_mode(mode, True)
            for rep in range(reps):
                time.sleep(2)
                res, snap = concurrent_downloads(net, 4, fname, timeout=30)
                s = summarise(res)
                s.update(mode=mode, loss_pct=loss, clients=4, rep=rep)
                rows.append(s)
                log("  loss=%d%% mode=%-7s agg=%.2f Mbps avg=%.2fs" % (loss, mode, s.get("aggregate_mbps", 0),
                                                                      s.get("avg_completion_s", 0)))
    topo.set_core_link_params(net, loss=0)
    save("e2_loss", rows)
    return rows


def e3_failover(net, reps):
    log("E3 link failure during transfer")
    fname = "fail_10MB.bin"
    run_one(net, "h1", ["upload", make_file(fname, 10)], user=("admin", "admin123"))
    rows = []
    for scenario in ("no_failure", "failover_on", "failover_off"):
        for rep in range(reps):
            set_mode("static", scenario != "failover_off")   # static => transfer starts on s1-s2
            time.sleep(2)
            h = net.get("h1")
            p = h.popen(client_cmd("h1", ["download", fname, "/tmp/fsdl/fail.bin"], timeout=5),
                        cwd=ROOT, stdout=-1, stderr=-1)
            t_fail = None
            if scenario != "no_failure":
                time.sleep(3)
                t_fail = time.time()
                net.configLinkStatus("s1", "s2", "down")
            stdout, stderr = p.communicate()
            try:
                res = json.loads(stdout.decode().strip().splitlines()[-1])
            except (ValueError, IndexError):
                res = {"ok": False, "error": stderr.decode()[-300:]}
            net.configLinkStatus("s1", "s2", "up")
            row = {"scenario": scenario, "rep": rep, "ok": res.get("ok"),
                   "completion_s": round(res["end"] - res["start"], 3) if res.get("ok") else None,
                   "max_gap_s": res.get("max_gap_s"), "reconnects": res.get("reconnects"),
                   "error": res.get("error")}
            rows.append(row)
            log("  %s" % row)
            time.sleep(3)
    save("e3_failover", rows)
    return rows


def tcp_reachable(net, host, port, timeout=3):
    cmd = ("python3 -c \"import socket,sys; s=socket.socket(); s.settimeout(%d); "
           "sys.exit(0 if s.connect_ex(('%s',%d))==0 else 1)\"" % (timeout, NC.SERVER_IP, port))
    net.get(host).cmd(cmd)
    return net.get(host).cmd("echo $?").strip() == "0"


def e4_security(net):
    log("E4 security / access policies")
    rows = []

    def rec(name, value, expected):
        rows.append({"check": name, "observed": value, "expected": expected, "passed": value == expected})
        log("  %-50s observed=%s expected=%s" % (name, value, expected))

    set_mode("dynamic", True)
    rec("h5 (denied host) can ping server", "0% packet loss" in net.get("h5").cmd("ping -c2 -W1 %s" % NC.SERVER_IP), True)
    rec("h5 (denied host) reaches file service", tcp_reachable(net, "h5", NC.SERVER_PORT), False)
    rec("h1 reaches file service", tcp_reachable(net, "h1", NC.SERVER_PORT), True)
    rec("h1 reaches server port 22 (firewall)", tcp_reachable(net, "h1", 22), False)

    # brute force from h3
    attempts = []
    t0 = time.time()
    for i in range(6):
        r = run_one(net, "h3", ["ping"], user=("carol", "guess%d" % i), timeout=3, retries=0)
        attempts.append({"attempt": i + 1, "t": round(time.time() - t0, 2), "error": r.get("error")})
        log("    brute-force attempt %d: %s" % (i + 1, r.get("error")))
        time.sleep(0.5)
    blocked_after = next((a["attempt"] for a in attempts if "authentication" not in str(a["error"])), None)
    rec("brute force blocked by SDN after attempt #", blocked_after, 4)
    rec("blocked host h3 cannot log in even with valid password",
        run_one(net, "h3", ["ping"], timeout=3, retries=0).get("ok"), False)
    rec("other users (h1) unaffected", run_one(net, "h1", ["ping"]).get("ok"), True)
    ctl_call({"cmd": "unblock", "ip": NC.HOSTS["h3"]["ip"]})
    time.sleep(1)
    rec("h3 works again after unblock", run_one(net, "h3", ["ping"]).get("ok"), True)
    save("e4_security", {"checks": rows, "brute_force": attempts})
    return rows


def e5_overload(net):
    log("E5 server overload (max 3 sessions, 10 clients)")
    fname = "bench_2MB.bin"
    run_one(net, "h1", ["upload", make_file(fname, 2)], user=("admin", "admin123"))
    topo.start_server(net, max_clients=3)
    rows = []
    for retries in (0, 8):
        jobs = []
        for i in range(10):
            h = CLIENT_HOSTS[i % 4]
            jobs.append((h, client_cmd(h, ["download", fname, "/tmp/fsdl/ov_%d.bin" % i], retries=retries)))
        res = run_parallel(net, jobs)
        s = summarise(res)
        s.update(client_retries=retries)
        rows.append(s)
        log("  retries=%d: %d/%d succeeded, BUSY responses=%d" % (retries, s.get("ok", 0), s["n"], s.get("busy", 0)))
        time.sleep(2)
    topo.start_server(net)
    save("e5_overload", rows)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=["e0", "e1", "e2", "e3", "e4", "e5"])
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--size-mb", type=int, default=5)
    a = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    try:
        ctl_call({"cmd": "stats"})
    except OSError:
        sys.exit("SDN controller not reachable on 127.0.0.1:%d - start it first (see README)" % NC.CONTROLLER_EVENT_PORT)

    setLogLevel("warning")
    os.system("mn -c > /dev/null 2>&1")
    net = topo.build()
    try:
        topo.start_server(net)
        os.makedirs("/tmp/fsdl", exist_ok=True)
        time.sleep(3)   # let switches connect
        log("pingall: %s%% loss" % net.pingAll(timeout=1))
        steps = {"e0": lambda: e0_functional(net),
                 "e1": lambda: e1_concurrency(net, a.reps, a.size_mb),
                 "e2": lambda: e2_loss(net, a.reps, a.size_mb),
                 "e3": lambda: e3_failover(net, a.reps),
                 "e4": lambda: e4_security(net),
                 "e5": lambda: e5_overload(net)}
        if any(s in a.only for s in ("e1", "e2")):
            run_one(net, "h1", ["upload", make_file("bench_%dMB.bin" % a.size_mb, a.size_mb)],
                    user=("admin", "admin123"))
        for s in a.only:
            steps[s]()
    finally:
        topo.stop(net)
    log("done - now run: python3 experiments/plot_results.py")


if __name__ == "__main__":
    main()
