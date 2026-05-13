#!/usr/bin/env python3
"""WebSocket market-data feed for zkCEX.

Standalone, stdlib-only WebSocket server (RFC 6455) that polls the existing
Binance-compatible REST endpoints in the background and pushes diffs to
subscribed clients. The trading UI uses this instead of REST polling so the
order book / trades / ticker / klines update with sub-second latency and the
backend isn't hammered by N tabs each polling 5 endpoints every 2-5s.

Wire protocol (Binance-compatible, since the rest of the app speaks that
idiom):

    --> {"method":"SUBSCRIBE","params":["depth@ETHUSDT","trade@ETHUSDT"],"id":1}
    <-- {"id":1,"result":null}
    <-- {"stream":"depth@ETHUSDT","data":{...}}
    <-- {"stream":"trade@ETHUSDT","data":{...}}

    --> {"method":"UNSUBSCRIBE","params":["trade@ETHUSDT"],"id":2}
    <-- {"id":2,"result":null}

    --> {"method":"LIST_SUBSCRIPTIONS","id":3}
    <-- {"id":3,"result":["depth@ETHUSDT"]}

The server also accepts plain HTTP GET /health on the same port (sniff the
first byte: 'G' = HTTP, otherwise expect a WebSocket Upgrade handshake).

Backpressure: each client has a bounded send buffer. Writes are non-blocking
with a 200ms budget; if a client falls behind we drop the slowest non-snapshot
frames first (we always keep depth + ticker, the two snapshot streams). After
30s of sustained backpressure the connection is closed.

Caps: 200 concurrent connections, 50 streams per connection.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import select
import socket
import socketserver
import struct
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from typing import Any

# Ensure sibling otel package is importable regardless of cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
os.environ.setdefault("OTEL_SERVICE_NAME", "zkcex-wsfeed")
try:
    from otel.shim import install as _otel_install  # noqa: E402
except Exception:  # noqa: BLE001

    def _otel_install():
        pass

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _validated_http_base_url(name: str, raw_url: str) -> str:
    return _validated_http_url(raw_url, name=name).rstrip("/")


def _http_request(url: str, **kwargs) -> urllib.request.Request:
    return urllib.request.Request(_validated_http_url(url), **kwargs)  # noqa: S310


def _http_urlopen(target, **kwargs):
    if isinstance(target, str):
        target = _validated_http_url(target)
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310


def log(*args: object) -> None:
    sys.stderr.write("[ws_feed] " + " ".join(str(a) for a in args) + "\n")
    sys.stderr.flush()


REST_BASE = _validated_http_base_url(
    "ZKCEX_REST_BASE", os.environ.get("ZKCEX_REST_BASE", "http://127.0.0.1:8094")
)
LISTEN_HOST = os.environ.get("WS_FEED_HOST", "127.0.0.1")
DEFAULT_PORT = 5510

EXCHANGE_INFO_REFRESH = 300.0  # 5 min
DEPTH_INTERVAL = 1.0  # per active symbol
TRADES_INTERVAL = 1.0  # per active symbol
TICKER_INTERVAL = 2.0  # per active symbol
KLINES_INTERVAL = 3.0  # per (symbol, interval)

PING_INTERVAL = 25.0
PONG_TIMEOUT = 60.0
MAX_CONNECTIONS = 200
MAX_STREAMS_PER_CONN = 50

# Backpressure
SEND_BUDGET_MS = 200
BACKPRESSURE_KILL_S = 30.0
SEND_QUEUE_HARD_LIMIT = 4096  # frames

# WebSocket opcodes
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ---------------------------------------------------------------------------
# WebSocket framing helpers
# ---------------------------------------------------------------------------


def encode_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    """Server -> client frame (no masking, RFC 6455)."""
    b1 = (0x80 if fin else 0x00) | (opcode & 0x0F)
    n = len(payload)
    if n < 126:
        header = bytes([b1, n])
    elif n < 65536:
        header = bytes([b1, 126]) + struct.pack(">H", n)
    else:
        header = bytes([b1, 127]) + struct.pack(">Q", n)
    return header + payload


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Block until exactly n bytes are read, or raise ConnectionError."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("eof during recv_exact")
        buf.extend(chunk)
    return bytes(buf)


def recv_frame(sock: socket.socket) -> tuple[int, bool, bytes]:
    """Decode one client -> server frame. Returns (opcode, fin, payload).

    Validates masking bit (clients MUST mask), unmasks payload, supports
    extended payload lengths (126 / 127). Raises ConnectionError on EOF.
    """
    head = recv_exact(sock, 2)
    b1, b2 = head[0], head[1]
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", recv_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", recv_exact(sock, 8))[0]
    if not masked:
        # RFC 6455: server MUST close on unmasked client frames.
        raise ConnectionError("client frame not masked")
    mask = recv_exact(sock, 4)
    payload = bytearray(recv_exact(sock, n) if n else b"")
    for i in range(len(payload)):
        payload[i] ^= mask[i % 4]
    return opcode, fin, bytes(payload)


def make_handshake_response(headers: dict[str, str]) -> bytes | None:
    """Build the 101 Switching Protocols response. Returns None if invalid."""
    if headers.get("upgrade", "").lower() != "websocket":
        return None
    if "upgrade" not in headers.get("connection", "").lower():
        return None
    key = headers.get("sec-websocket-key")
    if not key:
        return None
    accept = base64.b64encode(
        hashlib.sha1((key + GUID).encode("ascii")).digest()  # noqa: S324
    ).decode("ascii")
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {accept}",
        "",
        "",
    ]
    return "\r\n".join(lines).encode("ascii")


# ---------------------------------------------------------------------------
# REST poller (background hub)
# ---------------------------------------------------------------------------


def _http_get_json(path: str, timeout: float = 4.0) -> Any:
    if not path.startswith("/"):
        raise ValueError("REST path must be absolute")
    url = REST_BASE + path
    req = _http_request(url, headers={"Accept": "application/json"})
    with _http_urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class FeedHub:
    """Owns the REST polling loops and fan-out to subscribers.

    Threading model:
      * One background thread per stream-kind (depth/trades/ticker/klines)
        that walks the active subscription set and refreshes per-symbol state.
      * One thread for periodic exchangeInfo refresh.
      * Each connection runs in its own thread (created by the TCP server)
        and registers/unregisters streams via add_subscriber / remove_subscriber.

    Locking: a single recursive lock guards the subscription / state maps.
    Per-client send queues use their own lock (see Client below).
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        # stream_name -> set[Client]
        self.subs: dict[str, set] = {}
        # cached state per stream_name -> last pushed payload (or last raw poll)
        self.depth_state: dict[str, dict] = {}
        self.last_trade_id: dict[str, int] = {}
        self.last_ticker: dict[str, dict] = {}
        # (symbol, interval) -> last bar (open_time, close, etc.)
        self.last_kline: dict[tuple[str, str], dict] = {}
        # symbol metadata from exchangeInfo
        self.exchange_info: dict = {}
        # health metrics
        self.last_poll_at: dict[str, float] = {}
        self.dropped_frames = 0
        self.start_time = time.time()
        # clients registry (for stats / shutdown)
        self.clients: set = set()
        self.stop_event = threading.Event()

    # ---------- registration ----------
    def register_client(self, client: Client) -> None:
        with self.lock:
            self.clients.add(client)

    def unregister_client(self, client: Client) -> None:
        with self.lock:
            self.clients.discard(client)
            # remove from any subscription set
            empty: list[str] = []
            for name, s in self.subs.items():
                s.discard(client)
                if not s:
                    empty.append(name)
            for name in empty:
                del self.subs[name]

    def add_subscriber(self, client: Client, stream: str) -> None:
        with self.lock:
            self.subs.setdefault(stream, set()).add(client)

    def remove_subscriber(self, client: Client, stream: str) -> None:
        with self.lock:
            s = self.subs.get(stream)
            if s and client in s:
                s.discard(client)
                if not s:
                    del self.subs[stream]

    # ---------- helpers ----------
    @staticmethod
    def parse_stream(name: str) -> tuple[str, str, str | None]:
        """Return (kind, symbol, extra). Stream names look like:
        depth@ETHUSDT
        trade@ETHUSDT
        ticker@ETHUSDT
        kline@ETHUSDT_1m
        """
        if "@" not in name:
            return ("", "", None)
        kind, _, rest = name.partition("@")
        if kind == "kline":
            sym, _, interval = rest.partition("_")
            return ("kline", sym.upper(), interval or None)
        return (kind, rest.upper(), None)

    def active_symbols_for(self, kind: str) -> list[str]:
        with self.lock:
            out: set[str] = set()
            for name in self.subs.keys():
                k, sym, _ = self.parse_stream(name)
                if k == kind and sym:
                    out.add(sym)
            return sorted(out)

    def active_klines(self) -> list[tuple[str, str]]:
        with self.lock:
            out: set[tuple[str, str]] = set()
            for name in self.subs.keys():
                k, sym, ivl = self.parse_stream(name)
                if k == "kline" and sym and ivl:
                    out.add((sym, ivl))
            return sorted(out)

    def fanout(self, stream: str, data: Any) -> None:
        msg = json.dumps({"stream": stream, "data": data}, separators=(",", ":"))
        payload = msg.encode("utf-8")
        with self.lock:
            targets = list(self.subs.get(stream, ()))
        for c in targets:
            c.enqueue_text(payload, stream=stream)

    # ---------- pollers ----------
    def poll_exchange_info(self) -> None:
        try:
            info = _http_get_json("/v3/exchangeInfo", timeout=6.0)
            self.exchange_info = info or {}
            self.last_poll_at["exchangeInfo"] = time.time()
        except Exception as e:  # noqa: BLE001
            log("exchangeInfo poll skipped:", e)

    def poll_depth_once(self, symbol: str) -> None:
        try:
            d = _http_get_json(f"/v3/depth?symbol={symbol}&limit=20", timeout=4.0)
        except Exception:
            return
        # Compare against last cached snapshot. We push only when something
        # changed (lastUpdateId differs, or any row added/removed/changed).
        bids = [[str(b[0]), str(b[1])] for b in (d.get("bids") or [])][:20]
        asks = [[str(a[0]), str(a[1])] for a in (d.get("asks") or [])][:20]
        snap = {
            "lastUpdateId": int(d.get("lastUpdateId") or 0),
            "bids": bids,
            "asks": asks,
        }
        prev = self.depth_state.get(symbol)
        if (
            prev
            and prev["lastUpdateId"] == snap["lastUpdateId"]
            and prev["bids"] == snap["bids"]
            and prev["asks"] == snap["asks"]
        ):
            self.last_poll_at[f"depth@{symbol}"] = time.time()
            return
        self.depth_state[symbol] = snap
        self.last_poll_at[f"depth@{symbol}"] = time.time()
        self.fanout(f"depth@{symbol}", snap)

    def poll_trades_once(self, symbol: str) -> None:
        try:
            arr = _http_get_json(f"/v3/trades?symbol={symbol}&limit=50", timeout=4.0)
        except Exception:
            return
        if not isinstance(arr, list):
            return
        self.last_poll_at[f"trade@{symbol}"] = time.time()
        # On first poll for a symbol we don't push the entire historical
        # backlog — that would flood every new client subscriber. Instead we
        # baseline `last_trade_id` to whatever the latest id is and start
        # streaming from the next trade onward. The UI's REST snapshot
        # already populated the recent-trades list.
        first_poll = symbol not in self.last_trade_id
        max_id = max((int(t.get("id") or 0) for t in arr), default=0)
        if first_poll:
            self.last_trade_id[symbol] = max_id
            return
        last_id = self.last_trade_id[symbol]
        new_rows = [t for t in arr if int(t.get("id") or 0) > last_id]
        if not new_rows:
            return
        # Chronological order so subscribers see trades in the order they
        # actually executed.
        new_rows.sort(key=lambda t: int(t.get("id") or 0))
        for t in new_rows:
            tid = int(t.get("id") or 0)
            data = {
                "e": "trade",
                "s": symbol,
                "t": tid,
                "p": str(t.get("price")),
                "q": str(t.get("qty")),
                "T": int(t.get("time") or 0),
                "m": bool(t.get("isBuyerMaker")),
            }
            self.fanout(f"trade@{symbol}", data)
            if tid > self.last_trade_id.get(symbol, 0):
                self.last_trade_id[symbol] = tid

    def poll_ticker_once(self, symbol: str) -> None:
        try:
            r = _http_get_json(f"/v3/ticker/24h?symbol={symbol}", timeout=4.0)
        except Exception:
            return
        t = r[0] if isinstance(r, list) and r else r
        if not isinstance(t, dict):
            return
        self.last_poll_at[f"ticker@{symbol}"] = time.time()
        snap = {
            "e": "24hrTicker",
            "s": symbol,
            "c": str(t.get("lastPrice")),
            "P": str(t.get("priceChangePercent")),
            "h": str(t.get("highPrice")),
            "l": str(t.get("lowPrice")),
            "v": str(t.get("volume")),
            "q": str(t.get("quoteVolume", "")),
        }
        prev = self.last_ticker.get(symbol)
        if prev == snap:
            return
        self.last_ticker[symbol] = snap
        self.fanout(f"ticker@{symbol}", snap)

    def poll_klines_once(self, symbol: str, interval: str) -> None:
        try:
            arr = _http_get_json(
                f"/v3/klines?symbol={symbol}&interval={interval}&limit=2",
                timeout=4.0,
            )
        except Exception:
            return
        if not isinstance(arr, list) or not arr:
            return
        key = (symbol, interval)
        self.last_poll_at[f"kline@{symbol}_{interval}"] = time.time()
        prev = self.last_kline.get(key)
        # Two bars: [previous_closed?, current]. Push whichever changed.
        # Heuristic: if the latest open_time advanced, the previous bar just
        # closed (x=true), and a new one is open (x=false).
        last_bar = arr[-1]
        try:
            t_open = int(last_bar[0])
            t_close = int(last_bar[6])
            o = str(last_bar[1])
            h = str(last_bar[2])
            low = str(last_bar[3])
            c = str(last_bar[4])
            v = str(last_bar[5])
            n = int(last_bar[8])
        except (IndexError, ValueError, TypeError):
            return

        is_closed = False
        # If we have a previous bar and the open_time changed, mark prev closed.
        # Push it once with x=true.
        if prev and prev["t"] != t_open:
            prev_closed = dict(prev)
            prev_closed["x"] = True
            self.fanout(
                f"kline@{symbol}_{interval}",
                {"e": "kline", "s": symbol, "k": prev_closed},
            )

        cur = {
            "t": t_open,
            "T": t_close,
            "i": interval,
            "o": o,
            "c": c,
            "h": h,
            "l": low,
            "v": v,
            "n": n,
            "x": is_closed,
        }

        # Skip empty bars (price 0)
        try:
            if float(o) <= 0 and float(c) <= 0:
                # Don't update prev — wait for real data.
                return
        except ValueError:
            return

        if prev != cur:
            self.last_kline[key] = cur
            self.fanout(
                f"kline@{symbol}_{interval}",
                {"e": "kline", "s": symbol, "k": cur},
            )

    # ---------- background loops ----------
    def run_pollers(self) -> None:
        threading.Thread(target=self._loop_exchange_info, daemon=True).start()
        threading.Thread(
            target=self._loop_kind, args=("depth", DEPTH_INTERVAL), daemon=True
        ).start()
        threading.Thread(
            target=self._loop_kind, args=("trade", TRADES_INTERVAL), daemon=True
        ).start()
        threading.Thread(
            target=self._loop_kind, args=("ticker", TICKER_INTERVAL), daemon=True
        ).start()
        threading.Thread(target=self._loop_klines, daemon=True).start()
        threading.Thread(target=self._loop_keepalive, daemon=True).start()

    def _loop_exchange_info(self) -> None:
        # Initial fetch happens at startup (synchronously) too.
        while not self.stop_event.is_set():
            self.poll_exchange_info()
            self.stop_event.wait(EXCHANGE_INFO_REFRESH)

    def _loop_kind(self, kind: str, interval: float) -> None:
        per_symbol = {
            "depth": self.poll_depth_once,
            "trade": self.poll_trades_once,
            "ticker": self.poll_ticker_once,
        }[kind]
        while not self.stop_event.is_set():
            t0 = time.time()
            for sym in self.active_symbols_for(kind):
                try:
                    per_symbol(sym)
                except Exception:
                    sys.stderr.write(f"[ws_feed] {kind} poll failed for {sym}\n")
                    sys.stderr.write(traceback.format_exc())
            elapsed = time.time() - t0
            self.stop_event.wait(max(0.05, interval - elapsed))

    def _loop_klines(self) -> None:
        while not self.stop_event.is_set():
            t0 = time.time()
            for sym, ivl in self.active_klines():
                try:
                    self.poll_klines_once(sym, ivl)
                except Exception:
                    sys.stderr.write(f"[ws_feed] kline poll failed for {sym} {ivl}\n")
                    sys.stderr.write(traceback.format_exc())
            elapsed = time.time() - t0
            self.stop_event.wait(max(0.05, KLINES_INTERVAL - elapsed))

    def _loop_keepalive(self) -> None:
        """Walk all clients, send a WS ping if it's been > PING_INTERVAL since
        the last bytes were sent, and close any that haven't sent a pong in
        PONG_TIMEOUT.
        """
        while not self.stop_event.is_set():
            now = time.time()
            with self.lock:
                clients = list(self.clients)
            for c in clients:
                try:
                    if not c.alive:
                        continue
                    if now - c.last_pong_at > PONG_TIMEOUT and c.pinged_at > c.last_pong_at:
                        c.shutdown("pong timeout")
                        continue
                    if now - c.last_send_at > PING_INTERVAL:
                        c.send_ping()
                except Exception as e:  # noqa: BLE001
                    log("client keepalive skipped:", e)
            self.stop_event.wait(5.0)


