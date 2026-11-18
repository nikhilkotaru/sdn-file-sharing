"""
Controller tests with simulated OpenFlow 1.3 switches.

Real OpenFlow messages (PacketIn / PortStatus) built with the os-ken/Ryu
parser are fed to the controller; the FlowMods / PacketOuts it emits are
captured and checked. Skipped if neither os-ken nor Ryu is installed.
"""
import os
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "controller"))

try:
    try:
        from ryu.ofproto import ofproto_v1_3 as ofp, ofproto_v1_3_parser as parser
        from ryu.lib.packet import packet, ethernet, arp, ipv4, tcp
        from ryu.lib import hub
    except ImportError:
        from os_ken.ofproto import ofproto_v1_3 as ofp, ofproto_v1_3_parser as parser
        from os_ken.lib.packet import packet, ethernet, arp, ipv4, tcp
        from os_ken.lib import hub
    HAVE_SDN = True
except ImportError:
    HAVE_SDN = False

from common import netconfig as NC

H = NC.HOSTS


class FakeDP:
    def __init__(self, dpid):
        self.id = dpid
        self.ofproto = ofp
        self.ofproto_parser = parser
        self.sent = []

    def send_msg(self, m):
        self.sent.append(m)

    def flowmods(self, command=None):
        return [m for m in self.sent if isinstance(m, parser.OFPFlowMod)
                and (command is None or m.command == command)]

    def outs(self):
        return [m for m in self.sent if isinstance(m, parser.OFPPacketOut)]


def out_port(fm):
    for inst in fm.instructions:
        for a in inst.actions:
            return a.port
    return None  # drop


