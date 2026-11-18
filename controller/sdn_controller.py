"""
SDN controller for the Concurrent File Sharing System (OpenFlow 1.3).

Runs on Ryu (`ryu-manager`) or its maintained fork os-ken (`osken-manager`).

Responsibilities
  * Routing      : proxy-ARP + per-connection (5-tuple) path installation.
  * Load balance : file-transfer connections are spread over the two
                   equal-cost core paths (dynamic mode) or pinned to the
                   first path (static baseline mode).
  * Access policy: denied hosts cannot reach the file service; only allowed
                   TCP ports on the server are reachable (firewall).
  * Dynamic block: the file server reports failed logins; after N failures
                   in a window the client IP is blocked in the network.
  * Monitoring   : port/flow statistics every second -> link utilisation and
                   per-client bandwidth (logs/*.csv + `ctl.py watch`).
  * Failover     : on link failure, affected transfers are moved to the
                   surviving path immediately.

Control/event socket (JSON lines, TCP :9090) - used by the file server and
by controller/ctl.py. See README for the message formats.
"""
import collections
import csv
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:  # Ryu
    from ryu.base import app_manager
    from ryu.controller import ofp_event
    from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
    from ryu.ofproto import ofproto_v1_3
    from ryu.lib.packet import packet, ethernet, arp, ipv4, tcp, ether_types
    from ryu.lib import hub
except ImportError:  # os-ken (drop-in fork, works on newer Python)
    from os_ken.base import app_manager
    from os_ken.controller import ofp_event
    from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
    from os_ken.ofproto import ofproto_v1_3
    from os_ken.lib.packet import packet, ethernet, arp, ipv4, tcp, ether_types
    from os_ken.lib import hub

BaseApp = getattr(app_manager, "RyuApp", None) or getattr(app_manager, "OSKenApp")

from common import netconfig as NC
from routing import NetworkGraph, PathSelector

COOKIE_CONN = 0x10   # per-connection file-transfer flows
COOKIE_PAIR = 0x20   # generic host-pair flows (ICMP etc.)
COOKIE_ACL = 0x30    # policy / firewall drops
COOKIE_BLOCK = 0x40  # dynamic blocks

PRIO_BLOCK, PRIO_ACL, PRIO_CONN, PRIO_PAIR, PRIO_NOISE, PRIO_MISS = 300, 250, 100, 50, 1, 0

LOG_DIR = os.path.join(ROOT, "logs")


