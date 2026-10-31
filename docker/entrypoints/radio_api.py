"""Client for a Doodle Labs radio's ubus JSON-RPC API (`POST /ubus`). See docs/RADIO_API.md.

- Bounded: every request has a timeout. One instance serializes its own calls; the supervisor and
  the health reporter each use their own instance (and session), so a slow read never delays a
  change.
- Self-healing: an expired session (JSON-RPC -32002) logs in again once; a dropped keep-alive
  connection reconnects once.
- Secret-safe: replies that carry the mesh key (`network.wireless status`) are reduced to the
  fields the service needs inside this module, and no error message includes a reply body or
  the password.
"""
import fnmatch
import hashlib
import http.client
import json
import ssl
import threading

NULL_SESSION = "0" * 32
IW = "/usr/sbin/iw"
LINKSTATE = "/tmp/linkstate_current.json"
UBUS_STATUS = {1: "invalid command", 2: "invalid argument", 3: "method not found",
               4: "not found", 5: "no data", 6: "permission denied", 7: "timeout",
               8: "not supported", 9: "unknown error", 10: "connection failed"}

# What each kind of work needs from the login's ACLs: (scope, object or path, method or permission).
PERMISSIONS = {
    "observe": [("ubus", "system", "info"), ("ubus", "system", "board"),
                ("ubus", "iwinfo", "info"), ("ubus", "iwinfo", "txpowerlist"),
                ("ubus", "network.wireless", "status"), ("ubus", "uci", "get")],
    "set_power": [("ubus", "file", "exec"), ("file", IW, "exec")],
    "rf_toggle": [("ubus", "network.wireless", "up"), ("ubus", "network.wireless", "down")],
}
_TRUE = ("1", "true", "yes", "on")


class RadioError(RuntimeError):
    """The radio is unreachable, or a call failed."""


class AuthError(RadioError):
    """The radio rejected the API login."""


class ReplyError(RadioError):
    """The radio answered, but not with what was asked for (a malformed reply or a JSON-RPC
    error). Unlike an outage, other calls in the same pass can still succeed."""


class UbusError(ReplyError):
    def __init__(self, what, status):
        self.status = status
        super().__init__(f"{what}: {UBUS_STATUS.get(status, f'ubus status {status}')}")


