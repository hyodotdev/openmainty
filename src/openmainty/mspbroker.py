"""Loopback CONNECT broker for the subscription-broker profile.

Gate (2), slice 296. The broker profile's sandbox policy
allows the worker egress ONLY to this broker on loopback;
the broker itself enforces the host allowlist (exact
host:port, default api.meta.ai:443 only). Allowed CONNECT
requests open one upstream TCP socket and relay bytes both
ways with no TLS interception and no inspection. Anything
else gets 403 and opens no upstream socket.

The broker records ONLY metadata per connection: method,
target, decision, tunnel byte counts, open/close times,
and header NAMES. It never stores or logs header values
or tunnel bytes. Binding is loopback only; any other bind
address refuses. Connections are bounded, idle tunnels
time out, and stop() joins every thread.
"""

import copy
import socket
import threading
import time

LOOPBACK = "127.0.0.1"
DEFAULT_ALLOW = (("api.meta.ai", 443),)

_MAX_HEADER_BYTES = 8192
_MAX_HEADER_NAMES = 32
_MAX_LOG_ENTRIES = 4096
_DEFAULT_MAX_CONNECTIONS = 16
# Matches the canary turn timeout (300 s): a quiet model
# stream must be cut by the host, never by our broker.
_DEFAULT_IDLE_TIMEOUT_S = 300.0
_DEFAULT_UPSTREAM_TIMEOUT_S = 10.0
_RELAY_CHUNK = 65536

_FORBIDDEN_403 = (b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n"
                  b"Connection: close\r\n\r\n")
_OK_200 = (b"HTTP/1.1 200 Connection established\r\n\r\n")


class BrokerError(ValueError):
    """Fail-closed broker error (bad input or bind failure)."""


def _check_allow(allow):
    checked = []
    for entry in allow:
        try:
            host, port = entry
        except (TypeError, ValueError) as exc:
            raise BrokerError(
                "allow entry must be (host, port)") from exc
        if not isinstance(host, str) or not host:
            raise BrokerError("allow host must be text")
        if isinstance(port, bool) or not isinstance(port, int):
            raise BrokerError("allow port must be an integer")
        if port < 1 or port > 65535:
            raise BrokerError("allow port out of range")
        checked.append((host.lower(), port))
    return tuple(checked)


def _parse_target(target):
    """Split a CONNECT target into (host, port); None if bad.

    Exact host:port only. No userinfo, no brackets, no
    default ports: anything else is denied, never guessed.
    """
    if not isinstance(target, str):
        return None
    if "@" in target or "[" in target or "]" in target:
        return None
    if target.count(":") != 1:
        return None
    host, _, port_text = target.partition(":")
    if not host:
        return None
    if not port_text:
        return None
    for char in port_text:
        if char < "0" or char > "9":
            return None
    port = int(port_text, 10)
    if port < 1 or port > 65535:
        return None
    return (host.lower(), port)


def _reduce_target(method, target):
    """Reduce a request target to log-safe metadata.

    CONNECT with an exact host:port logs "host:port";
    anything else logs the fixed label "unparsed" (never
    userinfo or secrets). Non-CONNECT logs the absolute-URI
    hostname only — never path or query — or "?" when it
    has none.
    """
    if method == "CONNECT":
        parsed = _parse_target(target)
        if parsed is None:
            return "unparsed"
        return "%s:%d" % parsed
    try:
        from urllib.parse import urlsplit
        host = urlsplit(target).hostname
    except Exception:
        host = None
    if not host:
        return "?"
    return host[:128]


