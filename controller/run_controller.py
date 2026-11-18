#!/usr/bin/env python3
"""
Launcher that works with either Ryu or os-ken (newer os-ken releases ship no
`osken-manager` command).

    python3 controller/run_controller.py                 # dynamic routing, failover on
    python3 controller/run_controller.py --static        # baseline
    python3 controller/run_controller.py --no-failover
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

ap = argparse.ArgumentParser()
ap.add_argument("--static", action="store_true", help="static shortest-path routing (baseline)")
ap.add_argument("--no-failover", action="store_true")
ap.add_argument("--ofp-port", type=int, default=6653)
a = ap.parse_args()
os.environ["FS_ROUTING"] = "static" if a.static else "dynamic"
os.environ["FS_FAILOVER"] = "0" if a.no_failover else "1"

try:
    from ryu.lib import hub
    hub.patch(thread=False)
    from ryu import cfg
    from ryu.base.app_manager import AppManager
    PROJECT, OFP_HANDLER = "ryu", "ryu.controller.ofp_handler"
except ImportError:
    from os_ken.lib import hub
    hub.patch(thread=False)
    from os_ken import cfg
    from os_ken.base.app_manager import AppManager
    PROJECT, OFP_HANDLER = "os_ken", "os_ken.controller.ofp_handler"

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

cfg.CONF(args=["--ofp-tcp-listen-port", str(a.ofp_port)], project=PROJECT)
print("starting %s controller: OpenFlow :%d, control socket :9090, routing=%s failover=%s"
      % (PROJECT, a.ofp_port, os.environ["FS_ROUTING"], os.environ["FS_FAILOVER"] == "1"))
AppManager.run_apps([OFP_HANDLER, os.path.join(HERE, "sdn_controller.py")])
