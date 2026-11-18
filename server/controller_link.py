"""
Non-blocking notifier: the file server reports application events
(auth_ok / auth_fail / transfer_start / transfer_end) to the SDN controller
over a persistent TCP connection (JSON lines). If the controller is down the
file server keeps working; events are dropped and reconnection is retried.
"""
import json
import logging
import queue
import socket
import threading
import time

log = logging.getLogger("notifier")


class ControllerNotifier:
    def __init__(self, host, port, enabled=True):
        self.addr = (host, port)
        self.enabled = enabled
        self.q = queue.Queue(maxsize=2000)
        self.sock = None
        self.sent = 0
        if enabled:
            threading.Thread(target=self._run, daemon=True, name="notifier").start()

    def event(self, kind, **fields):
        if not self.enabled:
            return
        msg = {"cmd": "event", "type": kind, "ts": time.time()}
        msg.update(fields)
        try:
            self.q.put_nowait(msg)
        except queue.Full:
            pass

    def _connect(self):
        try:
            self.sock = socket.create_connection(self.addr, timeout=2)
            log.info("connected to SDN controller at %s:%d", *self.addr)
        except OSError as e:
            self.sock = None
            log.debug("controller unreachable: %s", e)

    def _run(self):
        warned = False
        while True:
            msg = self.q.get()
            for _attempt in range(2):
                if self.sock is None:
                    self._connect()
                if self.sock is None:
                    if not warned:
                        log.warning("SDN controller not reachable at %s:%d - events dropped", *self.addr)
                        warned = True
                    time.sleep(1)
                    break
                try:
                    self.sock.sendall((json.dumps(msg) + "\n").encode())
                    buf = b""
                    while b"\n" not in buf:
                        chunk = self.sock.recv(4096)
                        if not chunk:
                            raise ConnectionError("closed")
                        buf += chunk
                    self.sent += 1
                    warned = False
                    break
                except OSError:
                    try:
                        self.sock.close()
                    except OSError:
                        pass
                    self.sock = None
