"""Radio, link and power-control observations as SERVICE_HEALTH.md diagnostics. No ROS here.

Two sources, never mixed:

- `service` comes from the supervisor's own state (`Controller.health_state`), read without its
  lock or any HTTP, so the heartbeat never waits on the radio.
- `radio`, `links`, `link/<mac>`, `mesh/<mac>`, `system` and `temperature` come from `poll`, a
  read-only pass over the radio API that runs on its own thread and API session.

Each group is stamped when its own reads returned, not when the whole pass ended. Failed reads keep
the last observation, marked `retrying` and later STALE; a missing measurement is published as null
with a metric state, never as zero. A link report the radio has stopped rewriting is not presented
as new. See docs/PLAN.md for the metric keys.
"""
import hashlib
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
# Per-station files of ath9k_tpcd (docs/RADIO_API.md): how the peer hears us (and the power we
# used), and the per-peer power limit automatic power control set (255 = none).
STATION_DEBUGFS = "/sys/kernel/debug/ieee80211/{phy}/netdev:{device}/stations/{mac}/{name}"
STATION_FILES = ("fixed_txpower", "peer_signal_reply")
STATION_MISSING_PASSES = 3                 # passes of "no such file" for every peer = unsupported
PHY = re.compile(r"^phy[0-9]+$")
MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
# Field names that usually carry a peer's address. Otherwise an entry's only MAC-shaped value is
# used; an entry with several (a mesh route also names its next hop) is not guessed at.
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
MAX_TEXT = 2048                            # longest string or nested JSON copied from a peer
MIN_AIRTIME_WINDOW_MS = 1000               # shortest counter window worth a percentage
# The radio rewrites its link report every 2 s (linkstate.linkstate.interval); a report unchanged
# for this long means the radio's linkstate service has stopped, so its contents are not current.
LINKSTATE_STALE_S = 6
UNSUPPORTED = (3, 8)                       # ubus: method not found, not supported
# wifi-device settings published as radio settings: (UCI option, key)
SETTINGS = (("distance", "rf.distance_m"), ("txpower", "rf.saved_tx_power_dbm"),
            ("antenna_gain", "rf.antenna_gain_dbi"), ("txantenna", "rf.tx_antennas"),
            ("rxantenna", "rf.rx_antennas"))


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


def _text(values, key, value):
    """A string value, cut at MAX_TEXT and flagged when it was cut."""
    values[key] = value[:MAX_TEXT]
    if len(value) > MAX_TEXT:
        values[f"{key}_truncated"] = True


def _peer_values(prefix, entry):
    """Every field of one linkstate peer entry, as flat values. The per-peer format has not been
    observed on hardware yet (docs/RADIO_API.md), so nothing here depends on field names. Nested
    values become JSON text, or are left out and flagged `<key>_truncated` when too long: never a
    cut-off, unparseable JSON string."""
    values = {}
    for name, value in sorted(entry.items(), key=lambda item: str(item[0])):
        key = _key(prefix, name)
        if key == prefix or value is None:
            continue
        if isinstance(value, bool) or numeric(value):
            values[key] = value
        elif isinstance(value, str):
            _text(values, key, value)
        elif isinstance(value, (list, dict)):
            try:
                text = json.dumps(value, sort_keys=True, allow_nan=False, default=str)
            except (TypeError, ValueError):  # NaN/Infinity inside: not strict JSON
                text = None
            if text is None or len(text) > MAX_TEXT:
                values[f"{key}_truncated"] = True
            else:
                values[key] = text
    return values


def _station_table_values(entry):
    """One `iwinfo assoclist` station as flat `link.iw_*` values (the rx/tx rate tables flattened,
    e.g. `link.iw_tx_rate` in kbit/s, `link.iw_tx_retries`)."""
    values = {}
    for name, value in entry.items():
        if name == "mac":
            continue
        if name in ("rx", "tx") and isinstance(value, dict):
            items = [(_key(f"link.iw_{name}_", sub), item) for sub, item in value.items()]
        else:
            items = [(_key("link.iw_", name), value)]
        for key, item in items:
            if isinstance(item, bool) or numeric(item):
                values[key] = item
            elif isinstance(item, str):
                _text(values, key, item)
    return dict(sorted(values.items()))


