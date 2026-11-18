"""Unit tests for the controller's routing / load-balancing logic (no Ryu needed)."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "controller"))

from common import netconfig as NC
from routing import NetworkGraph, PathSelector

A = [1, 2, 4]   # s1-s2-s4
B = [1, 3, 4]   # s1-s3-s4


class TestGraph(unittest.TestCase):
    def setUp(self):
        self.g = NetworkGraph(NC.SWITCHES, NC.CORE_LINKS)

    def test_two_equal_cost_paths(self):
        self.assertEqual(self.g.paths(1, 4), [A, B])

    def test_hops_use_configured_ports(self):
        self.assertEqual(self.g.hops(A, 1), [(1, 10), (2, 2), (4, 1)])
        self.assertEqual(self.g.hops(list(reversed(B)), 3), [(4, 11), (3, 1), (1, 3)])

    def test_link_failure_removes_path(self):
        link, changed = self.g.set_port_state(1, 10, False)
        self.assertTrue(changed)
        self.assertEqual(link.name, "s1-s2")
        self.assertEqual(self.g.paths(1, 4), [B])
        self.assertFalse(self.g.path_is_up(A))
        # second end going down is not a new change
        self.assertFalse(self.g.set_port_state(2, 1, False)[1])
        # link only comes back when both ends are up
        self.assertFalse(self.g.set_port_state(1, 10, True)[1])
        self.assertTrue(self.g.set_port_state(2, 1, True)[1])
        self.assertEqual(self.g.paths(1, 4), [A, B])

    def test_no_path(self):
        self.g.set_port_state(1, 10, False)
        self.g.set_port_state(1, 11, False)
        self.assertEqual(self.g.paths(1, 4), [])

    def test_unknown_port_ignored(self):
        self.assertEqual(self.g.set_port_state(1, 1, False), (None, False))


class TestSelector(unittest.TestCase):
    def setUp(self):
        self.g = NetworkGraph(NC.SWITCHES, NC.CORE_LINKS)

    def assign(self, sel, conn):
        cur = sel.conn_path.get(conn)
        p = sel.choose(self.g.paths(1, 4), conn=conn, current=cur)
        sel.conn_path[conn] = p
        sel.transferring.add(conn)
        return p

    def test_static_always_first_path(self):
        sel = PathSelector(self.g, "static")
        self.assertEqual([self.assign(sel, ("10.0.0.%d" % i, 1000)) for i in range(4)], [A] * 4)

    def test_dynamic_balances_transfers(self):
        sel = PathSelector(self.g, "dynamic")
        paths = [self.assign(sel, ("10.0.0.%d" % i, 1000)) for i in range(4)]
        self.assertEqual(paths.count(A), 2)
        self.assertEqual(paths.count(B), 2)

    def test_dynamic_uses_measured_utilisation(self):
        sel = PathSelector(self.g, "dynamic")
        sel.link_mbps[0] = 9.0   # s1-s2 busy with non-tracked traffic
        self.assertEqual(sel.choose(self.g.paths(1, 4)), B)

    def test_conns_on_link_for_failover(self):
        sel = PathSelector(self.g, "dynamic")
        sel.conn_path = {("a", 1): A, ("b", 2): B}
        link, _ = self.g.set_port_state(1, 10, False)
        self.assertEqual(sel.conns_on_link(link), [("a", 1)])
        self.assertEqual(sel.choose(self.g.paths(1, 4), conn=("a", 1)), B)


if __name__ == "__main__":
    unittest.main()
