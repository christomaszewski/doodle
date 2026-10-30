"""Radio, link and power-control observations as SERVICE_HEALTH.md diagnostics. No ROS here.

Two sources, never mixed:

- `service` comes from the supervisor's own state (`Controller.health_state`), read without its
  lock or any HTTP, so the heartbeat never waits on the radio.
- `radio`, `links`, `link/<mac>`, `mesh/<mac>`, `system` and `temperature` come from `poll`, a
  read-only pass over the radio API that runs on its own thread and API session.

Failed reads keep the last observation, marked `retrying` and later STALE; a missing measurement
is published as null with a metric state, never as zero. See docs/PLAN.md for the metric keys.
"""
import json
import math
import re
import threading
import time
import uuid

import doodle_config
import power
from radio_api import LINKSTATE, RadioError, ReplyError, UbusError

OK, WARN, ERROR, STALE = 0, 1, 2, 3
SERVICE = "doodle"
GROUPS = ("radio", "links", "system")      # what one poll observes
NO_OVERRIDE = 255                          # ath9k_tpcd: ACK/broadcast power follows the level
DEBUGFS = "/sys/kernel/debug/ieee80211/{phy}/ath9k/{name}"
PHY = re.compile(r"^phy[0-9]+$")
MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
# Field names that usually carry a peer's address; any MAC-shaped value is the fallback.
MAC_FIELDS = ("mac", "macaddr", "mac_addr", "addr", "address", "orig_address", "originator",
              "station", "sta")
# Vendor fields behind the vendor-neutral link quality keys (SERVICE_HEALTH.md Revision 3). The
# per-peer linkstate format is not observed on hardware yet: these are the names the radio's
# linkstate configuration enables (include_rssi, include_pl_ratio, include_mesh_tq), plus common
# spellings. A value outside the key's plausible range (an RSSI on a vendor scale) is not mapped.
SIGNAL_FIELDS = ("signal_dbm", "rssi_dbm", "rssi", "signal")
SNR_FIELDS = ("snr_db", "snr")
LOSS_FIELDS = ("loss_pct", "pl_ratio")     # pl_ratio is a percentage: the radio alerts above 60
MAX_LOST_PEERS = 32                        # departed peers remembered (oldest dropped first)
MAX_TEXT = 256                             # longest string value copied from a peer entry


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def status(component, level=OK, message="OK", values=None):
    return {"component": component, "level": level, "message": message, "values": values or {}}


def _round(value, scale=1, digits=3):
    return round(value * scale, digits) if numeric(value) else None


def _float(value):
    """linkstate writes some numbers as strings ("chan_width": "20")."""
    if numeric(value):
        return value
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return int(number) if number.is_integer() else number if math.isfinite(number) else None
    return None


def _key(prefix, name):
    return prefix + re.sub(r"[^a-z0-9_]+", "_", str(name).lower()).strip("_")


def _peer_values(prefix, entry):
    """Every field of one linkstate peer entry, as flat values. The per-peer format has not been
    observed on hardware yet (docs/RADIO_API.md), so nothing here depends on field names."""
    values = {}
    for name, value in sorted(entry.items()):
        key = _key(prefix, name)
        if key == prefix or value is None:
            continue
        if isinstance(value, bool) or numeric(value):
            values[key] = value
        elif isinstance(value, str):
            values[key] = value[:MAX_TEXT]
        elif isinstance(value, (list, dict)):
            values[key] = json.dumps(value, sort_keys=True, allow_nan=False, default=str)[:MAX_TEXT]
    return values


def _measure(entry, names, low, high):
    for name in names:
        value = _float(entry.get(name))
        if value is not None and low <= value <= high:
            return value
    return None


