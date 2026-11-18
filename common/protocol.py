"""
Application-level protocol over raw TCP sockets.

Every control message is a frame:   [4-byte big-endian length][UTF-8 JSON]
File contents follow a control message as a raw byte stream whose exact
length was announced in the preceding JSON ("size" / "length").

Client -> server requests                Server replies
  {"op":"AUTH","user","password"}          {"status":"OK","role"} | {"status":"ERR",...}
  {"op":"LIST"}                            {"status":"OK","files":[...]}
  {"op":"INFO","name"}                     {"status":"OK","file":{...}}
  {"op":"UPLOAD","name","size","sha256"}   {"status":"READY"} <bytes> {"status":"OK",...}
  {"op":"DOWNLOAD","name","offset"}        {"status":"OK","size","sha256","length"} <bytes>
  {"op":"DELETE","name"}                   {"status":"OK"}
  {"op":"PING"}                            {"status":"OK","pong":true}
  {"op":"QUIT"}                            {"status":"OK"}
On overload the server answers the first frame with {"status":"BUSY","retry_after":s}.
"""
import json
import struct

HEADER = struct.Struct("!I")
MAX_FRAME = 64 * 1024
CHUNK = 64 * 1024


class ProtocolError(Exception):
    pass


def send_msg(sock, obj):
    data = json.dumps(obj).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise ProtocolError("frame too large")
    sock.sendall(HEADER.pack(len(data)) + data)


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), CHUNK))
        if not chunk:
            raise ConnectionError("connection closed by peer")
        buf.extend(chunk)
    return bytes(buf)


def recv_msg(sock):
    (n,) = HEADER.unpack(recv_exact(sock, HEADER.size))
    if n > MAX_FRAME:
        raise ProtocolError("frame of %d bytes exceeds limit" % n)
    try:
        obj = json.loads(recv_exact(sock, n).decode("utf-8"))
    except ValueError as e:
        raise ProtocolError("bad JSON: %s" % e)
    if not isinstance(obj, dict):
        raise ProtocolError("frame must be a JSON object")
    return obj


def stream_out(sock, fobj, length, on_chunk=None):
    """Send exactly `length` bytes from fobj."""
    sent = 0
    while sent < length:
        data = fobj.read(min(CHUNK, length - sent))
        if not data:
            raise IOError("file shorter than announced")
        sock.sendall(data)
        sent += len(data)
        if on_chunk:
            on_chunk(len(data))
    return sent


def stream_in(sock, fobj, length, hasher=None, on_chunk=None):
    """Receive exactly `length` bytes into fobj."""
    got = 0
    while got < length:
        data = sock.recv(min(CHUNK, length - got))
        if not data:
            raise ConnectionError("connection closed mid-transfer (%d/%d bytes)" % (got, length))
        fobj.write(data)
        if hasher:
            hasher.update(data)
        got += len(data)
        if on_chunk:
            on_chunk(len(data))
    return got