# ---------------------------------------------------------------------------
# Per-connection client wrapper (write side fanout, backpressure)
# ---------------------------------------------------------------------------


class Client:
    """Wraps one accepted connection. Owns the write side: a thread-local send
    queue + a writer thread. The reader runs on the request handler thread.
    """

    def __init__(self, sock: socket.socket, hub: FeedHub, addr) -> None:
        self.sock = sock
        self.hub = hub
        self.addr = addr
        self.streams: set[str] = set()
        self.alive = True
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        # Each entry is (priority, frame_bytes, stream_or_None)
        # priority: 0=critical (control/handshake reply), 1=snapshot (depth/ticker),
        # 2=trade, 3=kline. Lower = keep first when shedding.
        self.queue: deque = deque()
        self.queue_bytes = 0
        self.last_send_at = time.time()
        self.last_pong_at = time.time()
        self.pinged_at = 0.0
        self.backpressure_since: float | None = None
        self.writer_thread: threading.Thread | None = None

    # ---------- enqueue helpers ----------
    @staticmethod
    def _priority_for_stream(stream: str | None) -> int:
        if stream is None:
            return 0
        if stream.startswith("depth@") or stream.startswith("ticker@"):
            return 1
        if stream.startswith("trade@"):
            return 2
        if stream.startswith("kline@"):
            return 3
        return 2

    def enqueue_text(self, payload: bytes, stream: str | None = None) -> None:
        frame = encode_frame(OP_TEXT, payload, fin=True)
        prio = self._priority_for_stream(stream)
        with self.cond:
            if not self.alive:
                return
            self.queue.append((prio, frame, stream))
            self.queue_bytes += len(frame)
            # Hard limit: shed lowest-prio (highest number) frames first, but
            # never drop snapshot streams (priority <= 1).
            while len(self.queue) > SEND_QUEUE_HARD_LIMIT:
                idx = self._find_droppable_index()
                if idx is None:
                    break
                _p, dropped, _s = self.queue[idx]
                del self.queue[idx]
                self.queue_bytes -= len(dropped)
                self.hub.dropped_frames += 1
            self.cond.notify_all()

    def enqueue_control(self, frame: bytes) -> None:
        with self.cond:
            if not self.alive:
                return
            # Control frames: priority 0, push to the front so they go out fast.
            self.queue.appendleft((0, frame, None))
            self.queue_bytes += len(frame)
            self.cond.notify_all()

    def _find_droppable_index(self) -> int | None:
        # Scan from the back (oldest in time we shed *latest* low-prio frames
        # since the snapshot ones are the freshest). We want to drop the
        # lowest-priority entries. Iterate back-to-front, return first index
        # whose priority > 1.
        for i in range(len(self.queue) - 1, -1, -1):
            if self.queue[i][0] > 1:
                return i
        return None

    # ---------- writer ----------
    def start_writer(self) -> None:
        self.writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self.writer_thread.start()

    def _writer_loop(self) -> None:
        try:
            self.sock.settimeout(None)
            while True:
                with self.cond:
                    while self.alive and not self.queue:
                        self.cond.wait(timeout=10.0)
                        if not self.alive:
                            return
                    if not self.alive:
                        return
                    _prio, frame, stream = self.queue.popleft()
                    self.queue_bytes -= len(frame)
                # Send with non-blocking budget.
                ok = self._send_with_budget(frame)
                if not ok:
                    self.shutdown("send failed")
                    return
                self.last_send_at = time.time()
        except Exception:
            self.shutdown("writer crashed")

    def _send_with_budget(self, frame: bytes) -> bool:
        """Try to send the whole frame within SEND_BUDGET_MS. If we exceed it
        we mark the client as backpressured. After BACKPRESSURE_KILL_S of
        sustained pressure, return False so the caller closes us.
        """
        deadline = time.time() + (SEND_BUDGET_MS / 1000.0)
        view = memoryview(frame)
        sent = 0
        try:
            self.sock.setblocking(False)
            while sent < len(view):
                try:
                    n = self.sock.send(view[sent:])
                    if n == 0:
                        return False
                    sent += n
                except BlockingIOError:
                    if time.time() > deadline:
                        # Backpressure detected. Mark and try to wait briefly
                        # before giving up entirely.
                        if self.backpressure_since is None:
                            self.backpressure_since = time.time()
                        elif time.time() - self.backpressure_since > BACKPRESSURE_KILL_S:
                            return False
                        # Block-wait a tiny slice with select before retrying.
                        ready = select.select([], [self.sock], [], 0.05)
                        if not ready[1]:
                            # Still not writable. Give up on this frame to keep
                            # the queue moving; caller proceeds.
                            return True
                        deadline = time.time() + (SEND_BUDGET_MS / 1000.0)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return False
            self.backpressure_since = None
            return True
        finally:
            try:
                self.sock.setblocking(True)
            except OSError:
                pass

    # ---------- control ----------
    def send_ping(self) -> None:
        token = secrets.token_bytes(4)
        self.pinged_at = time.time()
        self.enqueue_control(encode_frame(OP_PING, token))

    def send_pong(self, payload: bytes) -> None:
        self.enqueue_control(encode_frame(OP_PONG, payload))

    def send_close(self, code: int = 1000, reason: str = "") -> None:
        body = struct.pack(">H", code) + reason.encode("utf-8")[:120]
        self.enqueue_control(encode_frame(OP_CLOSE, body))

    def shutdown(self, why: str = "") -> None:
        with self.cond:
            if not self.alive:
                return
            self.alive = False
            self.cond.notify_all()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Connection handler (HTTP sniff + WebSocket protocol)
