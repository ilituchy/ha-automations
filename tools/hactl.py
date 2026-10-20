#!/usr/bin/env python3
"""Minimal Home Assistant REST + WebSocket client. Stdlib only.

A dev tool, not part of the shipped app. It exists because verifying the
approval round-trip needs a WebSocket, and no machine in this project has
``websockets``, ``aiohttp`` or ``websocket-client`` installed. Writing ~200
lines of RFC 6455 against the stdlib is cheaper than making the verification
story depend on whatever happens to be pip-installed on a given box.

The token is read from a file and never printed. Nothing here takes a token
on the command line, because argv is visible in ``ps``.

Usage:
    tools/hactl.py ping
    tools/hactl.py states [substring]
    tools/hactl.py call notify mobile_app_iphone '{"message": "hi"}'
    tools/hactl.py ws '{"type": "auth/current_user"}'
    tools/hactl.py listen mobile_app_notification_action [seconds]

Environment:
    HA_URL        instance base URL; falls back to ~/.ha_url, then
                  http://homeassistant.local:8123
    HA_TOKEN_FILE default ~/.ha_token
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, Optional, Tuple

#: Not the author's instance address. A LAN IP in a public repository is not a
#: secret, but it is useless to everyone else and it is a detail about someone's
#: network, so the real one lives in an untracked ``~/.ha_url``. mDNS is the only
#: address that is correct by default, even though it is slower to resolve and
#: wrong for any instance not on port 8123.
DEFAULT_URL = "http://homeassistant.local:8123"


def _token() -> str:
    path = os.path.expanduser(os.environ.get("HA_TOKEN_FILE", "~/.ha_token"))
    with open(path, "r") as fh:
        tok = fh.read().strip()
    if not tok:
        raise SystemExit("token file %s is empty" % path)
    return tok


def _base() -> str:
    url = os.environ.get("HA_URL")
    if not url:
        try:
            with open(os.path.expanduser("~/.ha_url"), "r") as fh:
                url = fh.read().strip()
        except OSError:
            url = ""
    return (url or DEFAULT_URL).rstrip("/")


# ---------------------------------------------------------------- REST ----


def rest(path: str, payload: Optional[Any] = None, timeout: float = 15.0) -> Any:
    url = _base() + path
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": "Bearer " + _token(),
            "Content-Type": "application/json",
        },
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400]
        raise SystemExit("HTTP %s on %s: %s" % (exc.code, path, body))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


# ----------------------------------------------------------- WebSocket ----
#
# RFC 6455, client side, only what HA needs: text frames, client-masked out,
# server-unmasked in, ping/pong, close. No extensions, no fragmentation on
# send (HA messages are small), but fragmented *receives* are reassembled
# because a large /api/websocket state dump does arrive in continuations.


class WsError(RuntimeError):
    pass


class Ws:
    def __init__(self, url: str, timeout: float = 30.0) -> None:
        secure = url.startswith("https://") or url.startswith("wss://")
        hostport = url.split("://", 1)[1].split("/", 1)[0]
        if ":" in hostport:
            host, port_s = hostport.rsplit(":", 1)
            port = int(port_s)
        else:
            host, port = hostport, (443 if secure else 80)

        self.sock = socket.create_connection((host, port), timeout=timeout)
        if secure:
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(self.sock, server_hostname=host)
        self.sock.settimeout(timeout)
        self._buf = b""
        self._handshake(host, port, secure)

    def _handshake(self, host: str, port: int, secure: bool) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        hostheader = host if port in (80, 443) else "%s:%d" % (host, port)
        req = (
            "GET /api/websocket HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n" % (hostheader, key)
        )
        self.sock.sendall(req.encode())

        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WsError("connection closed during handshake")
            self._buf += chunk
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode()
        if "101" not in status:
            raise WsError("handshake failed: %s" % status)

        expect = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        lowered = head.decode("latin-1").lower()
        if expect.lower() not in lowered:
            raise WsError("Sec-WebSocket-Accept mismatch")

    # -- framing --

    def _recv_exactly(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WsError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self) -> Tuple[int, bytes]:
        b0, b1 = self._recv_exactly(2)
        opcode = b0 & 0x0F
        fin = bool(b0 & 0x80)
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exactly(8))[0]
        mask = self._recv_exactly(4) if masked else b""
        payload = self._recv_exactly(length)
        if masked:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        if not fin or opcode == 0:
            # Reassemble continuations. HA's initial state dumps need this.
            frames = [payload]
            first_op = opcode
            while not fin:
                b0b, b1b = self._recv_exactly(2)
                fin = bool(b0b & 0x80)
                nxt_len = b1b & 0x7F
                if nxt_len == 126:
                    nxt_len = struct.unpack("!H", self._recv_exactly(2))[0]
                elif nxt_len == 127:
                    nxt_len = struct.unpack("!Q", self._recv_exactly(8))[0]
                frames.append(self._recv_exactly(nxt_len))
            return first_op, b"".join(frames)
        return opcode, payload

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_json(self, obj: Dict[str, Any]) -> None:
        self._send_frame(0x1, json.dumps(obj).encode())

    def recv_json(self) -> Dict[str, Any]:
        while True:
            opcode, payload = self._read_frame()
            if opcode == 0x9:  # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                raise WsError("server closed the connection")
            if opcode == 0x1:
                return json.loads(payload.decode())
            # binary frames are not part of the HA protocol; ignore

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass
        try:
            self.sock.close()
        except Exception:  # noqa: BLE001
            pass


class HaWs:
    """Authenticated HA WebSocket session with the id counter handled."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.ws = Ws(_base(), timeout=timeout)
        self._id = 0
        hello = self.ws.recv_json()
        if hello.get("type") != "auth_required":
            raise WsError("unexpected greeting: %r" % hello)
        self.ws.send_json({"type": "auth", "access_token": _token()})
        ack = self.ws.recv_json()
        if ack.get("type") != "auth_ok":
            raise WsError("auth failed: %r" % ack)
        self.ha_version = ack.get("ha_version")

    def command(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        self._id += 1
        msg = dict(msg, id=self._id)
        self.ws.send_json(msg)
        while True:
            got = self.ws.recv_json()
            if got.get("id") == self._id and got.get("type") == "result":
                return got

    def subscribe(self, event_type: str) -> int:
        res = self.command({"type": "subscribe_events", "event_type": event_type})
        if not res.get("success"):
            raise WsError("subscribe failed: %r" % res.get("error"))
        return self._id

    def events(self, deadline: float) -> Iterator[Dict[str, Any]]:
        while time.time() < deadline:
            remaining = max(0.5, deadline - time.time())
            self.ws.sock.settimeout(remaining)
            try:
                msg = self.ws.recv_json()
            except socket.timeout:
                return
            if msg.get("type") == "event":
                yield msg["event"]

    def close(self) -> None:
        self.ws.close()


# ----------------------------------------------------------------- CLI ----


def _cmd_ping() -> int:
    print(rest("/api/"))
    cfg = rest("/api/config")
    print("%s  %s  tz=%s  state=%s" % (
        cfg.get("location_name"), cfg.get("version"),
        cfg.get("time_zone"), cfg.get("state")))
    ws = HaWs()
    me = ws.command({"type": "auth/current_user"})
    r = me.get("result", {})
    print("user: %s  id=%s  admin=%s  owner=%s" % (
        r.get("name"), r.get("id"), r.get("is_admin"), r.get("is_owner")))
    ws.close()
    return 0


def _cmd_states(needle: str = "") -> int:
    for e in sorted(rest("/api/states"), key=lambda x: x["entity_id"]):
        if needle.lower() in e["entity_id"].lower():
            print("%-48s %s" % (e["entity_id"], str(e["state"])[:40]))
    return 0


def _cmd_call(domain: str, service: str, payload: str = "{}") -> int:
    out = rest("/api/services/%s/%s" % (domain, service), json.loads(payload))
    print(json.dumps(out, indent=2)[:2000] if out else "ok")
    return 0


def _cmd_ws(raw: str) -> int:
    ws = HaWs()
    print(json.dumps(ws.command(json.loads(raw)), indent=2)[:8000])
    ws.close()
    return 0


def _cmd_listen(event_type: str, seconds: str = "120") -> int:
    ws = HaWs()
    ws.subscribe(event_type)
    print("listening for %s for %ss (ha %s)" % (event_type, seconds, ws.ha_version))
    sys.stdout.flush()
    for ev in ws.events(time.time() + float(seconds)):
        print(json.dumps(ev, indent=2))
        sys.stdout.flush()
    ws.close()
    return 0


def main(argv: list) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmds = {
        "ping": _cmd_ping,
        "states": _cmd_states,
        "call": _cmd_call,
        "ws": _cmd_ws,
        "listen": _cmd_listen,
    }
    fn = cmds.get(argv[0])
    if fn is None:
        print("unknown command %r" % argv[0])
        return 2
    return fn(*argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
