# Concurrent File Sharing System with SDN Control

A multi-user file-sharing application built on **raw TCP sockets**. Clients upload and download files
concurrently while an **OpenFlow 1.3 SDN controller** (os-ken / Ryu) routes, polices, load-balances
and monitors the file-transfer traffic inside a **Mininet** network.

| Project requirement | Where it is implemented |
|---|---|
| Concurrent TCP file transfers | `server/file_server.py`: thread-per-session server with a bounded slot pool |
| Upload and download | `UPLOAD` / `DOWNLOAD` ops, resumable downloads, SHA-256 verification |
| File metadata | `server/metadata.py`: size, sha256, owner, version, download count, timestamps (persistent JSON) |
| Authenticated access | `server/auth.py`: PBKDF2-SHA256 salted hashes; roles `admin` / `user` / `readonly` |
| SDN access policies | `controller/sdn_controller.py`: denied hosts, server-port firewall, dynamic brute-force blocking |
| Bandwidth monitoring | Port and flow statistics every 1 s, per-link and per-client Mbps, `logs/*.csv`, `ctl.py watch` |
| Simultaneous transfers tested | `tests/`, `experiments/run_experiments.py` (E1: 1 to 8 parallel clients) |

---

## 1. Architecture

```
            h1  h2  h3  h4      h5 (guest, denied)
             \   |   |   |    /
              +-------------+         path A  (10 Mbps, 2 ms)
              |     s1      |---p10---[ s2 ]---p10---+
              +-------------+                        |
                    p11                         +---------+
                     \------------[ s3 ]---p11--|   s4    |--- srv 10.0.0.100:5000  (file server)
                              path B (10 Mbps)  +---------+         |
                                                                    | mgmt link 192.168.100.0/24
   +-------------------------------------------------------------+   |
   | root namespace:  SDN controller  (OpenFlow :6653, events :9090) <-+
   +-------------------------------------------------------------+
```

* **s1** is the access switch for the clients. **s4** connects to the server. Two equal-cost core paths
  (s1-s2-s4 and s1-s3-s4) give the controller something to load-balance and fail over between.
* Core links are limited to 10 Mbps (Mininet `TCLink`), so contention and the effect of routing are visible.
* The topology contains a loop. The controller therefore answers ARP itself (proxy ARP) and installs
  explicit paths, so no flooding is needed. Switches run with `fail-mode secure`.
* The file server reports **application events** to the controller over a separate management link
  (socket–SDN integration):

| Event (server → controller) | Controller reaction |
|---|---|
| `auth_fail` | Counts failures per IP. 3 failures within 60 s → **drop rule** for that IP at its access switch for 60 s |
| `auth_ok` | Resets the failure counter |
| `transfer_start` | Marks the connection as an active transfer. In *dynamic* mode it may **move the connection to the less-loaded path** |
| `transfer_end` | Updates load accounting |

### Communication flow (download)

```
client                     s1 / s4 (OpenFlow)            controller                server
  | TCP SYN ---------------> table-miss --PacketIn-------> choose path, install
  |                                                       5-tuple flows on s1,s2|s3,s4
  | <======================= TCP connection established =========================> |
  | AUTH {user,pw} ---------------------------------------------------------------> | verify PBKDF2
  | <---------------------------------------------------------------- OK {role} --- | --auth_ok--> ctl
  | DOWNLOAD {name, offset} -------------------------------------------------------> |
  | <--------------------------------------------- OK {size, sha256, length} ------ | --transfer_start--> ctl
  | <============================== raw file bytes ================================ |   (may reroute)
  | verify SHA-256                                                                   | --transfer_end--> ctl
```

### Flow table (single table, priorities)

| Priority | Cookie | Match | Action | Purpose |
|---|---|---|---|---|
| 300 | 0x40 | `ipv4_src=<attacker>` | drop (hard timeout) | dynamic brute-force block |
| 250 | 0x30 | `ipv4_src, ipv4_dst=srv, tcp_dst` | drop | access policy / firewall |
| 100 | 0x10 | full TCP 5-tuple | output(port) | per-connection file-transfer path (idle 30 s) |
| 50 | 0x20 | `ipv4_src, ipv4_dst` | output(port) | other traffic (ping) |
| 1 | – | IPv6 / LLDP | drop | noise suppression |
| 0 | – | * | controller | table-miss |

---

## 2. Application protocol (TCP, port 5000)

Every control message is a frame: `[4-byte big-endian length][JSON object]`. File bytes follow as a raw
stream whose length was announced in the preceding frame. See `common/protocol.py`.

| Request | Reply |
|---|---|
| `{"op":"AUTH","user","password"}`, which must be first | `{"status":"OK","role"}` or `ERR` (the connection is then closed) |
| `{"op":"LIST"}` | `{"status":"OK","files":[{name,size,sha256,owner,version,downloads,uploaded_at}]}` |
| `{"op":"INFO","name"}` | `{"status":"OK","file":{...}}` |
| `{"op":"UPLOAD","name","size","sha256"}` | `READY`, then the client streams `size` bytes, then `OK {sha256}` |
| `{"op":"DOWNLOAD","name","offset"}` | `OK {size,sha256,offset,length}`, then `length` bytes |
| `{"op":"DELETE","name"}` | `OK`. Only the owner or an admin may delete |
| `{"op":"PING"}` / `{"op":"QUIT"}` | `OK` |
| *(server full)* | `{"status":"BUSY","retry_after":1.0}` |