# ---------------------------------------------------------------------------

HUB = FeedHub()


def _read_http_request_line_and_headers(
    sock: socket.socket, peek_byte: bytes
) -> tuple[str, dict[str, str], bytes]:
    """Read the rest of an HTTP request after we've already peeked the first
    byte. Returns (request_line, headers_lower_keyed, leftover_bytes).
    """
    buf = bytearray(peek_byte)
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(2048)
        if not chunk:
            raise ConnectionError("eof during http header")
        buf.extend(chunk)
        if len(buf) > 16384:
            raise ConnectionError("http headers too large")
    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    if not lines:
        raise ConnectionError("empty http request")
    request_line = lines[0].decode("iso-8859-1")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if b":" not in line:
            continue
        name, _, value = line.decode("iso-8859-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    return request_line, headers, rest


def handle_health(sock: socket.socket, request_line: str) -> None:
    n_clients = len(HUB.clients)
    n_active_symbols = len(
        {sym for name in HUB.subs for _, sym, _ in [HUB.parse_stream(name)] if sym}
    )
    last_poll_at = max(HUB.last_poll_at.values()) if HUB.last_poll_at else None
    body = json.dumps(
        {
            "ok": True,
            "n_clients": n_clients,
            "n_active_symbols": n_active_symbols,
            "n_streams": len(HUB.subs),
            "last_poll_at": last_poll_at,
            "dropped_frames": HUB.dropped_frames,
            "uptime_s": int(time.time() - HUB.start_time),
        }
    ).encode()
    resp = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"Access-Control-Allow-Origin: *\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n"
    ) + body
    try:
        sock.sendall(resp)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def handle_http_404(sock: socket.socket) -> None:
    body = b'{"error":"not_found"}'
    resp = (
        b"HTTP/1.1 404 Not Found\r\n"
        b"Content-Type: application/json\r\n"
        b"Access-Control-Allow-Origin: *\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n"
    ) + body
    try:
        sock.sendall(resp)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def handle_ws_session(client: Client, leftover: bytes) -> None:
    """Reader loop for one WebSocket connection. Defragments multi-frame text
    messages, dispatches SUBSCRIBE/UNSUBSCRIBE/LIST_SUBSCRIPTIONS, replies to
    pings, tracks pongs, and exits cleanly on close.
    """
    sock = client.sock
    # If any payload bytes came in with the upgrade request, prepend them.
    # (Most clients won't, but be safe.)
    if leftover:
        # Stuff into a small buffered reader. Since recv_frame uses recv on
        # the raw socket, we wrap with a thin shim.
        class _Buf:
            def __init__(self, sock, pre):
                self.sock = sock
                self.pre = bytearray(pre)

            def recv(self, n):
                if self.pre:
                    take = bytes(self.pre[:n])
                    del self.pre[:n]
                    return take
                return self.sock.recv(n)

            def settimeout(self, *a, **kw):
                return self.sock.settimeout(*a, **kw)

            def setblocking(self, *a, **kw):
                return self.sock.setblocking(*a, **kw)

        reader = _Buf(sock, leftover)
    else:
        reader = sock

    sock.settimeout(None)
    pending_text = bytearray()
    pending_op = None

    while client.alive:
        try:
            opcode, fin, payload = recv_frame(reader)
        except ConnectionError:
            break
        except (OSError, struct.error):
            break

        if opcode == OP_CLOSE:
            client.send_close(1000, "bye")
            break
        if opcode == OP_PING:
            client.send_pong(payload)
            continue
        if opcode == OP_PONG:
            client.last_pong_at = time.time()
            continue

        if opcode == OP_TEXT or opcode == OP_BIN:
            pending_text = bytearray(payload)
            pending_op = opcode
        elif opcode == OP_CONT:
            pending_text.extend(payload)
        else:
            # Unknown opcode -> protocol error.
            client.send_close(1002, "bad_opcode")
            break

        if not fin:
            continue

        if pending_op == OP_TEXT:
            try:
                msg = json.loads(pending_text.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                client.enqueue_text(json.dumps({"error": "bad_json"}).encode())
                pending_text = bytearray()
                pending_op = None
                continue
            handle_ws_message(client, msg)
        # We ignore binary payloads silently — protocol is JSON over text.
        pending_text = bytearray()
        pending_op = None


def handle_ws_message(client: Client, msg: dict) -> None:
    method = msg.get("method")
    params = msg.get("params") or []
    mid = msg.get("id")
    if method == "SUBSCRIBE":
        if not isinstance(params, list):
            client.enqueue_text(json.dumps({"id": mid, "error": "bad_params"}).encode())
            return
        # Validate stream names + cap.
        for raw in params:
            if not isinstance(raw, str) or "@" not in raw:
                client.enqueue_text(json.dumps({"id": mid, "error": "bad_stream"}).encode())
                return
        if len(client.streams) + len(set(params) - client.streams) > MAX_STREAMS_PER_CONN:
            client.enqueue_text(json.dumps({"id": mid, "error": "too_many_streams"}).encode())
            return
        for raw in params:
            kind, sym, ivl = HUB.parse_stream(raw)
            if kind not in ("depth", "trade", "ticker", "kline") or not sym:
                continue
            client.streams.add(raw)
            HUB.add_subscriber(client, raw)
            # Send an immediate snapshot if we already have one cached, so the
            # client doesn't have to wait up to 1-3s for the next poll cycle.
            _send_immediate_snapshot(client, raw, kind, sym, ivl)
        client.enqueue_text(json.dumps({"id": mid, "result": None}).encode())
        return

    if method == "UNSUBSCRIBE":
        if not isinstance(params, list):
            client.enqueue_text(json.dumps({"id": mid, "error": "bad_params"}).encode())
            return
        for raw in params:
            if isinstance(raw, str) and raw in client.streams:
                client.streams.discard(raw)
                HUB.remove_subscriber(client, raw)
        client.enqueue_text(json.dumps({"id": mid, "result": None}).encode())
        return

    if method == "LIST_SUBSCRIPTIONS":
        client.enqueue_text(json.dumps({"id": mid, "result": sorted(client.streams)}).encode())
        return

    client.enqueue_text(json.dumps({"id": mid, "error": "unknown_method"}).encode())


def _send_immediate_snapshot(
    client: Client, stream: str, kind: str, symbol: str, interval: str | None
) -> None:
    """When a client subscribes, ship them whatever cached snapshot we have so
    the page renders right away instead of waiting for the next poll tick.

    If we don't have a cached snapshot we fall back to a synchronous poll —
    but in that case the poll itself will fan-out to subscribers (including
    this client), so we don't enqueue a second copy here.
    """
    if kind == "depth":
        snap = HUB.depth_state.get(symbol)
        if snap is not None:
            client.enqueue_text(
                json.dumps({"stream": stream, "data": snap}, separators=(",", ":")).encode(),
                stream=stream,
            )
        else:
            try:
                HUB.poll_depth_once(symbol)  # fans out by itself
            except Exception as e:  # noqa: BLE001
                log("depth snapshot poll skipped:", symbol, e)
    elif kind == "ticker":
        snap = HUB.last_ticker.get(symbol)
        if snap is not None:
            client.enqueue_text(
                json.dumps({"stream": stream, "data": snap}, separators=(",", ":")).encode(),
                stream=stream,
            )
        else:
            try:
                HUB.poll_ticker_once(symbol)  # fans out by itself
            except Exception as e:  # noqa: BLE001
                log("ticker snapshot poll skipped:", symbol, e)
    elif kind == "kline" and interval:
        bar = HUB.last_kline.get((symbol, interval))
        if bar is not None:
            client.enqueue_text(
                json.dumps(
                    {"stream": stream, "data": {"e": "kline", "s": symbol, "k": bar}},
                    separators=(",", ":"),
                ).encode(),
                stream=stream,
            )
        else:
            try:
                HUB.poll_klines_once(symbol, interval)  # fans out by itself
            except Exception as e:  # noqa: BLE001
                log("kline snapshot poll skipped:", symbol, interval, e)
    # trade@: nothing to "snapshot" — the next real trade will arrive within
    # TRADES_INTERVAL. No-op.


class WSHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock: socket.socket = self.request

        # Cap connections.
        with HUB.lock:
            if len(HUB.clients) >= MAX_CONNECTIONS:
                try:
                    sock.sendall(
                        b"HTTP/1.1 503 Service Unavailable\r\n"
                        b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                finally:
                    sock.close()
                return

        # Sniff first byte: 'G' (or any HTTP verb) means an HTTP request.
        # WebSocket clients also start with 'G' (GET ...), so we have to read
        # the full request and decide based on Upgrade header.
        try:
            sock.settimeout(10.0)
            first = sock.recv(1)
            if not first:
                sock.close()
                return
            request_line, headers, leftover = _read_http_request_line_and_headers(sock, first)
        except (ConnectionError, OSError, TimeoutError):
            try:
                sock.close()
            except OSError:
                pass
            return

        # Plain HTTP /health
        path = request_line.split(" ", 2)[1] if " " in request_line else "/"
        if headers.get("upgrade", "").lower() != "websocket":
            if path.split("?", 1)[0] in ("/health", "/health/"):
                handle_health(sock, request_line)
                return
            handle_http_404(sock)
            return

        # WebSocket upgrade
        resp = make_handshake_response(headers)
        if resp is None:
            try:
                sock.sendall(
                    b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
            finally:
                sock.close()
            return
        try:
            sock.sendall(resp)
        except OSError:
            sock.close()
            return

        client = Client(sock, HUB, self.client_address)
        HUB.register_client(client)
        client.start_writer()
        try:
            handle_ws_session(client, leftover)
        finally:
            HUB.unregister_client(client)
            client.shutdown("session ended")


class ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    # Forward uncaught exceptions to the central error_collector
    # (loopback-only POST to :5690). Best-effort, never raises.
    try:
        from _error_reporter import install_global_handler  # type: ignore

        install_global_handler()
    except Exception as e:  # noqa: BLE001
        log("error reporter install skipped:", e)
    try:
        _otel_install()
    except Exception:  # noqa: BLE001
        log("otel install skipped")
    HUB.poll_exchange_info()
    HUB.run_pollers()
    server = ThreadingTCPServer((LISTEN_HOST, port), WSHandler)
    sys.stderr.write(f"[ws_feed] listening on {LISTEN_HOST}:{port} " f"(rest_base={REST_BASE})\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[ws_feed] shutting down\n")
        HUB.stop_event.set()
        server.shutdown()


if __name__ == "__main__":
    main()
