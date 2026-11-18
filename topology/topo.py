#!/usr/bin/env python3
"""
Mininet topology for the SDN file-sharing system.

    sudo python3 topology/topo.py            # builds network, starts file server, opens CLI

The controller must already be running (see README):
    ryu-manager controller/sdn_controller.py      (or osken-manager ...)
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch, Node
from mininet.link import TCLink, Link
from mininet.cli import CLI
from mininet.log import setLogLevel, info

from common import netconfig as NC


def build(core_bw=NC.CORE_BW_MBPS, core_delay=NC.CORE_DELAY, core_loss=0,
          access_bw=NC.ACCESS_BW_MBPS, controller_ip="127.0.0.1", controller_port=NC.OPENFLOW_PORT):
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=False, build=False)
    net.addController("c0", controller=RemoteController, ip=controller_ip, port=controller_port)

    sw = {n: net.addSwitch(n, dpid="%016x" % d, protocols="OpenFlow13", failMode="secure")
          for n, d in NC.SWITCHES.items()}
    hosts = {}
    for name, h in NC.HOSTS.items():
        hosts[name] = net.addHost(name, ip=h["ip"] + "/24", mac=h["mac"])
        net.addLink(hosts[name], sw[h["switch"]], port1=0, port2=h["port"], bw=access_bw)

    core = []
    for sa, pa, sb, pb in NC.CORE_LINKS:
        params = dict(bw=core_bw, delay=core_delay)
        if core_loss:
            params["loss"] = core_loss
        core.append(net.addLink(sw[sa], sw[sb], port1=pa, port2=pb, **params))

    # out-of-band management link: srv <-> root namespace (controller events)
    root = Node("root", inNamespace=False)
    mgmt = net.addLink(hosts["srv"], root, cls=Link)

    net.build()
    net.start()
    hosts["srv"].setIP(NC.MGMT_SERVER_IP + "/24", intf=mgmt.intf1)
    root.setIP(NC.MGMT_ROOT_IP + "/24", intf=mgmt.intf2)
    for h in hosts.values():
        h.cmd("sysctl -qw net.ipv6.conf.all.disable_ipv6=1 net.ipv6.conf.default.disable_ipv6=1")
    net.core_links = core
    net.mgmt_link = mgmt
    return net


def set_core_link_params(net, bw=NC.CORE_BW_MBPS, delay=NC.CORE_DELAY, loss=0):
    """Change shaping on all core links at runtime (used by experiments)."""
    for link in net.core_links:
        for intf in (link.intf1, link.intf2):
            kw = dict(bw=bw, delay=delay)
            if loss:
                kw["loss"] = loss
            intf.config(**kw)


def ensure_users():
    if not os.path.exists(os.path.join(ROOT, "config", "users.json")):
        os.system("python3 %s init-demo" % os.path.join(ROOT, "tools", "manage_users.py"))


def start_server(net, max_clients=8, extra=""):
    ensure_users()
    srv = net.get("srv")
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    srv.cmd("pkill -f server/file_server.py")
    srv.cmd("cd %s && python3 server/file_server.py --max-clients %d %s > logs/server.log 2>&1 &"
            % (ROOT, max_clients, extra))
    time.sleep(1.5)


def stop_server(net):
    net.get("srv").cmd("pkill -f server/file_server.py")


def stop(net):
    stop_server(net)
    try:
        net.mgmt_link.delete()
    except Exception:
        pass
    net.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-server", action="store_true")
    ap.add_argument("--max-clients", type=int, default=8)
    ap.add_argument("--core-bw", type=float, default=NC.CORE_BW_MBPS)
    ap.add_argument("--loss", type=float, default=0)
    a = ap.parse_args()
    os.chdir(ROOT)          # host shells inherit this cwd, so relative paths in the CLI work
    setLogLevel("info")
    net = build(core_bw=a.core_bw, core_loss=a.loss)
    if not a.no_server:
        start_server(net, a.max_clients)
        info("*** file server running on srv %s:%d (log: logs/server.log)\n" % (NC.SERVER_IP, NC.SERVER_PORT))
    info("*** try:  h1 python3 client/client.py --user alice --password alice123 list\n")
    CLI(net)
    stop(net)


if __name__ == "__main__":
    main()