**Concurrency and safety:** one thread per session. A bounded semaphore limits sessions (default 8); extra
clients get `BUSY`. Uploads go to a temp file and are renamed atomically, so readers never see a partial
file. Only one upload of the same name can run at a time. Metadata is guarded by a lock and saved
atomically. File names are validated, so path traversal is rejected. Every socket has a timeout.

**Client robustness:** retries with exponential backoff on BUSY, timeouts and connection resets. A
download **resumes** from the bytes already received and is verified with SHA-256.

---

## 3. Setup (Ubuntu VM)

```bash
git clone <your-repo-url> sdn-file-sharing && cd sdn-file-sharing
bash scripts/setup_ubuntu.sh          # mininet, openvswitch, os-ken, matplotlib, demo users
```

Demo accounts (created by `python3 tools/manage_users.py init-demo`):

| User | Password | Role |
|---|---|---|
| admin | admin123 | admin |
| alice / bob / carol / dave | alice123 / bob123 / carol123 / dave123 | user |
| guest | guest123 | readonly |

> If you prefer Ryu: `pip install setuptools==67.6.1 ryu eventlet==0.30.2`. The code imports Ryu first
> and falls back to os-ken. You can then also use `ryu-manager controller/sdn_controller.py`.

## 4. Running

**Terminal 1: controller** (as a normal user, in the project folder)
```bash
python3 controller/run_controller.py            # dynamic routing + failover (default)
# python3 controller/run_controller.py --static # baseline
```

**Terminal 2: network + file server**
```bash
sudo python3 topology/topo.py
```

**Terminal 3: live bandwidth monitor**
```bash
python3 controller/ctl.py watch
```

### Demo commands (inside the Mininet CLI)

```text
mininet> pingall
mininet> h1 python3 client/client.py --user alice --password alice123 upload README.md
mininet> h2 python3 client/client.py --user bob --password bob123 list
mininet> h2 python3 client/client.py --user bob --password bob123 download README.md
mininet> h1 dd if=/dev/urandom of=/tmp/big.bin bs=1M count=20
mininet> h1 python3 client/client.py --user alice --password alice123 upload /tmp/big.bin
# simultaneous transfers (watch them spread over s2 and s3 in terminal 3)
mininet> h1 python3 client/client.py --user alice --password alice123 download big.bin /tmp/a.bin &
mininet> h2 python3 client/client.py --user bob   --password bob123   download big.bin /tmp/b.bin &
mininet> h3 python3 client/client.py --user carol --password carol123 download big.bin /tmp/c.bin &
mininet> h4 python3 client/client.py --user dave  --password dave123  download big.bin /tmp/d.bin
# link failure while transferring -> controller reroutes
mininet> h1 python3 client/client.py --user alice --password alice123 -v download big.bin /tmp/a.bin &
mininet> link s1 s2 down
mininet> link s1 s2 up
# access policy: h5 is denied (times out); ICMP still works
mininet> h5 ping -c2 srv
mininet> h5 python3 client/client.py --user guest --password guest123 --retries 0 --timeout 3 list
# brute force: the 4th attempt is blocked by the network
mininet> h3 python3 client/client.py --user carol --password x --retries 0 --timeout 3 ping
mininet> h3 python3 client/client.py --user carol --password y --retries 0 --timeout 3 ping
mininet> h3 python3 client/client.py --user carol --password z --retries 0 --timeout 3 ping
mininet> h3 python3 client/client.py --user carol --password carol123 --retries 0 --timeout 3 ping
# interactive shell
mininet> xterm h1        (then: python3 client/client.py --user alice --password alice123 shell)
```

Admin controls from terminal 3:
```bash
python3 controller/ctl.py mode static | dynamic
python3 controller/ctl.py failover off | on
python3 controller/ctl.py block 10.0.0.4 60
python3 controller/ctl.py unblock 10.0.0.3
python3 controller/ctl.py stats
```

Logs: `logs/server.log`, `logs/controller_events.jsonl`, `logs/link_util.csv`, `logs/client_usage.csv`.

### Demo console (web GUI)

One command starts the controller (if it is not already running), the Mininet network and the file
server, and serves a dashboard for the whole demo:

```bash
sudo python3 gui/server.py          # then open http://localhost:8080 in the VM's browser
```

* The left column lists the rubric components (D1/D2). Each has one-click demos that run the real
  clients on the Mininet hosts and show pass/fail checks, charts and the exact commands with their output.
* The right column is a live map fed by the controller: link load, cut links, every connection's path
  (path A orange, path B teal), blocked hosts and controller events. Click a core link to cut or restore it.
* **Run all checks** runs ten functional, SDN and robustness demos in about 1.5 minutes and shows a scorecard.
* The Architecture and Results tabs show the design and the saved results from `results/`.
  Quick performance runs from the console are saved to `results/quick/` and do not overwrite the full results.