class FileShareController(BaseApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(FileShareController, self).__init__(*args, **kwargs)
        with open(os.path.join(ROOT, "config", "policy.json")) as f:
            self.policy = json.load(f)
        self.graph = NetworkGraph(NC.SWITCHES, NC.CORE_LINKS)
        self.selector = PathSelector(self.graph, os.environ.get("FS_ROUTING", "dynamic"))
        self.failover = os.environ.get("FS_FAILOVER", "1") != "0"

        self.datapaths = {}
        self.ip_host = NC.ip_to_host()
        self.blocked = {}                                  # ip -> expiry (epoch)
        self.auth_failures = collections.defaultdict(collections.deque)
        self.port_prev = {}                                # (dpid, port) -> (tx, rx, t)
        self.port_rate = {}                                # (dpid, port) -> (tx_mbps, rx_mbps)
        self.flow_prev = {}                                # flow key -> (bytes, t)
        self.client_rate = collections.defaultdict(lambda: {"down": 0.0, "up": 0.0})
        self.client_total = collections.defaultdict(lambda: {"down": 0, "up": 0})
        self.transfers = {}                                # conn -> info from server
        self.recent_events = collections.deque(maxlen=50)

        os.makedirs(LOG_DIR, exist_ok=True)
        self.link_csv = open(os.path.join(LOG_DIR, "link_util.csv"), "a", newline="")
        self.client_csv = open(os.path.join(LOG_DIR, "client_usage.csv"), "a", newline="")
        self.event_log = open(os.path.join(LOG_DIR, "controller_events.jsonl"), "a")
        self.link_w = csv.writer(self.link_csv)
        self.client_w = csv.writer(self.client_csv)
        if self.link_csv.tell() == 0:
            self.link_w.writerow(["ts", "link", "up", "mbps", "util_pct"])
        if self.client_csv.tell() == 0:
            self.client_w.writerow(["ts", "client_ip", "down_mbps", "up_mbps", "down_total_mb", "up_total_mb"])

        self.logger.info("FileShareController: routing=%s failover=%s", self.selector.mode, self.failover)
        hub.spawn(self._monitor_loop)
        hub.spawn(self._control_server)

    # ================================================================ helpers
    def _log_event(self, kind, **data):
        rec = {"ts": round(time.time(), 3), "event": kind}
        rec.update(data)
        self.recent_events.append(rec)
        self.event_log.write(json.dumps(rec) + "\n")
        self.event_log.flush()
        self.logger.info("[EVENT] %s %s", kind, json.dumps(data))

    def _add_flow(self, dp, priority, match, actions, cookie=0, idle=0, hard=0, notify=False):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)] if actions else []
        flags = ofp.OFPFF_SEND_FLOW_REM if notify else 0
        dp.send_msg(parser.OFPFlowMod(datapath=dp, cookie=cookie, priority=priority, match=match,
                                      instructions=inst, idle_timeout=idle, hard_timeout=hard,
                                      flags=flags))

    def _del_flows(self, dp, cookie, match=None, priority=None):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        match = match or parser.OFPMatch()
        if priority is None:
            msg = parser.OFPFlowMod(datapath=dp, cookie=cookie, cookie_mask=0xFFFFFFFFFFFFFFFF,
                                    command=ofp.OFPFC_DELETE, out_port=ofp.OFPP_ANY,
                                    out_group=ofp.OFPG_ANY, match=match)
        else:
            msg = parser.OFPFlowMod(datapath=dp, cookie=cookie, cookie_mask=0xFFFFFFFFFFFFFFFF,
                                    command=ofp.OFPFC_DELETE_STRICT, priority=priority,
                                    out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY, match=match)
        dp.send_msg(msg)

    def _packet_out(self, dp, in_port, data, actions):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                        in_port=in_port, actions=actions, data=data))

    def _host(self, ip):
        name = self.ip_host.get(ip)
        return NC.HOSTS[name] if name else None

    def _sw(self, name):
        return NC.SWITCHES[name]

    # ================================================================ switch setup
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        self.datapaths[dp.id] = dp
        # wipe anything left over from a previous controller run
        dp.send_msg(parser.OFPFlowMod(datapath=dp, command=ofp.OFPFC_DELETE, table_id=ofp.OFPTT_ALL,
                                      out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                      match=parser.OFPMatch()))
        # table-miss -> controller (whole packet, no buffering)
        self._add_flow(dp, PRIO_MISS, parser.OFPMatch(),
                       [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])
        # silence IPv6 and LLDP noise
        self._add_flow(dp, PRIO_NOISE, parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IPV6), [])
        self._add_flow(dp, PRIO_NOISE, parser.OFPMatch(eth_type=ether_types.ETH_TYPE_LLDP), [])
        # re-apply any active blocks
        for ip, exp in self.blocked.items():
            self._install_block(ip, max(1, int(exp - time.time())))
        self.logger.info("Switch %s (dpid=%d) connected", NC.dpid_to_name().get(dp.id, "?"), dp.id)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change(self, ev):
        dp = ev.datapath
        if ev.state == DEAD_DISPATCHER and dp.id in self.datapaths:
            del self.datapaths[dp.id]
            self.logger.warning("Switch dpid=%s disconnected", dp.id)

    # ================================================================ packet-in
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype in (ether_types.ETH_TYPE_LLDP, ether_types.ETH_TYPE_IPV6):
            return
        a = pkt.get_protocol(arp.arp)
        if a:
            self._handle_arp(dp, in_port, eth, a)
            return
        ip = pkt.get_protocol(ipv4.ipv4)
        if ip is None:
            return
        src, dst = self._host(ip.src), self._host(ip.dst)
        if src is None or dst is None:
            return
        parser = dp.ofproto_parser

        # ---- dynamic block (brute-force protection)
        if self._is_blocked(ip.src):
            self._install_block(ip.src, int(self.blocked[ip.src] - time.time()) + 1)
            return

        t = pkt.get_protocol(tcp.tcp)
        # ---- firewall: only allowed TCP ports on the server
        if t and ip.dst == NC.SERVER_IP and t.dst_port not in self.policy["allowed_server_tcp_ports"]:
            m = parser.OFPMatch(eth_type=0x0800, ip_proto=6, ipv4_src=ip.src, ipv4_dst=ip.dst,
                                tcp_dst=t.dst_port)
            self._add_flow(dp, PRIO_ACL, m, [], cookie=COOKIE_ACL, idle=60)
            self._log_event("firewall_drop", src=ip.src, dst_port=t.dst_port)
            return
        # ---- access policy: denied hosts may not use the file service
        if t and ip.dst == NC.SERVER_IP and t.dst_port == NC.SERVER_PORT \
                and ip.src in self.policy["denied_hosts"]:
            m = parser.OFPMatch(eth_type=0x0800, ip_proto=6, ipv4_src=ip.src, ipv4_dst=ip.dst,
                                tcp_dst=NC.SERVER_PORT)
            self._add_flow(dp, PRIO_ACL, m, [], cookie=COOKIE_ACL, idle=60)
            self._log_event("policy_deny", src=ip.src, reason="host in denied_hosts")
            return

        # ---- file-transfer connection -> per-connection path
        conn = None
        if t and ip.dst == NC.SERVER_IP and t.dst_port == NC.SERVER_PORT:
            conn = (ip.src, t.src_port)
        elif t and ip.src == NC.SERVER_IP and t.src_port == NC.SERVER_PORT:
            conn = (ip.dst, t.dst_port)
        if conn:
            path = self.selector.conn_path.get(conn)
            if path is None or not self.graph.path_is_up(path):
                path = self._choose_conn_path(conn)
            if path is None:
                return
            self._install_conn(conn, path)
        else:
            path = self._install_pair(ip.src, ip.dst)
            if path is None:
                return
        if dp.id in path:
            self._packet_out(dp, in_port, msg.data, [parser.OFPActionOutput(dp.ofproto.OFPP_TABLE)])

    def _handle_arp(self, dp, in_port, eth, a):
        """Proxy-ARP: answer from the static host table so we never flood a looped topology."""
        if a.opcode != arp.ARP_REQUEST:
            return
        target = self._host(a.dst_ip)
        if target is None:
            return
        reply = packet.Packet()
        reply.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP,
                                             dst=eth.src, src=target["mac"]))
        reply.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=target["mac"], src_ip=a.dst_ip,
                                   dst_mac=a.src_mac, dst_ip=a.src_ip))
        reply.serialize()
        parser, ofp = dp.ofproto_parser, dp.ofproto
        self._packet_out(dp, ofp.OFPP_CONTROLLER, reply.data, [parser.OFPActionOutput(in_port)])

    # ================================================================ path install
    def _conn_endpoints(self, conn):
        client = self._host(conn[0])
        server = NC.HOSTS["srv"]
        return client, server

    def _choose_conn_path(self, conn, current=None):
        client, server = self._conn_endpoints(conn)
        cands = self.graph.paths(self._sw(client["switch"]), self._sw(server["switch"]))
        return self.selector.choose(cands, conn=conn, current=current)

    def _install_conn(self, conn, path, old_path=None):
        """Install both directions of one TCP connection along `path`."""
        client_ip, client_port = conn
        client, server = self._conn_endpoints(conn)
        idle = self.policy["flow_idle_timeout_s"]
        fwd = self.graph.hops(path, server["port"])
        rev = self.graph.hops(list(reversed(path)), client["port"])

        def match(dp, src_ip, dst_ip, sport, dport):
            return dp.ofproto_parser.OFPMatch(eth_type=0x0800, ip_proto=6, ipv4_src=src_ip,
                                              ipv4_dst=dst_ip, tcp_src=sport, tcp_dst=dport)

        # install interior switches first, edge switches last, so the switch-over is clean
        order = sorted(range(len(path)), key=lambda i: 0 if 0 < i < len(path) - 1 else 1)
        for i in order:
            dpid, out = fwd[i]
            dp = self.datapaths.get(dpid)
            if dp:
                self._add_flow(dp, PRIO_CONN, match(dp, client_ip, NC.SERVER_IP, client_port, NC.SERVER_PORT),
                               [dp.ofproto_parser.OFPActionOutput(out)], COOKIE_CONN, idle=idle, notify=True)
            dpid, out = rev[len(path) - 1 - i]
            dp = self.datapaths.get(dpid)
            if dp:
                self._add_flow(dp, PRIO_CONN, match(dp, NC.SERVER_IP, client_ip, NC.SERVER_PORT, client_port),
                               [dp.ofproto_parser.OFPActionOutput(out)], COOKIE_CONN, idle=idle, notify=True)
        # remove stale entries on switches that are no longer on the path
        if old_path:
            for dpid in set(old_path) - set(path):
                dp = self.datapaths.get(dpid)
                if dp:
                    self._del_flows(dp, COOKIE_CONN, match(dp, client_ip, NC.SERVER_IP, client_port, NC.SERVER_PORT), PRIO_CONN)
                    self._del_flows(dp, COOKIE_CONN, match(dp, NC.SERVER_IP, client_ip, NC.SERVER_PORT, client_port), PRIO_CONN)
        if self.selector.conn_path.get(conn) != path:
            self.logger.info("conn %s:%s -> path %s", client_ip, client_port, self.graph.path_name(path))
        self.selector.conn_path[conn] = path

    def _install_pair(self, src_ip, dst_ip):
        """Generic routing (ICMP etc.) along the first live path, both directions."""
        s, d = self._host(src_ip), self._host(dst_ip)
        cands = self.graph.paths(self._sw(s["switch"]), self._sw(d["switch"]))
        if not cands:
            return None
        path = cands[0]
        # Traffic to/from the server: cache ICMP only, so every TCP SYN still reaches
        # packet_in and goes through the firewall / access-policy checks.
        extra = {"ip_proto": 1} if NC.SERVER_IP in (src_ip, dst_ip) else {}
        for a, b, p in ((s, d, path), (d, s, list(reversed(path)))):
            for dpid, out in self.graph.hops(p, b["port"]):
                dp = self.datapaths.get(dpid)
                if dp:
                    m = dp.ofproto_parser.OFPMatch(eth_type=0x0800, ipv4_src=a["ip"], ipv4_dst=b["ip"],
                                                   **extra)
                    self._add_flow(dp, PRIO_PAIR, m, [dp.ofproto_parser.OFPActionOutput(out)],
                                   COOKIE_PAIR, idle=20)
        return path

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def flow_removed(self, ev):
        msg = ev.msg
        ofp = msg.datapath.ofproto
        if msg.cookie != COOKIE_CONN or msg.reason == ofp.OFPRR_DELETE:
            return
        m = msg.match
        if m.get("tcp_dst") == NC.SERVER_PORT:
            conn = (m.get("ipv4_src"), m.get("tcp_src"))
        else:
            conn = (m.get("ipv4_dst"), m.get("tcp_dst"))
        path = self.selector.conn_path.get(conn)
        if path and msg.datapath.id in path:
            self.selector.forget(conn)
            self.transfers.pop(conn, None)

    # ================================================================ link failure
    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def port_status(self, ev):
        msg = ev.msg
        dp, ofp, desc = msg.datapath, msg.datapath.ofproto, msg.desc
        up = not (desc.state & ofp.OFPPS_LINK_DOWN) and not (desc.config & ofp.OFPPC_PORT_DOWN)
        if msg.reason == ofp.OFPPR_DELETE:
            up = False
        link, changed = self.graph.set_port_state(dp.id, desc.port_no, up)
        if not changed:
            return
        self._log_event("link_up" if up else "link_down", link=link.name)
        if up:
            return
        # generic flows are cheap to relearn
        for d in self.datapaths.values():
            self._del_flows(d, COOKIE_PAIR)
        if not self.failover:
            self.logger.warning("failover disabled: transfers on %s are left stranded", link.name)
            return
        t0 = time.time()
        moved = 0
        for conn in self.selector.conns_on_link(link):
            old = self.selector.conn_path[conn]
            new = self._choose_conn_path(conn)
            if new is None:
                self.selector.forget(conn)
                continue
            self._install_conn(conn, new, old_path=old)
            moved += 1
        self._log_event("reroute", link=link.name, connections_moved=moved,
                        controller_ms=round((time.time() - t0) * 1000, 2))

    # ================================================================ blocking
    def _is_blocked(self, ip):
        exp = self.blocked.get(ip)
        if exp is None:
            return False
        if exp < time.time():
            del self.blocked[ip]
            return False
        return True

    def _install_block(self, ip, duration):
        host = self._host(ip)
        if host is None:
            return
        dp = self.datapaths.get(self._sw(host["switch"]))
        if dp:
            m = dp.ofproto_parser.OFPMatch(eth_type=0x0800, ipv4_src=ip)
            self._add_flow(dp, PRIO_BLOCK, m, [], COOKIE_BLOCK, hard=max(1, duration))

    def block_ip(self, ip, duration, reason):
        self.blocked[ip] = time.time() + duration
        self._install_block(ip, duration)
        self._log_event("block", ip=ip, duration_s=duration, reason=reason)

    def unblock_ip(self, ip):
        self.blocked.pop(ip, None)
        self.auth_failures.pop(ip, None)
        host = self._host(ip)
        if host:
            dp = self.datapaths.get(self._sw(host["switch"]))
            if dp:
                m = dp.ofproto_parser.OFPMatch(eth_type=0x0800, ipv4_src=ip)
                self._del_flows(dp, COOKIE_BLOCK, m, PRIO_BLOCK)
        self._log_event("unblock", ip=ip)

    # ================================================================ app events
    def handle_app_event(self, ev):
        kind = ev.get("type")
        if kind == "auth_fail":
            ip = ev.get("client_ip")
            now = time.time()
            q = self.auth_failures[ip]
            q.append(now)
            while q and now - q[0] > self.policy["auth_fail_window_s"]:
                q.popleft()
            self._log_event("auth_fail", ip=ip, user=ev.get("user"), count=len(q))
            if len(q) >= self.policy["auth_fail_threshold"] and not self._is_blocked(ip):
                self.block_ip(ip, self.policy["block_duration_s"], "brute force: %d failed logins" % len(q))
        elif kind == "auth_ok":
            self.auth_failures.pop(ev.get("client_ip"), None)
        elif kind == "transfer_start":
            conn = (ev["client_ip"], int(ev["client_port"]))
            self.transfers[conn] = ev
            self.selector.transferring.add(conn)
            cur = self.selector.conn_path.get(conn)
            if cur is not None and self.selector.mode == "dynamic":
                new = self._choose_conn_path(conn, current=cur)
                if new and new != cur:
                    self._install_conn(conn, new, old_path=cur)
                    self._log_event("rebalance", conn="%s:%s" % conn, file=ev.get("file"),
                                    path=self.graph.path_name(new))
        elif kind == "transfer_end":
            conn = (ev["client_ip"], int(ev["client_port"]))
            self.transfers.pop(conn, None)
            self.selector.transferring.discard(conn)
            self._log_event("transfer_end", conn="%s:%s" % conn, file=ev.get("file"),
                            bytes=ev.get("bytes"), ok=ev.get("ok"))
        return {"status": "OK"}

    # ================================================================ monitoring
    def _monitor_loop(self):
        interval = self.policy["monitor_interval_s"]
        tick = 0
        while True:
            for dp in list(self.datapaths.values()):
                parser, ofp = dp.ofproto_parser, dp.ofproto
                dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY))
                if dp.id == self._sw(NC.CLIENT_SWITCH):
                    dp.send_msg(parser.OFPFlowStatsRequest(dp, cookie=COOKIE_CONN,
                                                           cookie_mask=0xFFFFFFFFFFFFFFFF))
            hub.sleep(interval)
            tick += 1
            for ip in [i for i, e in self.blocked.items() if e < time.time()]:
                self.unblock_ip(ip)
            self._write_stats()
            if tick % 5 == 0:
                self._print_summary()

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply(self, ev):
        now = time.time()
        dpid = ev.msg.datapath.id
        for st in ev.msg.body:
            key = (dpid, st.port_no)
            prev = self.port_prev.get(key)
            self.port_prev[key] = (st.tx_bytes, st.rx_bytes, now)
            if prev:
                dt = max(now - prev[2], 1e-3)
                self.port_rate[key] = (max(0, st.tx_bytes - prev[0]) * 8 / dt / 1e6,
                                       max(0, st.rx_bytes - prev[1]) * 8 / dt / 1e6)
        for link in self.graph.links:
            ra = self.port_rate.get(link.a, (0, 0))[0]
            rb = self.port_rate.get(link.b, (0, 0))[0]
            self.selector.link_mbps[link.idx] = max(ra, rb)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def flow_stats_reply(self, ev):
        now = time.time()
        rates = collections.defaultdict(lambda: {"down": 0.0, "up": 0.0})
        for st in ev.msg.body:
            m = st.match
            src, dst = m.get("ipv4_src"), m.get("ipv4_dst")
            if src is None:
                continue
            if src == NC.SERVER_IP:
                client, direction = dst, "down"
            else:
                client, direction = src, "up"
            key = (src, dst, m.get("tcp_src"), m.get("tcp_dst"))
            prev = self.flow_prev.get(key)
            self.flow_prev[key] = (st.byte_count, now)
            delta = st.byte_count if prev is None or st.byte_count < prev[0] else st.byte_count - prev[0]
            self.client_total[client][direction] += delta
            if prev:
                rates[client][direction] += delta * 8 / max(now - prev[1], 1e-3) / 1e6
        self.client_rate = rates
        heavy = self.policy["heavy_user_mbps"]
        for ip, r in rates.items():
            if r["down"] + r["up"] > heavy:
                self.logger.info("[MONITOR] heavy user %s: %.2f Mbps", ip, r["down"] + r["up"])

    def _write_stats(self):
        ts = round(time.time(), 3)
        for link in self.graph.links:
            mbps = self.selector.link_mbps.get(link.idx, 0.0)
            self.link_w.writerow([ts, link.name, int(link.up), round(mbps, 3),
                                  round(100 * mbps / NC.CORE_BW_MBPS, 1)])
        for ip, r in self.client_rate.items():
            tot = self.client_total[ip]
            self.client_w.writerow([ts, ip, round(r["down"], 3), round(r["up"], 3),
                                    round(tot["down"] / 1e6, 3), round(tot["up"] / 1e6, 3)])
        self.link_csv.flush()
        self.client_csv.flush()

    def _print_summary(self):
        parts = ["%s=%s%.1fMbps" % (l.name, "" if l.up else "DOWN:", self.selector.link_mbps.get(l.idx, 0))
                 for l in self.graph.links]
        self.logger.info("[MONITOR] mode=%s conns=%d transfers=%d blocked=%s | %s",
                         self.selector.mode, len(self.selector.conn_path), len(self.selector.transferring),
                         list(self.blocked), " ".join(parts))

    def stats(self):
        now = time.time()
        return {
            "status": "OK",
            "mode": self.selector.mode,
            "failover": self.failover,
            "switches": sorted(self.datapaths),
            "links": [{"name": l.name, "up": l.up, "mbps": round(self.selector.link_mbps.get(l.idx, 0), 3),
                       "util_pct": round(100 * self.selector.link_mbps.get(l.idx, 0) / NC.CORE_BW_MBPS, 1)}
                      for l in self.graph.links],
            "connections": [{"client": "%s:%s" % c, "path": self.graph.path_name(p),
                             "transferring": c in self.selector.transferring,
                             "file": self.transfers.get(c, {}).get("file")}
                            for c, p in self.selector.conn_path.items()],
            "clients": {ip: {"down_mbps": round(r["down"], 3), "up_mbps": round(r["up"], 3),
                             "down_total_mb": round(self.client_total[ip]["down"] / 1e6, 3),
                             "up_total_mb": round(self.client_total[ip]["up"] / 1e6, 3)}
                        for ip, r in self.client_rate.items()},
            "blocked": {ip: round(e - now, 1) for ip, e in self.blocked.items()},
            "recent_events": list(self.recent_events)[-10:],
        }

    # ================================================================ control socket
    def _control_server(self):
        self.logger.info("Control/event socket listening on 0.0.0.0:%d", NC.CONTROLLER_EVENT_PORT)
        server = hub.StreamServer(("0.0.0.0", NC.CONTROLLER_EVENT_PORT), self._control_client)
        server.serve_forever()

    def _control_client(self, sock, addr):
        buf = b""
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        resp = self._dispatch(json.loads(line.decode()))
                    except Exception as e:  # never let a bad message kill the controller
                        resp = {"status": "ERR", "error": str(e)}
                    sock.sendall((json.dumps(resp) + "\n").encode())
        except Exception as e:
            self.logger.debug("control client %s error: %s", addr, e)
        finally:
            sock.close()

    def _dispatch(self, req):
        cmd = req.get("cmd")
        if cmd == "event":
            return self.handle_app_event(req)
        if cmd == "stats":
            return self.stats()
        if cmd == "set_mode":
            if req.get("routing") in ("static", "dynamic"):
                self.selector.mode = req["routing"]
            if "failover" in req:
                self.failover = bool(req["failover"])
            self._log_event("mode", routing=self.selector.mode, failover=self.failover)
            return {"status": "OK", "mode": self.selector.mode, "failover": self.failover}
        if cmd == "block":
            self.block_ip(req["ip"], int(req.get("duration", self.policy["block_duration_s"])), "admin")
            return {"status": "OK"}
        if cmd == "unblock":
            self.unblock_ip(req["ip"])
            return {"status": "OK"}
        if cmd == "reset_stats":
            self.client_total.clear()
            return {"status": "OK"}
        return {"status": "ERR", "error": "unknown cmd %r" % cmd}