class ConnectBroker:
    """A loopback CONNECT broker with an exact allowlist."""

    def __init__(self, allow=DEFAULT_ALLOW, bind=LOOPBACK,
                 max_connections=_DEFAULT_MAX_CONNECTIONS,
                 idle_timeout_s=_DEFAULT_IDLE_TIMEOUT_S,
                 upstream_timeout_s=(
                     _DEFAULT_UPSTREAM_TIMEOUT_S)):
        if bind != LOOPBACK:
            raise BrokerError(
                "broker binds loopback only: %r" % (bind,))
        self._allow = _check_allow(tuple(allow))
        if isinstance(max_connections, bool) \
                or not isinstance(max_connections, int) \
                or max_connections < 1:
            raise BrokerError("max_connections must be >= 1")
        self._max_connections = max_connections
        self._idle_timeout_s = float(idle_timeout_s)
        self._upstream_timeout_s = float(upstream_timeout_s)
        self._srv = None
        self.port = None
        self._stop = threading.Event()
        self._accept_thread = None
        self._lock = threading.Lock()
        self._active = 0
        self._seq = 0
        self._log = []
        self._stopped = False
        self._workers = set()

    def start(self):
        """Bind the loopback ephemeral port; return the port."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((LOOPBACK, 0))
            srv.listen(16)
            srv.settimeout(0.5)
        except OSError as exc:
            try:
                srv.close()
            except OSError:
                pass
            raise BrokerError(
                "broker bind failed: %s" % exc) from exc
        self._srv = srv
        self.port = srv.getsockname()[1]
        self._accept_thread = threading.Thread(
            target=self._serve, daemon=True)
        self._accept_thread._msp_broker = True
        self._accept_thread.start()
        return self.port

    def stop(self):
        """Close the listener and join every broker thread.

        Joins the accept thread, then every tracked handler
        thread (pumps are joined by their handler) under a
        bounded total wait. Returns {"joined": n, "alive":
        [names]}: any thread still alive after the bound is
        reported by name instead of being waited out. An
        open tunnel's "allowed" entry is therefore recorded
        before stop() returns whenever its handler joined.
        """
        self._stop.set()
        srv, self._srv = self._srv, None
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        worker = self._accept_thread
        if worker is not None:
            worker.join(timeout=10)
        with self._lock:
            pending = list(self._workers)
        deadline = time.monotonic() + 15
        alive = []
        joined = 0
        for thread in pending:
            budget = deadline - time.monotonic()
            if budget > 0:
                thread.join(timeout=budget)
            if thread.is_alive():
                alive.append(thread.name)
            else:
                joined += 1
        with self._lock:
            self._stopped = True
        return {"joined": joined, "alive": alive}

    def log(self):
        """A deep copy of the metadata log (read-only)."""
        with self._lock:
            return copy.deepcopy(self._log)

    def _stopped_for_test(self):
        with self._lock:
            return self._stopped

    def _record(self, entry):
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self._log.append(entry)
            if len(self._log) > _MAX_LOG_ENTRIES:
                del self._log[:len(self._log)
                              - _MAX_LOG_ENTRIES]

    def _serve(self):
        while not self._stop.is_set():
            srv = self._srv
            if srv is None:
                break
            try:
                conn, _peer = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                if self._active >= self._max_connections:
                    capped = True
                else:
                    capped = False
                    self._active += 1
            if capped:
                try:
                    conn.sendall(_FORBIDDEN_403)
                except OSError:
                    pass
                try:
                    conn.close()
                except OSError:
                    pass
                self._record({"method": "?",
                              "target": "?",
                              "decision": "denied-capped",
                              "header_names": [],
                              "bytes_up": 0, "bytes_down": 0,
                              "at_open": time.time(),
                              "at_close": time.time()})
                continue
            worker = threading.Thread(
                target=self._handle, args=(conn,),
                daemon=True)
            worker._msp_broker = True
            with self._lock:
                self._workers.add(worker)
            worker.start()

    def _read_head(self, conn):
        """Read until the header-block end; NAMES only.

        Returns (method, target, names). Header values are
        never retained: only the text before each colon is
        kept, capped in count and length.
        """
        raw = b""
        try:
            while b"\r\n\r\n" not in raw \
                    and len(raw) < _MAX_HEADER_BYTES:
                part = conn.recv(4096)
                if not part:
                    break
                raw += part
        except OSError:
            return ("?", "?", [])
        try:
            head = raw.split(b"\r\n\r\n", 1)[0].decode(
                "ascii")
        except UnicodeDecodeError:
            return ("?", "?", [])
        if not head:
            return ("?", "?", [])
        lines = head.split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) < 2:
            return ("?", "?", [])
        method = parts[0][:16]
        target = parts[1][:128]
        names = []
        for hline in lines[1:][:_MAX_HEADER_NAMES]:
            if ":" in hline:
                names.append(
                    hline.split(":", 1)[0].strip()[:64])
        return (method, target, names)

    def _handle(self, conn):
        at_open = time.time()
        try:
            conn.settimeout(5.0)
            method, target, names = self._read_head(conn)
            logged = _reduce_target(method, target)
            if method != "CONNECT":
                try:
                    conn.sendall(_FORBIDDEN_403)
                except OSError:
                    pass
                self._record({"method": method,
                              "target": logged,
                              "decision": "denied-not-connect",
                              "header_names": names,
                              "bytes_up": 0, "bytes_down": 0,
                              "at_open": at_open,
                              "at_close": time.time()})
                return
            want = _parse_target(target)
            if want is None or want not in self._allow:
                try:
                    conn.sendall(_FORBIDDEN_403)
                except OSError:
                    pass
                self._record({"method": method,
                              "target": logged,
                              "decision": "denied",
                              "header_names": names,
                              "bytes_up": 0, "bytes_down": 0,
                              "at_open": at_open,
                              "at_close": time.time()})
                return
            self._tunnel(conn, method, logged, names,
                         at_open)
        finally:
            with self._lock:
                self._active -= 1
                self._workers.discard(
                    threading.current_thread())
            try:
                conn.close()
            except OSError:
                pass

    def _tunnel(self, conn, method, target, names,
                at_open):
        """Relay one allowed tunnel; count bytes, keep none.

        The "allowed" entry is recorded the moment the 200
        is sent (bytes 0, at_close None) so log() shows
        open tunnels; the same entry is updated in place
        when the tunnel ends.
        """
        host, port = _parse_target(target)
        try:
            upstream = socket.create_connection(
                (host, port),
                timeout=self._upstream_timeout_s)
        except OSError:
            try:
                conn.sendall(_FORBIDDEN_403)
            except OSError:
                pass
            self._record({"method": method,
                          "target": target,
                          "decision": "denied-upstream",
                          "header_names": names,
                          "bytes_up": 0, "bytes_down": 0,
                          "at_open": at_open,
                          "at_close": time.time()})
            return
        upstream.settimeout(1.0)
        conn.settimeout(1.0)
        try:
            conn.sendall(_OK_200)
        except OSError:
            try:
                upstream.close()
            except OSError:
                pass
            self._record({"method": method,
                          "target": target,
                          "decision": "denied-200-failed",
                          "header_names": names,
                          "bytes_up": 0, "bytes_down": 0,
                          "at_open": at_open,
                          "at_close": time.time()})
            return
        entry = {"method": method,
                 "target": target,
                 "decision": "allowed",
                 "header_names": list(names),
                 "bytes_up": 0, "bytes_down": 0,
                 "at_open": at_open,
                 "at_close": None}
        self._record(entry)
        counts = {"up": 0, "down": 0}
        last_io = [time.time()]
        done = threading.Event()

        def _pump(src, dst, key):
            try:
                while not done.is_set() \
                        and not self._stop.is_set():
                    try:
                        data = src.recv(_RELAY_CHUNK)
                    except socket.timeout:
                        if time.time() - last_io[0] \
                                > self._idle_timeout_s:
                            break
                        continue
                    except OSError:
                        break
                    if not data:
                        break
                    try:
                        dst.sendall(data)
                    except OSError:
                        break
                    counts[key] += len(data)
                    last_io[0] = time.time()
            finally:
                done.set()

        first = threading.Thread(
            target=_pump, args=(conn, upstream, "up"),
            daemon=True)
        second = threading.Thread(
            target=_pump, args=(upstream, conn, "down"),
            daemon=True)
        first._msp_broker = True
        second._msp_broker = True
        first.start()
        second.start()
        first.join(timeout=self._idle_timeout_s + 30)
        second.join(timeout=self._idle_timeout_s + 30)
        try:
            upstream.close()
        except OSError:
            pass
        with self._lock:
            entry["bytes_up"] = counts["up"]
            entry["bytes_down"] = counts["down"]
            entry["at_close"] = time.time()
