#!/usr/bin/env python3
"""
Manage file-server accounts (config/users.json).

  python3 tools/manage_users.py init-demo          # creates the demo accounts below
  python3 tools/manage_users.py add dave secret user
  python3 tools/manage_users.py remove dave
  python3 tools/manage_users.py list
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
from auth import UserStore

DEMO = [("admin", "admin123", "admin"), ("alice", "alice123", "user"), ("bob", "bob123", "user"),
        ("carol", "carol123", "user"), ("dave", "dave123", "user"), ("guest", "guest123", "readonly")]


def main():
    path = os.path.join(ROOT, "config", "users.json")
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    store = UserStore(path)
    cmd = sys.argv[1]
    if cmd == "init-demo":
        for u, p, r in DEMO:
            store.add(u, p, r)
        print("created demo users:", ", ".join("%s/%s (%s)" % d for d in DEMO))
    elif cmd == "add":
        store.add(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "user")
    elif cmd == "remove":
        store.remove(sys.argv[2])
    elif cmd == "list":
        for u, rec in sorted(store.users.items()):
            print("%-10s %s" % (u, rec["role"]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