def _measure(entry, names, low, high):
    for name in names:
        value = _float(entry.get(name))
        if value is not None and low <= value <= high:
            return value
    return None


def _dbm(value):
    """A dBm reading from the station table; iwinfo reports 0 when it has none."""
    return value if numeric(value) and -130 <= value < 0 else None


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


def station_quality(entry, noise_dbm=None):
    """Vendor-neutral keys from one `iwinfo assoclist` station: signal (the kernel's average, else
    the last frame's), SNR against the station's noise reading or the radio's noise floor, and the
    current transmit PHY rate (kbit/s in the table)."""
    values = {}
    signal = _dbm(entry.get("signal_avg")) or _dbm(entry.get("signal"))
    noise = _dbm(entry.get("noise")) or (noise_dbm if numeric(noise_dbm) else None)
    if signal is not None:
        values["link.signal_dbm"] = signal
        if noise is not None:
            values["link.snr_db"] = round(signal - noise, 1)
    tx = entry.get("tx") if isinstance(entry.get("tx"), dict) else {}
    if numeric(tx.get("rate")) and 0 < tx["rate"] <= 10_000_000:
        values["link.rate_mbps"] = round(tx["rate"] / 1000, 1)
    return values


def peer_address(entry):
    if not isinstance(entry, dict):
        return None
    for name in MAC_FIELDS:
        value = entry.get(name)
        if isinstance(value, str) and MAC.match(value.strip().lower()):
            return value.strip().lower()
    found = {value.strip().lower() for value in entry.values()
             if isinstance(value, str) and MAC.match(value.strip().lower())}
    return found.pop() if len(found) == 1 else None


def _report_version(linkstate):
    """What identifies one rewrite of the link report: the radio's clock at the rewrite (wrong as
    a wall time, but it advances), else the report's content."""
    sysinfo = linkstate.get("sysinfo") if isinstance(linkstate.get("sysinfo"), dict) else {}
    if numeric(sysinfo.get("localtime")):
        return ("localtime", sysinfo["localtime"])
    text = json.dumps(linkstate, sort_keys=True, default=str)
    return ("content", hashlib.sha256(text.encode()).hexdigest())


