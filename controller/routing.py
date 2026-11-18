"""
Pure routing / load-balancing logic used by the SDN controller.

Kept free of any Ryu/os-ken imports so it can be unit-tested on any machine
(see tests/test_routing.py).
"""
from __future__ import annotations


class Link:
    def __init__(self, idx, dpid_a, port_a, dpid_b, port_b, name):
        self.idx = idx
        self.a = (dpid_a, port_a)
        self.b = (dpid_b, port_b)
        self.name = name
        self.port_up = {self.a: True, self.b: True}

    @property
    def up(self):
        return all(self.port_up.values())


class NetworkGraph:
    """Switch-level graph with per-port up/down state."""

    def __init__(self, switches, links):
        """
        switches: {name: dpid}
        links:    [(sw_a, port_a, sw_b, port_b), ...]
        """
        self.dpid_name = {d: n for n, d in switches.items()}
        self.links = []
        self.port_link = {}            # (dpid, port) -> Link
        for i, (sa, pa, sb, pb) in enumerate(links):
            da, db = switches[sa], switches[sb]
            link = Link(i, da, pa, db, pb, "%s-%s" % (sa, sb))
            self.links.append(link)
            self.port_link[(da, pa)] = link
            self.port_link[(db, pb)] = link

    # ------------------------------------------------------------ state
    def set_port_state(self, dpid, port, up):
        """Returns (link, changed) – changed is True if the *link* state flipped."""
        link = self.port_link.get((dpid, port))
        if link is None:
            return None, False
        before = link.up
        link.port_up[(dpid, port)] = up
        return link, before != link.up

    def neighbours(self, dpid):
        """Yields (neighbour_dpid, out_port, link) over links that are up."""
        for link in self.links:
            if not link.up:
                continue
            if link.a[0] == dpid:
                yield link.b[0], link.a[1], link
            elif link.b[0] == dpid:
                yield link.a[0], link.b[1], link

    # ------------------------------------------------------------ paths
    def paths(self, src, dst, max_extra_hops=0):
        """All loop-free paths src->dst (lists of dpids) over live links.

        Only paths with length <= shortest + max_extra_hops are returned, sorted
        by (length, dpid sequence) so that ordering is deterministic.
        """
        if src == dst:
            return [[src]]
        found = []

        def dfs(node, path):
            for nb, _port, _link in self.neighbours(node):
                if nb in path:
                    continue
                if nb == dst:
                    found.append(path + [nb])
                else:
                    dfs(nb, path + [nb])

        dfs(src, [src])
        if not found:
            return []
        shortest = min(len(p) for p in found)
        found = [p for p in found if len(p) <= shortest + max_extra_hops]
        found.sort(key=lambda p: (len(p), p))
        return found

    def out_port(self, dpid, next_dpid):
        for nb, port, _link in self.neighbours(dpid):
            if nb == next_dpid:
                return port
        return None

    def path_links(self, path):
        """Link objects traversed by a dpid path (regardless of up/down)."""
        out = []
        for u, v in zip(path, path[1:]):
            for link in self.links:
                ends = {link.a[0], link.b[0]}
                if ends == {u, v}:
                    out.append(link)
                    break
        return out

    def path_is_up(self, path):
        return all(l.up for l in self.path_links(path)) and len(self.path_links(path)) == len(path) - 1

    def path_name(self, path):
        return "-".join(self.dpid_name.get(d, str(d)) for d in path)

    def hops(self, path, final_port):
        """[(dpid, out_port)] for every switch along path; last hop exits on final_port."""
        hops = []
        for i, dpid in enumerate(path):
            if i == len(path) - 1:
                hops.append((dpid, final_port))
            else:
                hops.append((dpid, self.out_port(dpid, path[i + 1])))
        return hops


class PathSelector:
    """Chooses a path for each file-transfer connection.

    static  : always the first (deterministic shortest) live path -> baseline.
    dynamic : least-loaded live path, where load =
              TRANSFER_WEIGHT * (#active transfers already on the path)
              + current measured utilisation of the busiest link (Mbps).
    """

    TRANSFER_WEIGHT = 5.0      # Mbps-equivalent cost of one active transfer

    def __init__(self, graph: NetworkGraph, mode="dynamic"):
        self.graph = graph
        self.mode = mode
        self.link_mbps = {}        # link idx -> measured Mbps (max of both directions)
        self.conn_path = {}        # conn key -> path (list of dpids)
        self.transferring = set()  # conn keys with an active file transfer

    def path_load(self, path, exclude=None):
        links = {l.idx for l in self.graph.path_links(path)}
        transfers = 0
        for conn in self.transferring:
            if conn == exclude:
                continue
            other = self.conn_path.get(conn)
            if other and links & {l.idx for l in self.graph.path_links(other)}:
                transfers += 1
        util = max([self.link_mbps.get(i, 0.0) for i in links] or [0.0])
        return self.TRANSFER_WEIGHT * transfers + util, transfers, util

    def choose(self, candidates, conn=None, current=None):
        if not candidates:
            return None
        if self.mode == "static":
            return candidates[0]
        best, best_score = None, None
        for p in candidates:
            score = self.path_load(p, exclude=conn)[0]
            # stickiness: prefer the current path on ties to avoid flapping
            if current is not None and p == current:
                score -= 0.01
            if best_score is None or score < best_score:
                best, best_score = p, score
        return best

    def forget(self, conn):
        self.conn_path.pop(conn, None)
        self.transferring.discard(conn)

    def conns_on_link(self, link):
        return [c for c, p in self.conn_path.items()
                if link.idx in {l.idx for l in self.graph.path_links(p)}]