@unittest.skipUnless(HAVE_SDN, "os-ken / ryu not installed")
class ControllerTest(unittest.TestCase):
    def setUp(self):
        import sdn_controller as sc
        sc.LOG_DIR = tempfile.mkdtemp()
        self._spawn = sc.hub.spawn
        sc.hub.spawn = lambda *a, **k: None      # no background threads in tests
        os.environ["FS_ROUTING"] = "dynamic"
        self.c = sc.FileShareController()
        self.sc = sc
        self.dps = {d: FakeDP(d) for d in (1, 2, 3, 4)}
        for dp in self.dps.values():
            self.c.switch_features(types.SimpleNamespace(msg=types.SimpleNamespace(datapath=dp)))
            dp.sent.clear()

    def tearDown(self):
        self.sc.hub.spawn = self._spawn

    def packet_in(self, dpid, in_port, pkt):
        pkt.serialize()
        msg = parser.OFPPacketIn(self.dps[dpid], buffer_id=ofp.OFP_NO_BUFFER, total_len=len(pkt.data),
                                 reason=0, table_id=0, cookie=0,
                                 match=parser.OFPMatch(in_port=in_port), data=pkt.data)
        self.c.packet_in(types.SimpleNamespace(msg=msg))

    def syn(self, host, sport, dport=NC.SERVER_PORT):
        h = H[host]
        p = packet.Packet()
        p.add_protocol(ethernet.ethernet(dst=NC.SERVER_MAC, src=h["mac"], ethertype=0x0800))
        p.add_protocol(ipv4.ipv4(src=h["ip"], dst=NC.SERVER_IP, proto=6))
        p.add_protocol(tcp.tcp(src_port=sport, dst_port=dport, bits=tcp.TCP_SYN))
        self.packet_in(1, h["port"], p)

    def conn_flows(self, dpid):
        return [f for f in self.dps[dpid].flowmods(ofp.OFPFC_ADD) if f.cookie == self.sc.COOKIE_CONN]

    # ------------------------------------------------------------------ tests
    def test_switch_setup_installs_table_miss(self):
        dp = FakeDP(9)
        self.c.switch_features(types.SimpleNamespace(msg=types.SimpleNamespace(datapath=dp)))
        miss = [f for f in dp.flowmods(ofp.OFPFC_ADD) if f.priority == 0]
        self.assertEqual(out_port(miss[0]), ofp.OFPP_CONTROLLER)

    def test_proxy_arp(self):
        p = packet.Packet()
        p.add_protocol(ethernet.ethernet(dst="ff:ff:ff:ff:ff:ff", src=H["h1"]["mac"], ethertype=0x0806))
        p.add_protocol(arp.arp(opcode=arp.ARP_REQUEST, src_mac=H["h1"]["mac"], src_ip=H["h1"]["ip"],
                               dst_mac="00:00:00:00:00:00", dst_ip=NC.SERVER_IP))
        self.packet_in(1, 1, p)
        out = self.dps[1].outs()[0]
        reply = packet.Packet(out.data).get_protocol(arp.arp)
        self.assertEqual(reply.opcode, arp.ARP_REPLY)
        self.assertEqual(reply.src_mac, NC.SERVER_MAC)
        self.assertEqual(out.actions[0].port, 1)

    def test_file_connection_installs_bidirectional_path(self):
        self.syn("h1", 40000)
        # path s1-s2-s4: s1 forward out port 10, s2 out 2, s4 out to server port 1
        fwd = {d: [out_port(f) for f in self.conn_flows(d) if f.match["ipv4_src"] == H["h1"]["ip"]]
               for d in (1, 2, 4)}
        self.assertEqual(fwd, {1: [10], 2: [2], 4: [1]})
        rev = [out_port(f) for f in self.conn_flows(1) if f.match["ipv4_src"] == NC.SERVER_IP]
        self.assertEqual(rev, [1])                   # back to h1
        self.assertEqual(self.conn_flows(3), [])     # path B unused
        self.assertEqual(self.dps[1].outs()[0].actions[0].port, ofp.OFPP_TABLE)

    def test_dynamic_rebalance_on_transfer_start(self):
        for i, h in enumerate(("h1", "h2")):
            self.syn(h, 40000 + i)
            self.c.handle_app_event({"type": "transfer_start", "client_ip": H[h]["ip"],
                                     "client_port": 40000 + i, "file": "x"})
        paths = sorted(self.c.graph.path_name(p) for p in self.c.selector.conn_path.values())
        self.assertEqual(paths, ["s1-s2-s4", "s1-s3-s4"])
        self.assertTrue(self.conn_flows(3))          # second transfer moved onto s3

    def test_static_mode_does_not_rebalance(self):
        self.c._dispatch({"cmd": "set_mode", "routing": "static"})
        for i, h in enumerate(("h1", "h2")):
            self.syn(h, 40000 + i)
            self.c.handle_app_event({"type": "transfer_start", "client_ip": H[h]["ip"],
                                     "client_port": 40000 + i})
        self.assertEqual({self.c.graph.path_name(p) for p in self.c.selector.conn_path.values()}, {"s1-s2-s4"})

    def test_denied_host_dropped(self):
        self.syn("h5", 40000)
        drops = [f for f in self.dps[1].flowmods(ofp.OFPFC_ADD) if f.cookie == self.sc.COOKIE_ACL]
        self.assertEqual(len(drops), 1)
        self.assertIsNone(out_port(drops[0]))
        self.assertEqual(self.c.selector.conn_path, {})
        self.assertEqual(self.dps[1].outs(), [])

    def pair_flows(self, dpid):
        return [f for f in self.dps[dpid].flowmods(ofp.OFPFC_ADD) if f.cookie == self.sc.COOKIE_PAIR]

    def test_ping_does_not_bypass_access_policy(self):
        # a ping from a denied host installs pair flows; they must cover ICMP only
        self.c._install_pair(H["h5"]["ip"], NC.SERVER_IP)
        flows = self.pair_flows(1)
        self.assertTrue(flows)
        self.assertTrue(all(f.match["ip_proto"] == 1 for f in flows))
        # so the following file-service SYN still hits the controller and is dropped
        self.syn("h5", 40000)
        drops = [f for f in self.dps[1].flowmods(ofp.OFPFC_ADD) if f.cookie == self.sc.COOKIE_ACL]
        self.assertEqual(len(drops), 1)

    def test_host_to_host_pair_flows_stay_generic(self):
        self.c._install_pair(H["h1"]["ip"], H["h2"]["ip"])
        self.assertTrue(all("ip_proto" not in f.match for f in self.pair_flows(1)))

    def test_firewall_blocks_other_server_ports(self):
        self.syn("h1", 40000, dport=22)
        drops = [f for f in self.dps[1].flowmods(ofp.OFPFC_ADD) if f.cookie == self.sc.COOKIE_ACL]
        self.assertEqual(drops[0].match["tcp_dst"], 22)

    def test_brute_force_triggers_block(self):
        ip = H["h3"]["ip"]
        for _ in range(3):
            self.c.handle_app_event({"type": "auth_fail", "client_ip": ip, "user": "carol"})
        self.assertIn(ip, self.c.blocked)
        blocks = [f for f in self.dps[1].flowmods(ofp.OFPFC_ADD) if f.priority == self.sc.PRIO_BLOCK]
        self.assertEqual(blocks[0].match["ipv4_src"], ip)
        self.assertEqual(blocks[0].hard_timeout, self.c.policy["block_duration_s"])
        self.c._dispatch({"cmd": "unblock", "ip": ip})
        self.assertNotIn(ip, self.c.blocked)

    def port_status(self, dpid, port, down):
        state = ofp.OFPPS_LINK_DOWN if down else 0
        desc = parser.OFPPort(port, "00:00:00:00:00:00", "p", 0, state, 0, 0, 0, 0, 0, 0)
        msg = parser.OFPPortStatus(self.dps[dpid], ofp.OFPPR_MODIFY, desc)
        self.c.port_status(types.SimpleNamespace(msg=msg))

    def test_link_failure_reroutes_active_connection(self):
        self.syn("h1", 40000)
        conn = (H["h1"]["ip"], 40000)
        self.assertEqual(self.c.graph.path_name(self.c.selector.conn_path[conn]), "s1-s2-s4")
        for dp in self.dps.values():
            dp.sent.clear()
        self.port_status(1, 10, down=True)            # s1-s2 fails
        self.assertEqual(self.c.graph.path_name(self.c.selector.conn_path[conn]), "s1-s3-s4")
        s1_fwd = [out_port(f) for f in self.conn_flows(1) if f.match["ipv4_src"] == H["h1"]["ip"]]
        self.assertEqual(s1_fwd, [11])
        self.assertTrue(self.conn_flows(3))
        self.assertTrue(self.dps[2].flowmods(ofp.OFPFC_DELETE_STRICT))   # stale entries removed

    def test_failover_off_leaves_flows(self):
        self.c._dispatch({"cmd": "set_mode", "failover": False})
        self.syn("h1", 40000)
        for dp in self.dps.values():
            dp.sent.clear()
        self.port_status(1, 10, down=True)
        self.assertEqual(self.conn_flows(1), [])
        self.assertEqual(self.c.graph.path_name(self.c.selector.conn_path[(H["h1"]["ip"], 40000)]), "s1-s2-s4")

    def test_stats_command(self):
        self.syn("h1", 40000)
        st = self.c._dispatch({"cmd": "stats"})
        self.assertEqual(st["status"], "OK")
        self.assertEqual(len(st["links"]), 4)
        self.assertEqual(st["connections"][0]["path"], "s1-s2-s4")


if __name__ == "__main__":
    unittest.main()