* To reach it from the Windows host, add a VirtualBox NAT port forward (host 127.0.0.1:8080 to guest 8080)
  and open http://localhost:8080 there.

Stop it with Ctrl+C; it tears down Mininet and the controller it started. Do not run `topo.py` at the same time.

---

## 5. Testing and performance evaluation

**Unit and socket tests** (no Mininet needed, also run on Windows/macOS):
```bash
python3 -m unittest discover -s tests -v
```
* `test_app.py` (9 tests): auth, upload/download integrity, roles, bad names, resume, metadata persistence,
  6 parallel uploads + 8 parallel downloads, same-name upload race, BUSY + retry, server without controller.
* `test_routing.py` (9 tests): path computation, port numbers, link failure, static vs dynamic selection.
* `test_controller.py` (13 tests): the real controller fed real OpenFlow 1.3 messages from simulated
  switches. Covers proxy ARP, bidirectional path install, rebalancing, policy drop, firewall, brute-force
  block, reroute on link failure, failover off, and that a ping cannot open a path around the access policy.
* `test_metadata.py` (3 tests): a retried upload takes over its own stale attempt, the stale attempt
  cannot overwrite the newer file, and other users are still locked out.

**Mininet experiments** (controller must be running):
```bash
sudo python3 experiments/run_experiments.py          # ~10-15 min; --only e1 e3 to pick experiments
python3 experiments/plot_results.py                  # results/SUMMARY.md + PNG charts
```

| Exp | Scenario | Compared | Metrics |
|---|---|---|---|
| E0 | Functional tests | – | pass/fail |
| E1 | 1, 2, 4, 8 simultaneous 5 MB downloads | **static (baseline) vs dynamic** routing | aggregate Mbps, avg completion time, Jain fairness, path usage |
| E2 | 0 / 1 / 3 % loss on core links, 4 clients | static vs dynamic | aggregate Mbps |
| E3 | s1-s2 link fails 3 s into a 10 MB download | no failure / **failover on** / failover off | completion time, longest stall, reconnects |
| E4 | Denied host, port-22 probe, brute-force login | – | blocked or allowed, attempt number at which the block takes effect |
| E5 | 10 clients, server limited to 3 sessions | client retries 0 vs 8 | success rate, BUSY count |

**Expected behaviour.** With static routing every transfer shares path A, so the aggregate is capped near
10 Mbps. Dynamic routing spreads transfers over both paths, which gives up to about 20 Mbps for 2 or more
clients and shorter completion times. With failover on, a link failure causes a sub-second stall. With
failover off, the client waits for its timeout and resumes on a new connection.

**Measured** (Ubuntu VM, 2 vCPUs, 2 repetitions; absolute rates are limited by the VM's CPU, so compare
the two modes rather than the numbers themselves; full tables in `results/SUMMARY.md`):

| | Static (baseline) | Dynamic |
|---|---|---|
| E1, 2 clients, aggregate | 2.5 Mbps | 12.0 Mbps |
| E1, 4 clients, aggregate / avg completion | 2.1 Mbps / 75 s | 9.9 Mbps / 15 s |
| E1, 8 clients, aggregate / avg completion | 2.1 Mbps / 134 s | 7.1 Mbps / 43 s |
| E2, 4 clients at 1 % / 3 % loss | 1.8 / 1.7 Mbps | 8.8 / 4.8 Mbps |

E3: with failover on the longest stall was 0.9 s with no reconnect; with failover off it was 5.8 s and one
reconnect. E4: all 8 security checks pass, the block takes effect on login attempt 4. E5: 3/10 clients served
without retries, 9/10 with retries (41 BUSY replies).

---

## 6. Project layout

```
common/netconfig.py      topology constants shared by Mininet and the controller
common/protocol.py       framing + streaming helpers
server/file_server.py    concurrent TCP server
server/auth.py           users, PBKDF2 hashing, roles
server/metadata.py       thread-safe metadata + atomic uploads
server/controller_link.py   async event reporting to the controller
client/client.py         CLI / shell client with retry + resume
controller/sdn_controller.py   OpenFlow 1.3 app (routing, policy, monitoring, failover)
controller/routing.py    path computation + load-aware selection (pure Python)
controller/run_controller.py   launcher (Ryu or os-ken)
controller/ctl.py        admin CLI + live monitor
config/policy.json       access policy and thresholds
topology/topo.py         Mininet topology
experiments/             automated experiments + plotting
gui/server.py            web demo console (runs the network, serves gui/static/)
tests/                   unit / socket / controller tests
tools/manage_users.py    account management
```

## 7. Troubleshooting

* `Exception: Error creating interface pair (root-eth0...)`: a previous run did not clean up. Run `sudo mn -c`.
* `pingall` fails: check that the controller is running **before** `topo.py` and that terminal 1 shows
  `Switch s1..s4 connected`.
* Port 9090 or 6653 is already in use: stop the old controller (`pkill -f run_controller`).
* `os-ken` will not install on Ubuntu 24.04: use `pip3 install --break-system-packages os-ken` or a venv.