def link_quality(kind, entry, noise_dbm=None):
    """One peer's link in vendor-neutral keys: signal, SNR (reported, or derived from this same
    report's noise floor), link quality (BATMAN TQ as a percentage) and packet loss. A measurement
    the peer entry lacks is left out, never zero."""
    values = {}
    signal = _measure(entry, SIGNAL_FIELDS, -130, 0)
    snr = _measure(entry, SNR_FIELDS, -30, 90)
    if snr is None and signal is not None and numeric(noise_dbm):
        snr = round(signal - noise_dbm, 1)
    quality = _measure(entry, ("quality_pct",), 0, 100)
    tq = _measure(entry, ("tq",), 0, 255)
    if quality is None and tq is not None:
        quality = round(tq * 100 / 255)
    for key, value in (("signal_dbm", signal), ("snr_db", snr), ("quality_pct", quality),
                       ("loss_pct", _measure(entry, LOSS_FIELDS, 0, 100))):
        if value is not None:
            values[f"{kind}.{key}"] = value
    return values


def peer_address(entry):
    if not isinstance(entry, dict):
        return None
    candidates = [entry.get(name) for name in MAC_FIELDS] + list(entry.values())
    for value in candidates:
        if isinstance(value, str) and MAC.match(value.strip().lower()):
            return value.strip().lower()
    return None