class HealthReporter:
    def __init__(self, instance, vehicle="", settings=None, *, clock=time.monotonic):
        self.instance, self.vehicle = instance, vehicle
        self.settings = doodle_config.health(settings)
        self.names = self.settings["peers"]  # MAC -> operator's name for that peer
        self.clock = clock
        self.publisher_id = str(uuid.uuid4())
        self.sequence = 0
        self.revision = 0        # increments with every recorded group (a new observation)
        self.samples = {}        # group -> (monotonic time, sample id, [reports])
        self.failures = {}       # group -> why the latest attempt failed
        self.peers = {}          # (kind, mac) -> {"seen": monotonic, "values": {...}}
        self.board = None
        self.uptime = None
        self.hardware_id = ""
        self.capability_state = {}  # read -> "unsupported" once the API cannot provide it
        self.survey_previous = None  # (frequency, airtime counters, monotonic time) baseline
        self.report_version = None   # identity of the latest link report seen
        self.report_changed_at = None  # monotonic time it last changed
        self.station_missing = {}    # station file -> consecutive passes it was missing for all
        self.started = clock()
        self.lock = threading.RLock()

    # --- collection (health thread) -------------------------------------------------------------

    def record(self, group, reports, at=None):
        with self.lock:
            self.samples[group] = (self.clock() if at is None else at, str(uuid.uuid4()), reports)
            self.failures.pop(group, None)
            self.revision += 1

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
            system_at = self.clock()
            uptime = (system or {}).get("uptime")
            rebooted = numeric(uptime) and numeric(self.uptime) and uptime < self.uptime
            if self.board is None or rebooted:
                board = attempt("board", radio.board)
                if board is not None:
                    self.identify(board)
            if rebooted:
                self.capability_state.clear()  # a firmware change may change what is readable
                self.station_missing.clear()
            if numeric(uptime):
                self.uptime = uptime
            rf_up = attempt("rf", radio.rf_up)
            wireless = attempt("wireless", radio.wireless) if rf_up else None
            radio_at = self.clock()
            control = attempt("power control", radio.power_control)
            linkstate = attempt("linkstate", lambda: radio.read_json(LINKSTATE))
            if linkstate is not None and not isinstance(linkstate, dict):
                errors["linkstate"] = ReplyError(f"{LINKSTATE} is not a JSON object")
                linkstate = None
            stations = self._read_station_table(radio, errors) if rf_up else None
            links_at = self.clock()
            overrides = {}
            phy = (wireless or {}).get("phy")
            phy = phy if rf_up and isinstance(phy, str) and PHY.match(phy) else None
            if phy:
                for name in ("txpower_selfgen", "txpower_mcast"):
                    overrides[name] = self._read_override(radio, phy, name, errors)
            survey = survey_at = None
            frequency = (wireless or {}).get("frequency")
            if rf_up and numeric(frequency) and "survey" not in self.capability_state:
                survey = self._read_survey(radio, frequency, errors)
                survey_at = self.clock()
            station_files = {}
            if phy:
                station_files = self._read_station_files(
                    radio, phy, self._station_order(linkstate, stations))
        except RadioError as error:  # unreachable, login rejected: nothing in this pass is current
            for group in GROUPS:
                self.fail(group, error)
            return

        new_report, report_age = False, None
        if linkstate is not None:
            version = _report_version(linkstate)
            if version != self.report_version:
                self.report_version, self.report_changed_at = version, links_at
                new_report = True
            report_age = round(links_at - self.report_changed_at, 1)
            if report_age > LINKSTATE_STALE_S:
                errors["linkstate"] = ReplyError(
                    f"{LINKSTATE} not updated for {report_age:g} s (the radio's linkstate "
                    "service may have stopped)")
                linkstate = None

        noise = self._noise_floor(linkstate, wireless)
        self.record("radio", [self._radio(rf_up, wireless, control, linkstate, overrides, survey,
                                          survey_at, noise, errors)], at=radio_at)
        if linkstate is None and stations is None:
            self.fail("links", errors.get("linkstate") or errors.get("station table")
                      or "no link report")
        elif new_report or stations is not None:
            problems = [str(errors[what]) for what in ("linkstate", "station table")
                        if what in errors]
            self.record("links", self._links(links_at, linkstate, stations, station_files, noise,
                                             report_age, problems), at=links_at)
        # Otherwise the same link report as last time and no station table: nothing new.
        if system is None:
            self.fail("system", errors.get("system", "no system information"))
        else:
            self.record("system", self._system(system), at=system_at)

    @staticmethod
    def _noise_floor(linkstate, wireless):
        """The link report's noise floor, else the driver's own reading (0 there means none)."""
        noise = _round((linkstate or {}).get("noise"), digits=1)
        if noise is None:
            noise = _dbm((wireless or {}).get("noise"))
        return noise

    def _read_station_table(self, radio, errors):
        if self.capability_state.get("assoclist") == "unsupported":
            return None
        try:
            return radio.assoclist()
        except UbusError as error:
            if error.status in UNSUPPORTED:
                self.capability_state["assoclist"] = "unsupported"
                return None
            errors["station table"] = error
        except ReplyError as error:
            errors["station table"] = error
        return None

    def _station_order(self, linkstate, stations):
        """Present stations whose per-peer files to read: named peers first, then by address."""
        macs = set()
        if stations is not None:
            macs.update(filter(None, map(peer_address, stations)))
        elif linkstate is not None and isinstance(linkstate.get("sta_stats"), list):
            macs.update(filter(None, map(peer_address, linkstate["sta_stats"])))
        named = [mac for mac in self.names if mac in macs]
        return (named + sorted(macs - set(named)))[:self.settings["reverse_link_peers"]]

    def _read_station_files(self, radio, phy, macs):
        """Per-peer automatic power control files, raw until their format is observed. A file
        missing for every peer in several passes in a row is not provided by this radio."""
        results = {}
        names = [n for n in STATION_FILES if self.capability_state.get(f"station {n}") is None]
        if not macs or not names:
            return results
        missing = dict.fromkeys(names, 0)
        for mac in macs:
            values = results.setdefault(mac, {})
            for name in names:
                path = STATION_DEBUGFS.format(phy=phy, device=radio.device, mac=mac, name=name)
                try:
                    text = radio.read_text(path).strip()
                except UbusError as error:
                    if error.status == 4:  # no such file (or the peer left mid-pass)
                        missing[name] += 1
                        continue
                    values.update({f"link.{name}": None,
                                   f"health.metric.link.{name}.state": "unavailable"})
                    continue
                except ReplyError:
                    values.update({f"link.{name}": None,
                                   f"health.metric.link.{name}.state": "unavailable"})
                    continue
                if not text:  # rpcd read nothing (debugfs reports size 0 on some builds)
                    missing[name] += 1
                    continue
                values.update(self._station_value(name, text))
        for name in names:
            if missing[name] == len(macs):
                self.station_missing[name] = self.station_missing.get(name, 0) + 1
                if self.station_missing[name] >= STATION_MISSING_PASSES:
                    self.capability_state[f"station {name}"] = "unsupported"
            else:
                self.station_missing[name] = 0
        return results

    @staticmethod
    def _station_value(name, text):
        if name == "fixed_txpower" and re.fullmatch(r"-?\d+", text):
            limit = int(text)
            if limit == NO_OVERRIDE:
                return {"link.apc_limited": False}
            return {"link.apc_limited": True, "link.apc_limit_dbm": limit}
        values = {}
        _text(values, f"link.{name}", text)
        return values

    def _read_survey(self, radio, frequency, errors):
        try:
            return radio.survey(frequency)
        except UbusError as error:
            if error.status in UNSUPPORTED:  # no `iwinfo survey` on this firmware
                self.capability_state["survey"] = "unsupported"
                return None
            errors["survey"] = error
        except ReplyError as error:
            errors["survey"] = error
        return None

    def _airtime(self, frequency, counters, at):
        """Percent of the channel's active time spent transmitting, receiving and sensing it busy,
        over the counter change since the baseline. The first reading only sets the baseline; a
        change shorter than MIN_AIRTIME_WINDOW_MS keeps it and waits for a longer window. When
        the counters restart (other readers, channel changes), the new counters alone are used
        only if they cover no more than the time since the previous reading."""
        keys = ("active_time", "rx_time", "tx_time")
        if not counters or not all(numeric(counters.get(k)) for k in keys):
            return None
        previous = self.survey_previous
        if previous is None or previous[0] != frequency:
            self.survey_previous = (frequency, counters, at)
            return None
        delta = {k: counters[k] - previous[1][k] for k in keys}
        restarted = min(delta.values()) < 0
        if restarted:
            self.survey_previous = (frequency, counters, at)
            elapsed_ms = max(0.0, at - previous[2]) * 1000
            if counters["active_time"] > elapsed_ms * 1.1 + 1000:
                return None  # restarted long before the previous reading: no known window
            window = counters
        elif delta["active_time"] >= MIN_AIRTIME_WINDOW_MS:
            self.survey_previous = (frequency, counters, at)
            window = delta
        else:
            return None
        active = window["active_time"]
        if active < MIN_AIRTIME_WINDOW_MS:
            return None
        busy = counters.get("busy_time") if numeric(counters.get("busy_time")) else None
        if busy is not None and not restarted:
            start = previous[1].get("busy_time")
            busy = busy - start if numeric(start) else None
        return {"tx": round(100 * window["tx_time"] / active, 3),
                "rx": round(100 * window["rx_time"] / active, 3),
                "busy": round(100 * busy / active, 3) if busy is not None and busy >= 0 else None,
                "window_s": round(active / 1000, 1)}

    def _read_override(self, radio, phy, name, errors):
        if self.capability_state.get(name) == "unsupported":
            return None
        try:
            text = radio.read_text(DEBUGFS.format(phy=phy, name=name)).strip()
        except UbusError as error:
            if error.status == 4:  # no such file: not an ath9k radio with automatic power control
                self.capability_state[name] = "unsupported"
                return None
            errors[name] = error
            return None
        except ReplyError as error:
            errors[name] = error
            return None
        if not re.fullmatch(r"-?\d+", text):
            # rpcd reads nothing from a file that reports size 0 on some builds: not readable here
            self.capability_state[name] = "unsupported"
            return None
        return int(text)

    def _radio(self, rf_up, wireless, control, linkstate, overrides, survey, survey_at, noise,
               errors):
        values = {"rf.state": "on" if rf_up else "off" if rf_up is False else None}
        missing = []  # sources of temporarily unavailable measurements (unsupported is not news)

        def put(key, value, *, state=None, source=None, warn=True):
            values[key] = value
            if value is None:
                reason = state or "unavailable"
                values[f"health.metric.{key}.state"] = reason
                if reason != "unsupported" and warn:
                    missing.append(source or key)

        if rf_up is None:
            missing.append("rf")
        if control is None:
            put("rf.auto_power_control", None, source="power control")
            put("rf.auto_power_control_aggressive", None, source="power control")
        else:
            values["rf.auto_power_control"] = control["automatic"]
            values["rf.auto_power_control_aggressive"] = control["aggressive"]
            # Configured settings (what a power cycle returns to, the ACK timeout distance, the
            # antennas in use): published when the radio has them; their absence is not a gap.
            settings = control.get("settings") or {}
            for option, key in SETTINGS:
                if _float(settings.get(option)) is not None:
                    values[key] = _float(settings[option])
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
            uci = (control or {}).get("settings") or {}
            for key, field in (("rf.country", "country"), ("rf.htmode", "htmode")):
                text = wireless.get(field) or uci.get(field)
                if isinstance(text, str) and text:
                    values[key] = text[:64]
            link = linkstate or {}
            # A field the link report lacks is not reported by this firmware or linkstate
            # configuration; a failed or stale read of the report is a temporary gap.
            absent = "unavailable" if linkstate is None else "unsupported"
            width = _float(link.get("chan_width"))
            if width is None and control is not None:
                width = _float(control.get("chanbw"))
            put("rf.bandwidth_mhz", width, state=absent, source="linkstate")
            put("rf.noise_dbm", noise, state=absent, source="linkstate")
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
                    state = self.capability_state.get(name) or (
                        "unavailable" if readable else "unsupported")
                    put(key, None, state=state, source="wireless" if not overrides else name)
                    continue
                values[key.replace("_dbm", "_override")] = raw != NO_OVERRIDE
                put(key, level if raw == NO_OVERRIDE else raw, source=name)
            # Transmit airtime: with the level, the best available proxy for the power the
            # amplifier turns into heat (the radio has no current or temperature sensors).
            airtime = self._airtime(wireless.get("frequency"), survey, survey_at) \
                if survey else None
            survey_state = self.capability_state.get("survey") or "unavailable"
            for key, part in (("rf.tx_airtime_pct", "tx"), ("rf.rx_airtime_pct", "rx"),
                              ("rf.busy_pct", "busy"), ("rf.airtime_window_s", "window_s")):
                # With a window but no busy counter, this firmware does not count busy time.
                state = "unsupported" if airtime and part == "busy" else survey_state
                put(key, airtime[part] if airtime else None, state=state, source="survey",
                    warn=survey is None and "survey" in errors)

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

    def _links(self, now, linkstate, stations, station_files, noise, report_age, problems):
        """Peers from the radio's link report and the kernel's station table (either may be None).
        A peer is present when the latest source covering its kind lists it."""
        unidentified, counts = 0, {}
        current = {"link": {}, "mesh": {}}   # kind -> mac -> this pass's values
        reported_snr = set()                 # stations whose link report carries its own SNR
        if linkstate is not None:
            for kind, field, count_key in (("link", "sta_stats", "sta_count"),
                                           ("mesh", "mesh_stats", "mesh_count")):
                entries = linkstate.get(field) if isinstance(linkstate.get(field), list) else []
                count = linkstate.get(count_key)
                counts[kind] = count if numeric(count) else len(entries)
                for entry in entries:
                    mac = peer_address(entry)
                    if mac is None:
                        unidentified += 1
                        continue
                    current[kind][mac] = {**_peer_values(f"{kind}.", entry),
                                          **link_quality(kind, entry, noise)}
                    if _measure(entry, SNR_FIELDS, -30, 90) is not None:
                        reported_snr.add(mac)
        if stations is not None:
            for entry in stations:
                mac = peer_address(entry)
                if mac is None:
                    unidentified += 1
                    continue
                # The station table's format is known, so its signal and rate win; a SNR the
                # radio reports itself beats one derived here.
                quality = station_quality(entry, noise)
                if mac in reported_snr:
                    quality.pop("link.snr_db", None)
                current["link"].setdefault(mac, {}).update(
                    {**_station_table_values(entry), **quality})
            counts.setdefault("link", len(stations))
        for mac, values in station_files.items():
            if mac in current["link"]:
                current["link"][mac].update(values)
        covered = {"link": linkstate is not None or stations is not None,
                   "mesh": linkstate is not None}

        reports = []
        for kind in ("link", "mesh"):
            for mac, values in current[kind].items():
                self.peers[(kind, mac)] = {"seen": now, "values": values}
            for (peer_kind, mac), peer in self.peers.items():
                if peer_kind != kind:
                    continue
                name = self.names.get(mac)
                label = f"{name}: " if name else ""
                named = {f"{kind}.name": name} if name else {}
                component = f"{kind}/{mac}"
                if mac in current[kind]:
                    reports.append(status(component, OK, label + "reported",
                                          {f"{kind}.present": True, **peer["values"], **named}))
                elif not covered[kind]:
                    reports.append(status(component, OK, label + "not observed (no link report)",
                                          {f"{kind}.present": None, **named,
                                           f"health.metric.{kind}.present.state": "unavailable"}))
                else:
                    age = round(now - peer["seen"], 1)
                    reports.append(status(component, OK, f"{label}not reported for {age:g} s",
                                          {f"{kind}.present": False, f"{kind}.last_seen_s": age,
                                           **named}))
        lost = sorted((peer["seen"], key) for key, peer in self.peers.items() if peer["seen"] < now)
        for _, key in lost[:max(0, len(lost) - MAX_LOST_PEERS)]:
            del self.peers[key]
            reports = [r for r in reports if r["component"] != f"{key[0]}/{key[1]}"]

        values = {"links.stations": counts.get("link"), "links.mesh_nodes": counts.get("mesh")}
        if stations is not None:
            values["links.associated"] = len(stations)
        if report_age is not None:
            values["links.report_age_s"] = report_age
        for key in [k for k, v in values.items() if v is None]:
            values[f"health.metric.{key}.state"] = "unavailable"
        parts = [f"{values['links.stations']} station(s)" if values["links.stations"] is not None
                 else "stations unknown",
                 f"{values['links.mesh_nodes']} mesh node(s)"
                 if values["links.mesh_nodes"] is not None else "mesh nodes unknown"]
        message = ", ".join(parts)
        if unidentified:
            values["links.unidentified"] = unidentified
            message += f"; entries without a recognizable address: {unidentified}"
        level = OK
        if problems:
            level = WARN
            message = "measurement unavailable: " + "; ".join(sorted(problems)) + "; " + message
        return [status("links", level, message, values)] + sorted(reports,
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