class RadioAPI:
    def __init__(self, host, username, password, *, scheme="https", port=None, fingerprint="",
                 device="wlan0", uci_radio="radio0", timeout=5.0, transport=None):
        self.host, self.username, self.scheme = host, username, scheme
        self.port = port or (443 if scheme == "https" else 80)
        self.fingerprint = fingerprint
        self.device, self.uci_radio, self.timeout = device, uci_radio, timeout
        self._password = password
        self._transport = transport or self._http
        self._lock = threading.Lock()
        self._conn = None
        self._session = None
        self._id = 0
        self.acls = {}

    @classmethod
    def from_config(cls, connection, password, **kwargs):
        return cls(connection["host"], connection["username"], password,
                   scheme=connection["scheme"], port=connection["port"],
                   fingerprint=connection["tls_fingerprint_sha256"],
                   device=connection["device"], uci_radio=connection["uci_radio"], **kwargs)

    def __repr__(self):  # never show the password
        return f"RadioAPI({self.scheme}://{self.host}:{self.port} as {self.username})"

    # --- transport ------------------------------------------------------------------------------

    def _open(self):
        if self.scheme == "https":
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            # The radio's certificate is self-signed; authenticity comes from the optional pin.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout,
                                               context=context)
            conn.connect()
            if self.fingerprint:
                seen = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
                if seen != self.fingerprint:
                    conn.close()
                    raise RadioError(f"radio TLS certificate {seen} does not match "
                                     "tls_fingerprint_sha256 (update the pin if the radio's "
                                     "certificate was renewed)")
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            conn.connect()
        # Never let http.client reconnect behind our back: every connection must pass the pin.
        conn.auto_open = 0
        return conn

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _http(self, payload, timeout):
        body = json.dumps(payload).encode()
        for attempt in (0, 1):
            try:
                if self._conn is not None and self._conn.sock is None:
                    self.close()  # the radio closed the keep-alive connection
                if self._conn is None:
                    self._conn = self._open()
                self._conn.sock.settimeout(timeout)
                self._conn.request("POST", "/ubus", body, {"Content-Type": "application/json"})
                response = self._conn.getresponse()
                data = response.read()
                if response.status != 200:
                    self.close()
                    raise RadioError(f"radio answered HTTP {response.status} on /ubus")
                return json.loads(data)
            except RadioError:
                self.close()
                raise
            except ValueError:
                self.close()
                raise ReplyError("radio sent a reply that is not JSON") from None
            except (ConnectionError, http.client.RemoteDisconnected) as error:
                # A keep-alive connection the radio already closed: reconnect once.
                self.close()
                if attempt:
                    raise RadioError(f"radio unreachable at {self.host}: {error}") from None
            except (OSError, http.client.HTTPException) as error:  # timeouts, TLS, DNS
                self.close()
                raise RadioError(f"radio unreachable at {self.host}: "
                                 f"{error.__class__.__name__}: {error}") from None

    def _rpc(self, params, timeout):
        self._id += 1
        reply = self._transport({"jsonrpc": "2.0", "id": self._id, "method": "call",
                                 "params": params}, timeout or self.timeout)
        if not isinstance(reply, dict):
            raise ReplyError("radio sent a malformed reply")
        return reply

    @staticmethod
    def _data(what, reply):
        result = reply.get("result")
        if not isinstance(result, list) or not result or not isinstance(result[0], int):
            raise ReplyError(f"{what}: malformed reply")
        if result[0] != 0:
            raise UbusError(what, result[0])
        data = result[1] if len(result) > 1 else {}
        if not isinstance(data, dict):
            raise ReplyError(f"{what}: malformed reply")
        return data

    # --- session --------------------------------------------------------------------------------

    def _login(self):
        self._session = None
        reply = self._rpc([NULL_SESSION, "session", "login",
                           {"username": self.username, "password": self._password}], None)
        try:
            if reply.get("error"):
                raise UbusError("login", 6)
            data = self._data("login", reply)
            session = data["ubus_rpc_session"]
            if not isinstance(session, str) or not session:
                raise KeyError
        except (RadioError, KeyError):
            raise AuthError(f"radio rejected the API login for '{self.username}': wrong password, "
                            "radio still booting, or no such login (after upgrading from pre-2024 "
                            "firmware with settings kept there is no 'user' login; see "
                            "docs/RADIO_SETUP.md)") from None
        self._session = session
        self.acls = data.get("acls") if isinstance(data.get("acls"), dict) else {}

    def call(self, obj, method, args=None, *, timeout=None):
        what = f"{obj}.{method}"
        with self._lock:
            for attempt in (0, 1):
                if self._session is None:
                    self._login()
                reply = self._rpc([self._session, obj, method, args or {}], timeout)
                error = reply.get("error")
                if error:
                    if isinstance(error, dict) and error.get("code") == -32002 and not attempt:
                        self._session = None  # expired or unknown session: log in again once
                        continue
                    message = error.get("message") if isinstance(error, dict) else None
                    raise ReplyError(f"{what}: {message or 'JSON-RPC error'}")
                return self._data(what, reply)

    def missing_permissions(self, *needs):
        """Permissions from PERMISSIONS[need] that this login's ACLs do not grant."""
        with self._lock:
            if self._session is None:
                self._login()
            acls = self.acls
        missing = []
        for need in needs:
            for scope, name, perm in PERMISSIONS[need]:
                table = acls.get(scope) if isinstance(acls.get(scope), dict) else {}
                granted = any(fnmatch.fnmatchcase(name, pattern) and isinstance(perms, list)
                              and any(p == "*" or fnmatch.fnmatchcase(perm, p) for p in perms)
                              for pattern, perms in table.items())
                if not granted:
                    missing.append(f"{scope} {name} {perm}")
        return missing

    def require(self, *needs):
        missing = self.missing_permissions(*needs)
        if missing:
            raise RadioError(f"the API login '{self.username}' lacks permission for: "
                             f"{', '.join(missing)} (firmware 2025-06+ 'user' has full access)")

    # --- reads ------------------------------------------------------------------------------------

    def board(self):
        data = self.call("system", "board")
        release = data.get("release") if isinstance(data.get("release"), dict) else {}
        return {"hostname": data.get("hostname"), "model": data.get("model"),
                "parent_model": data.get("parent_model"), "sub_model": data.get("sub_model"),
                "firmware": release.get("version")}

    def system_info(self):
        data = self.call("system", "info")
        return {"uptime": data.get("uptime"), "load": data.get("load"),
                "memory": data.get("memory")}

    def wireless(self):
        data = self.call("iwinfo", "info", {"device": self.device})
        return {key: data.get(key) for key in
                ("txpower", "channel", "frequency", "noise", "country", "htmode", "bssid", "phy")}

    def txpower_list(self):
        data = self.call("iwinfo", "txpowerlist", {"device": self.device})
        results = data.get("results") if isinstance(data.get("results"), list) else []
        return sorted({r["dbm"] for r in results if isinstance(r, dict)
                       and isinstance(r.get("dbm"), int) and not isinstance(r["dbm"], bool)})

    def survey(self, frequency):
        """Cumulative airtime counters (ms) of the channel at `frequency` MHz: how long the
        radio was on it, sensed it busy, received and transmitted."""
        data = self.call("iwinfo", "survey", {"device": self.device})
        for entry in data.get("results") if isinstance(data.get("results"), list) else []:
            if isinstance(entry, dict) and entry.get("mhz") == frequency:
                return {key: entry.get(key)
                        for key in ("active_time", "busy_time", "rx_time", "tx_time")}
        raise ReplyError(f"iwinfo.survey: no entry for {frequency} MHz")

    def rf_up(self):
        # The full reply includes the mesh key; only the up flag leaves this method.
        radio = self.call("network.wireless", "status").get(self.uci_radio)
        if not isinstance(radio, dict):
            raise ReplyError(f"the radio has no wireless device '{self.uci_radio}'")
        return radio.get("up") is True

    def power_control(self):
        values = self.call("uci", "get", {"config": "wireless", "section": self.uci_radio}) \
                     .get("values") or {}
        return {"automatic": str(values.get("dynamic_txpower", "0")).lower() in _TRUE,
                "aggressive": str(values.get("dynamic_txpower_aggr", "0")).lower() in _TRUE,
                "chanbw": values.get("chanbw")}

    def read_text(self, path):
        data = self.call("file", "read", {"path": path}).get("data")
        if not isinstance(data, str):
            raise ReplyError(f"file.read {path}: malformed reply")
        return data

    def read_json(self, path):
        try:
            return json.loads(self.read_text(path))
        except ValueError:
            raise ReplyError(f"{path} on the radio is not JSON") from None

    # --- writes (live only; nothing here touches flash) ------------------------------------------

    def set_txpower(self, dbm):
        if isinstance(dbm, bool) or not isinstance(dbm, int):
            raise ValueError("transmit power must be whole dBm")
        data = self.call("file", "exec", {"command": IW, "params": [
            "dev", self.device, "set", "txpower", "fixed", str(dbm * 100)]})
        if data.get("code") != 0:
            detail = str(data.get("stderr") or "").strip()[:200]
            raise RadioError(f"iw set txpower {dbm} dBm failed (exit {data.get('code')})"
                             + (f": {detail}" if detail else ""))

    def set_rf(self, up):
        self.call("network.wireless", "up" if up else "down", {"device": self.uci_radio})