class HealthReporter:
    def __init__(self, instance, vehicle="", settings=None, *, clock=time.monotonic):
        self.instance, self.vehicle = instance, vehicle
        self.settings = doodle_config.health(settings)
        self.clock = clock
        self.publisher_id = str(uuid.uuid4())
        self.sequence = 0
        self.samples = {}        # group -> (monotonic time, sample id, [reports])
        self.failures = {}       # group -> why the latest attempt failed
        self.peers = {}          # (kind, mac) -> {"seen": monotonic, "values": {...}}
        self.board = None
        self.uptime = None
        self.hardware_id = ""
        self.override_state = {}  # debugfs name -> "unsupported" once the API cannot read it
        self.started = clock()
        self.lock = threading.RLock()

    # --- collection (health thread) -------------------------------------------------------------

    def record(self, group, reports):
        with self.lock:
            self.samples[group] = (self.clock(), str(uuid.uuid4()), reports)
            self.failures.pop(group, None)

    def fail(self, group, error):
        with self.lock:
            self.failures[group] = str(error)

    def identify(self, board):
        with self.lock:
            self.board = board
            model = board.get("sub_model") or board.get("parent_model") or board.get("model")
            self.hardware_id = " ".join(str(v) for v in (model, board.get("hostname")) if v)

    def poll(self, radio):
        """One read-only pass. A per-call failure (permission, missing file, malformed reply)
        only blanks what that call provides; an outage or rejected login fails the whole pass."""
        errors = {}

        def attempt(what, call):
            try:
                return call()
            except ReplyError as error:
                errors[what] = error
                return None

        try:
            system = attempt("system", radio.system_info)
            uptime = (system or {}).get("uptime")
            rebooted = numeric(uptime) and numeric(self.uptime) and uptime < self.uptime
            if self.board is None or rebooted:
                board = attempt("board", radio.board)
                if board is not None:
                    self.identify(board)
            if rebooted:
                self.override_state.clear()  # a firmware change may have changed what is readable
            if numeric(uptime):
                self.uptime = uptime
            rf_up = attempt("rf", radio.rf_up)
            wireless = attempt("wireless", radio.wireless) if rf_up else None
            control = attempt("power control", radio.power_control)
            linkstate = attempt("linkstate", lambda: radio.read_json(LINKSTATE))
            if linkstate is not None and not isinstance(linkstate, dict):
                errors["linkstate"] = ReplyError(f"{LINKSTATE} is not a JSON object")
                linkstate = None
            overrides = {}
            phy = (wireless or {}).get("phy")
            if rf_up and isinstance(phy, str) and PHY.match(phy):
                for name in ("txpower_selfgen", "txpower_mcast"):
                    overrides[name] = self._read_override(radio, phy, name, errors)
        except RadioError as error:  # unreachable, login rejected: nothing in this pass is current
            for group in GROUPS:
                self.fail(group, error)
            return

        self.record("radio", [self._radio(rf_up, wireless, control, linkstate, overrides, errors)])
        if linkstate is None:
            self.fail("links", errors.get("linkstate", "no link report"))
        else:
            self.record("links", self._links(linkstate))
        if system is None:
            self.fail("system", errors.get("system", "no system information"))
        else:
            self.record("system", self._system(system))

    def _read_override(self, radio, phy, name, errors):
        if self.override_state.get(name) == "unsupported":
            return None
        try:
            text = radio.read_text(DEBUGFS.format(phy=phy, name=name)).strip()
        except UbusError as error:
            if error.status == 4:  # no such file: not an ath9k radio with automatic power control
                self.override_state[name] = "unsupported"
                return None
            errors[name] = error
            return None
        except ReplyError as error:
            errors[name] = error
            return None
        if not re.fullmatch(r"-?\d+", text):
            # rpcd reads nothing from a file that reports size 0 on some builds: not readable here
            self.override_state[name] = "unsupported"
            return None
        return int(text)

    def _radio(self, rf_up, wireless, control, linkstate, overrides, errors):
        values = {"rf.state": "on" if rf_up else "off" if rf_up is False else None}
        missing = []  # sources of temporarily unavailable measurements (unsupported is not news)

        def put(key, value, *, state=None, source=None):
            values[key] = value
            if value is None:
                reason = state or "unavailable"
                values[f"health.metric.{key}.state"] = reason
                if reason != "unsupported":
                    missing.append(source or key)

        if rf_up is None:
            missing.append("rf")
        if control is None:
            put("rf.auto_power_control", None, source="power control")
            put("rf.auto_power_control_aggressive", None, source="power control")
        else:
            values["rf.auto_power_control"] = control["automatic"]
            values["rf.auto_power_control_aggressive"] = control["aggressive"]
        level = None
        if rf_up:
            # The debugfs files need the phy name from `iwinfo info`; a reply without one means
            # this firmware cannot tell us, a failed call is a temporary gap.
            readable = wireless is None or (isinstance(wireless.get("phy"), str)
                                            and PHY.match(wireless["phy"]))
            wireless = wireless or {}
            level = wireless.get("txpower") if numeric(wireless.get("txpower")) else None
            put("rf.tx_power_dbm", level, source="wireless")
            put("rf.channel", wireless.get("channel") if numeric(wireless.get("channel")) else None,
                source="wireless")
            put("rf.frequency_mhz", _round(wireless.get("frequency")), source="wireless")
            link = linkstate or {}
            # A field the link report lacks is not reported by this firmware or linkstate
            # configuration; a failed read of the report is a temporary gap.
            absent = "unavailable" if linkstate is None else "unsupported"
            width = _float(link.get("chan_width"))
            if width is None and control is not None:
                width = _float(control.get("chanbw"))
            put("rf.bandwidth_mhz", width, state=absent, source="linkstate")
            put("rf.noise_dbm", _round(link.get("noise"), digits=1), state=absent,
                source="linkstate")
            put("rf.noise_peak_dbm", _round(link.get("noise_peak"), digits=1), state=absent,
                source="linkstate")
            put("rf.channel_activity_pct", _round(_float(link.get("activity"))), state=absent,
                source="linkstate")
            lna = link.get("lna_status")
            put("rf.lna_status", str(lna) if isinstance(lna, (str, int)) else None, state=absent,
                source="linkstate")
            for key, name in (("rf.ack_power_dbm", "txpower_selfgen"),
                              ("rf.broadcast_power_dbm", "txpower_mcast")):
                raw = overrides.get(name)
                if raw is None:
                    state = self.override_state.get(name) or (
                        "unavailable" if readable else "unsupported")
                    put(key, None, state=state, source="wireless" if not overrides else name)
                    continue
                values[key.replace("_dbm", "_override")] = raw != NO_OVERRIDE
                put(key, level if raw == NO_OVERRIDE else raw, source=name)

        warnings = []
        if missing:
            detail = sorted({str(errors.get(m, m)) for m in missing})
            warnings.append("measurement unavailable: " + "; ".join(detail))
        if numeric(level):
            for key, what in (("rf.ack_power_dbm", "ACK"), ("rf.broadcast_power_dbm", "broadcast")):
                if numeric(values.get(key)) and values[key] > level:
                    warnings.append(f"{what} power {values[key]} dBm is above the {level} dBm "
                                    "transmit power level (automatic power control)")
        if warnings:
            return status("radio", WARN, "; ".join(warnings), values)
        if rf_up is False:
            return status("radio", OK, "RF off", values)
        summary = [f"{level} dBm" if numeric(level) else "RF on"]
        if numeric(values.get("rf.channel")):
            band = ", ".join(f"{values[k]:g} MHz" + suffix for k, suffix in
                             (("rf.frequency_mhz", ""), ("rf.bandwidth_mhz", " wide"))
                             if numeric(values.get(k)))
            summary.append(f"channel {values['rf.channel']}" + (f" ({band})" if band else ""))
        return status("radio", OK, ", ".join(summary), values)

    def _links(self, linkstate):
        now = self.clock()
        reports, unidentified = [], 0
        counts = {}
        noise = _float(linkstate.get("noise"))
        for kind, field, count_key in (("link", "sta_stats", "sta_count"),
                                       ("mesh", "mesh_stats", "mesh_count")):
            entries = linkstate.get(field) if isinstance(linkstate.get(field), list) else []
            count = linkstate.get(count_key)
            counts[kind] = count if numeric(count) else len(entries)
            present = set()
            for entry in entries:
                mac = peer_address(entry)
                if mac is None:
                    unidentified += 1
                    continue
                present.add(mac)
                self.peers[(kind, mac)] = {"seen": now, "values": {
                    **_peer_values(f"{kind}.", entry), **link_quality(kind, entry, noise)}}
            for (peer_kind, mac), peer in self.peers.items():
                if peer_kind != kind:
                    continue
                values = {f"{kind}.present": mac in present, **peer["values"]}
                if mac in present:
                    reports.append(status(f"{kind}/{mac}", OK, "reported", values))
                else:
                    age = round(now - peer["seen"], 1)
                    reports.append(status(f"{kind}/{mac}", OK, f"not reported for {age:g} s",
                                          {f"{kind}.present": False, f"{kind}.last_seen_s": age}))
        lost = sorted((peer["seen"], key) for key, peer in self.peers.items() if peer["seen"] < now)
        for _, key in lost[:max(0, len(lost) - MAX_LOST_PEERS)]:
            del self.peers[key]
            reports = [r for r in reports if r["component"] != f"{key[0]}/{key[1]}"]
        values = {"links.stations": counts["link"], "links.mesh_nodes": counts["mesh"]}
        message = f"{counts['link']} station(s), {counts['mesh']} mesh node(s)"
        if unidentified:
            values["links.unidentified"] = unidentified
            message += f"; entries without a recognizable address: {unidentified}"
        return [status("links", OK, message, values)] + sorted(reports,
                                                                key=lambda r: r["component"])

    def _system(self, system):
        board = self.board or {}
        memory = system.get("memory") if isinstance(system.get("memory"), dict) else {}
        load = system.get("load") if isinstance(system.get("load"), list) else []
        values = {"uptime_s": system.get("uptime") if numeric(system.get("uptime")) else None}
        for index, key in enumerate(("system.load_1m", "system.load_5m", "system.load_15m")):
            values[key] = _round(load[index], 1 / 65536, 2) if index < len(load) else None
        values["system.memory_total_mb"] = _round(memory.get("total"), 1e-6, 1)
        values["system.memory_free_mb"] = _round(memory.get("free"), 1e-6, 1)
        for key in [k for k, v in values.items() if v is None]:
            values[f"health.metric.{key}.state"] = "unavailable"
        for key, field in (("system.firmware", "firmware"), ("system.model", "model"),
                           ("system.parent_model", "parent_model"),
                           ("system.sub_model", "sub_model"), ("system.hostname", "hostname")):
            if isinstance(board.get(field), str) and board[field]:
                values[key] = board[field]
        missing = [k for k in values if k.startswith("health.metric.")]
        system_report = status("system", WARN if missing else OK,
                               "measurement unavailable" if missing else
                               board.get("firmware") or "OK", values)
        # The radio has no temperature sensor (no hwmon/thermal zones): say so explicitly.
        temperature = status("temperature", OK, "unsupported by hardware",
                             {"temp.radio_c": None,
                              "health.metric.temp.radio_c.state": "unsupported",
                              "health.availability": "unsupported"})
        return [system_report, temperature]

    # --- publication (heartbeat thread) ---------------------------------------------------------

    def _service(self, state):
        snapshot = state["snapshot"]
        target = snapshot["target"]
        values = {"rf.target": power.describe(target) if target is not None else None,
                  "rf.target_dbm": snapshot.get("target_dbm"),
                  "rf.tx_power_dbm": snapshot.get("tx_power_dbm"),
                  "rf.state": snapshot.get("rf"),
                  "rf.converged": bool(snapshot.get("converged")),
                  "rf.changing": state.get("changing") or "",
                  "rf.startup": power.describe(snapshot["startup"]),
                  "rf.shutdown": power.describe(snapshot["shutdown"])}
        span = snapshot.get("range_dbm")
        values["rf.tx_power_min_dbm"], values["rf.tx_power_max_dbm"] = span or (None, None)
        if state["at"] is None:
            return status("service", STALE, "waiting for first observation", values)
        if snapshot.get("error"):
            return status("service", ERROR, snapshot["error"], values)
        if state.get("changing"):
            return status("service", OK, state["changing"], values)
        if snapshot.get("converged"):
            return status("service", OK, snapshot.get("detail") or "OK", values)
        return status("service", WARN, snapshot.get("detail") or "not at target", values)

    def snapshot(self, state):
        """The complete component set for one publication. `state` is Controller.health_state()."""
        now = self.clock()
        with self.lock:
            self.sequence += 1
            groups = [("service", state["at"] if state["at"] is not None else self.started,
                       str(state.get("sample_id", "")), [self._service(state)])]
            for group in GROUPS:
                at, sample_id, reports = self.samples.get(group, (self.started, "", None))
                if reports is None:
                    names = ("system", "temperature") if group == "system" else (group,)
                    reports = [status(name, STALE, "waiting for first observation")
                               for name in names]
                groups.append((group, at, sample_id, reports))
            stale_after = self.settings["stale_after_s"]
            output = []
            for group, at, sample_id, reports in groups:
                error = self.failures.get(group)
                if group == "service":
                    # In-process state: current unless the control loop stopped observing.
                    old = now - at > stale_after and not state.get("changing")
                    available = "stale" if old and state["at"] is not None else "current"
                else:
                    available = ("stale" if now - at > stale_after else
                                 "retrying" if error else "current")
                for report in reports:
                    values = {"health.sample_age_s": round(max(0.0, now - at), 3),
                              "health.sample_id": sample_id, "health.availability": available,
                              **report["values"]}
                    level, message = report["level"], report["message"]
                    for metric, bounds in self.settings["limits"].items():
                        value = values.get(metric)
                        if not numeric(value):
                            continue
                        for bound, severity in (("error_above", ERROR), ("error_below", ERROR),
                                                ("warn_above", WARN), ("warn_below", WARN)):
                            if bound in bounds and (value > bounds[bound] if bound.endswith("above")
                                                    else value < bounds[bound]):
                                if level != STALE:
                                    level = max(level, severity)
                                message += f"; {metric} {value:g} crossed {bound} {bounds[bound]:g}"
                                break
                    if available == "stale" and level != STALE:
                        values["health.last_level"] = level
                        level = STALE
                    if level == STALE:
                        values["health.availability"] = "stale"
                    if error:
                        values["health.error"] = error
                    values.update({"health.instance": self.instance,
                                   "health.vehicle_id": self.vehicle,
                                   "health.service": SERVICE,
                                   "health.publisher_id": self.publisher_id,
                                   "health.sequence": self.sequence,
                                   "health.stale_after_s": stale_after,
                                   "health.publish_interval_s": self.settings["interval_s"]})
                    output.append({"name": f"{self.instance}: {report['component']}",
                                   "level": level, "message": message,
                                   "hardware_id": self.hardware_id, "values": values})
            return output
