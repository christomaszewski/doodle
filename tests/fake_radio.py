"""A simulated Doodle Labs radio speaking the ubus JSON-RPC API.

Reply shapes follow docs/RADIO_API.md (firmware 2026-03.1); every value is synthetic. The model
reproduces the behaviors the service depends on: the 19-32 dBm range with silent clamping,
`auto`/RF-on returning to the maximum, RF off removing the interface, session expiry, reboots,
and replies that carry the mesh key.
"""
import http.server
import json
import threading

FULL_ACL = {"access-group": {"custom_dl_user": ["read", "write"], "unauthenticated": ["read"]},
            "cgi-io": {"*": ["read", "write"]}, "file": {"*": ["*"]},
            "ubus": {"*": ["*"], "session": ["access", "login"]},
            "uci": {"*": ["read", "write"]}}
MESH_KEY = "FIXTURE-MESH-KEY-must-not-leak"
NOT_FOUND, PERMISSION_DENIED = 4, 6


class FakeRadio:
    def __init__(self, password="fixture-password"):
        self.password = password
        self.acls = FULL_ACL
        self.lock = threading.Lock()
        self.sessions = set()
        self.calls = []                  # (object, method, args) after login
        self.supported = list(range(19, 33))
        self.txpower = 32
        self.rf_up = True
        self.rf_up_after = 0             # network.wireless status polls before RF reports up
        self._up_in = None
        self.uptime = 1000
        self.frequency, self.channel = 2450, 51
        self.dynamic_txpower = "1"
        self.ignore_txpower = False      # a radio that accepts `iw` but does not change
        self.fail = {}                   # "object.method" -> ubus status to return
        self.unreadable = set()          # file paths whose `file read` answers "not found"
        self.unreachable = False
        # ath9k_tpcd's ACK/broadcast power overrides (255 = follow the level), read via debugfs
        self.debugfs = {"txpower_selfgen": "255\n", "txpower_mcast": "255\n"}
        self.sta_stats, self.mesh_stats = [], []  # linkstate peer entries (format synthetic)
        # `iwinfo survey` counters (ms) for the current channel; each read adds `survey_step`
        self.survey = {"active_time": 30000, "busy_time": 250, "rx_time": 150, "tx_time": 15}
        self.survey_step = {"active_time": 5000, "busy_time": 40, "rx_time": 25, "tx_time": 2}

    # --- scenario helpers ------------------------------------------------------------------------

    def reboot(self):
        with self.lock:
            self.uptime, self.txpower, self.rf_up, self._up_in = 5, 32, True, None
            self.sessions.clear()

    def wifi_restart(self):
        with self.lock:
            self.txpower = 32

    def expire_sessions(self):
        with self.lock:
            self.sessions.clear()

    def writes(self):
        return [c for c in self.calls if c[:2] in (("file", "exec"), ("network.wireless", "up"),
                                                  ("network.wireless", "down"))]

    # --- JSON-RPC ---------------------------------------------------------------------------------

    def __call__(self, payload, timeout=None):
        """In-memory transport for RadioAPI(transport=...)."""
        if self.unreachable:
            from radio_api import RadioError
            raise RadioError("radio unreachable at fake: timed out")
        return json.loads(json.dumps(self.handle(json.loads(json.dumps(payload)))))

    def handle(self, request):
        with self.lock:
            session, obj, method, args = request["params"]
            reply = {"jsonrpc": "2.0", "id": request.get("id")}
            if (obj, method) == ("session", "login"):
                if args.get("username") != "user" or args.get("password") != self.password:
                    reply["error"] = {"code": -32002, "message": "Access denied"}
                    return reply
                session = f"{len(self.sessions) + 1:032x}"
                self.sessions.add(session)
                reply["result"] = [0, {"ubus_rpc_session": session, "timeout": 300,
                                       "expires": 299, "acls": self.acls,
                                       "data": {"username": "user"}}]
                return reply
            if session not in self.sessions:
                reply["error"] = {"code": -32002, "message": "Access denied"}
                return reply
            self.calls.append((obj, method, args))
            status = self.fail.get(f"{obj}.{method}")
            if status is not None:
                reply["result"] = [status]
                return reply
            result = self._method(obj, method, args)
            reply["result"] = result if isinstance(result, list) else [0, result]
            return reply

    def _method(self, obj, method, args):
        if (obj, method) == ("system", "info"):
            self.uptime += 1
            return {"localtime": 1700000000, "uptime": self.uptime, "load": [2000, 3000, 4000],
                    "memory": {"total": 60000000, "free": 12000000}}
        if (obj, method) == ("system", "board"):
            return {"kernel": "4.14.221", "hostname": "smartradio-example",
                    "system": "Qualcomm Atheros QCA9533 ver 2 rev 0",
                    "model": "DoodleLabs SmartRadio", "board_name": "smartradio",
                    "release": {"distribution": "Doodle Labs", "version": "firmware-2026-03.1"},
                    "parent_model": "EXAMPLE-PARENT", "sub_model": "EXAMPLE-SUB"}
        if (obj, method) == ("network.wireless", "status"):
            if self._up_in is not None:
                self._up_in -= 1
                if self._up_in <= 0:
                    self.rf_up, self._up_in = True, None
            return {"radio0": {"up": self.rf_up, "pending": False, "autostart": True,
                               "disabled": False, "config": {"channel": str(self.channel)},
                               "interfaces": [{"section": "wifi0", "ifname": "wlan0",
                                               "config": {"mode": "mesh",
                                                          "mesh_id": "example-mesh",
                                                          "key": MESH_KEY}}]}}
        if (obj, method) in (("network.wireless", "down"), ("network.wireless", "up")):
            if args.get("device") != "radio0":
                return [NOT_FOUND]
            if method == "down":
                self.rf_up = False
            else:
                self.txpower = max(self.supported)  # back from saved config, not last setting
                if self.rf_up_after:
                    self._up_in = self.rf_up_after
                else:
                    self.rf_up = True
            return {}
        if obj == "iwinfo":
            if not self.rf_up or args.get("device") != "wlan0":
                return [NOT_FOUND]
            if method == "info":
                return {"phy": "phy0", "ssid": "example-mesh", "bssid": "00:30:1A:12:34:56",
                        "country": "US", "mode": "Mesh Point", "channel": self.channel,
                        "frequency": self.frequency, "txpower": self.txpower, "noise": -94,
                        "htmode": "HT20"}
            if method == "txpowerlist":
                return {"results": [{"dbm": d, "mw": round(10 ** (d / 10)),
                                     "active": d == self.txpower} for d in self.supported]}
            if method == "survey":
                current = dict(self.survey, mhz=self.frequency, noise=-94, busy_time_ext=0)
                for key, step in self.survey_step.items():
                    self.survey[key] += step
                other = {"mhz": self.frequency + 5, "noise": 0, "active_time": 0,
                         "busy_time": 0, "busy_time_ext": 0, "rx_time": 0, "tx_time": 0}
                return {"results": [other, current]}
        if (obj, method) == ("uci", "get"):
            if args == {"config": "wireless", "section": "radio0"}:
                return {"values": {".type": "wifi-device", ".name": "radio0", "chanbw": "20",
                                   "channel": str(self.channel),
                                   "dynamic_txpower": self.dynamic_txpower,
                                   "dynamic_txpower_aggr": "0"}}
            return [NOT_FOUND]
        if (obj, method) == ("file", "exec"):
            command, params = args.get("command"), args.get("params") or []
            if command == "/usr/sbin/iw" and params[:5] == ["dev", "wlan0", "set", "txpower",
                                                            "fixed"]:
                if not self.rf_up:
                    return {"code": 237, "stderr": "command failed: No such device (-19)"}
                if not self.ignore_txpower:
                    wanted = int(params[5]) // 100
                    self.txpower = min(max(wanted, min(self.supported)), max(self.supported))
                return {"code": 0}  # exits 0 even when it clamped
            return {"code": 1, "stderr": "unexpected command"}
        if (obj, method) == ("file", "read"):
            path = args.get("path")
            if path in self.unreadable:
                return [NOT_FOUND]
            if path == "/tmp/linkstate_current.json":
                return {"data": json.dumps({
                    "sysinfo": {"cpu_load": [20000, 18000, 19000], "freemem": 12000000,
                                "localtime": 1700000000},
                    "oper_chan": self.channel, "oper_freq": self.frequency, "chan_width": "20",
                    "noise": -94.0, "noise_peak": -92.0, "activity": 1, "lna_status": "1",
                    "sta_count": len(self.sta_stats), "sta_stats": self.sta_stats,
                    "mesh_count": len(self.mesh_stats), "mesh_stats": self.mesh_stats})}
            prefix = "/sys/kernel/debug/ieee80211/phy0/ath9k/"
            if self.rf_up and path.startswith(prefix) and path[len(prefix):] in self.debugfs:
                return {"data": self.debugfs[path[len(prefix):]]}
            return [NOT_FOUND]
        return [3]  # method not found


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, like uhttpd

    def do_POST(self):
        if self.path != "/ubus":
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.server.radio.unreachable:  # drop the connection without answering
            self.close_connection = True
            return
        self.server.connections.add(id(self.connection))
        data = json.dumps(self.server.radio.handle(json.loads(body))).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if self.server.close_after_reply:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, radio, address=("127.0.0.1", 0)):
        super().__init__(address, Handler)
        self.radio = radio
        self.connections = set()
        self.close_after_reply = False

    def handle_error(self, request, client_address):
        pass  # clients that hang up mid-handshake (TLS pin tests) are expected

    def __enter__(self):
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.shutdown()
        self.server_close()
        self.thread.join()
