#!/usr/bin/env python3
"""
Admin CLI for the SDN controller's control socket.

  python3 controller/ctl.py stats              # one-shot JSON
  python3 controller/ctl.py watch              # live bandwidth dashboard
  python3 controller/ctl.py mode dynamic       # or: static
  python3 controller/ctl.py failover off       # or: on
  python3 controller/ctl.py block 10.0.0.3 60
  python3 controller/ctl.py unblock 10.0.0.3
"""
import argparse
import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import netconfig as NC


def call(req, host="127.0.0.1", port=NC.CONTROLLER_EVENT_PORT, timeout=3.0):
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.split(b"\n", 1)[0].decode())


def bar(pct, width=30):
    n = int(round(min(pct, 100) / 100 * width))
    return "#" * n + "." * (width - n)


def watch(host, interval):
    while True:
        try:
            st = call({"cmd": "stats"}, host)
        except OSError as e:
            print("controller unreachable: %s" % e)
            time.sleep(interval)
            continue
        os.system("clear" if os.name != "nt" else "cls")
        print("SDN File-Sharing Monitor   mode=%s  failover=%s  switches=%s"
              % (st["mode"], st["failover"], st["switches"]))
        print("\nCore links (capacity %d Mbps)" % NC.CORE_BW_MBPS)
        for l in st["links"]:
            state = "UP  " if l["up"] else "DOWN"
            print("  %-6s %s [%s] %6.2f Mbps %5.1f%%" % (l["name"], state, bar(l["util_pct"]), l["mbps"], l["util_pct"]))
        print("\nClients (from flow stats at s1)")
        for ip, c in sorted(st["clients"].items()):
            print("  %-10s down %6.2f Mbps  up %6.2f Mbps  total down %8.2f MB  up %8.2f MB"
                  % (ip, c["down_mbps"], c["up_mbps"], c["down_total_mb"], c["up_total_mb"]))
        print("\nConnections")
        for c in st["connections"]:
            print("  %-18s path %-12s %s" % (c["client"], c["path"],
                                            ("TRANSFER " + str(c["file"])) if c["transferring"] else "idle"))
        print("\nBlocked: %s" % (st["blocked"] or "none"))
        print("\nRecent events")
        for e in st["recent_events"][-6:]:
            print("  " + json.dumps(e))
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller", default="127.0.0.1")
    ap.add_argument("cmd", choices=["stats", "watch", "mode", "failover", "block", "unblock"])
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()
    if a.cmd == "watch":
        try:
            watch(a.controller, 1.0)
        except KeyboardInterrupt:
            pass
        return
    if a.cmd == "stats":
        req = {"cmd": "stats"}
    elif a.cmd == "mode":
        req = {"cmd": "set_mode", "routing": a.args[0]}
    elif a.cmd == "failover":
        req = {"cmd": "set_mode", "failover": a.args[0].lower() in ("on", "1", "true")}
    elif a.cmd == "block":
        req = {"cmd": "block", "ip": a.args[0], "duration": int(a.args[1]) if len(a.args) > 1 else 60}
    else:
        req = {"cmd": "unblock", "ip": a.args[0]}
    print(json.dumps(call(req, a.controller), indent=2))


if __name__ == "__main__":
    main()
