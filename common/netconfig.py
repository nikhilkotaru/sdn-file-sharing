r"""
Single source of truth for the emulated network.

Both the Mininet topology (topology/topo.py) and the SDN controller
(controller/sdn_controller.py) import this file, so host addresses, switch
DPIDs and port numbers always agree.

Topology (two equal-cost paths between the client switch and the server switch):

        h1 h2 h3 h4 h5                         srv (file server)
         \  |  |  | /                             |
             [s1] --p10---- p1[s2]p2 ----p10-- [s4] ---- mgmt link ---> root namespace
               \                                 /                       (controller :9090)
                p11---- p1[s3]p2 ----------p11--
"""

# ---------------------------------------------------------------- application
SERVER_IP = "10.0.0.100"
SERVER_MAC = "00:00:00:00:00:64"
SERVER_PORT = 5000                 # TCP port of the file-sharing service

# Out-of-band management link between the server host and the root namespace,
# used by the file server to report application events to the controller.
MGMT_SERVER_IP = "192.168.100.2"
MGMT_ROOT_IP = "192.168.100.1"
CONTROLLER_EVENT_PORT = 9090       # controller's JSON control/event socket
OPENFLOW_PORT = 6653

# ---------------------------------------------------------------- switches
SWITCHES = {"s1": 1, "s2": 2, "s3": 3, "s4": 4}   # name -> datapath id

CLIENT_SWITCH = "s1"
SERVER_SWITCH = "s4"

# ---------------------------------------------------------------- hosts
# port = switch port the host is attached to
HOSTS = {
    "h1":  {"ip": "10.0.0.1", "mac": "00:00:00:00:00:01", "switch": "s1", "port": 1},
    "h2":  {"ip": "10.0.0.2", "mac": "00:00:00:00:00:02", "switch": "s1", "port": 2},
    "h3":  {"ip": "10.0.0.3", "mac": "00:00:00:00:00:03", "switch": "s1", "port": 3},
    "h4":  {"ip": "10.0.0.4", "mac": "00:00:00:00:00:04", "switch": "s1", "port": 4},
    "h5":  {"ip": "10.0.0.5", "mac": "00:00:00:00:00:05", "switch": "s1", "port": 5},  # untrusted guest
    "srv": {"ip": SERVER_IP, "mac": SERVER_MAC, "switch": "s4", "port": 1},
}

# ---------------------------------------------------------------- core links
# (switch_a, port_a, switch_b, port_b)
CORE_LINKS = [
    ("s1", 10, "s2", 1),    # path A, hop 1
    ("s2", 2, "s4", 10),    # path A, hop 2
    ("s1", 11, "s3", 1),    # path B, hop 1
    ("s3", 2, "s4", 11),    # path B, hop 2
]

CORE_BW_MBPS = 10          # deliberately small so contention is visible
CORE_DELAY = "2ms"
ACCESS_BW_MBPS = 100


def ip_to_host():
    return {h["ip"]: name for name, h in HOSTS.items()}


def dpid_to_name():
    return {dpid: name for name, dpid in SWITCHES.items()}
