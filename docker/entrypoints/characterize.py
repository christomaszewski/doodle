"""Characterization mode: what the radio characterization analysis needs, recorded. No ROS here.

Off unless `characterization.enabled` in the radio config. Then a poll of its own reads this radio
and, when `characterization.gcs` names it, the GCS radio over the mesh, every
`characterization.poll_interval_s`, each over its own API session. health_ros.py publishes each
snapshot as JSON on /<namespace>/radio/characterization, beside /diagnostics in the bag.

Only reads: nothing here writes to either radio. A failed read is recorded where its data would
have been, never left out, and a radio that cannot be reached costs one bounded request per poll.
The GCS radio is reached with this radio's API password.

Per radio: RF state; `iwinfo info` (frequency, channel, TX power, noise); the link report without
its wrong radio clock (it carries its own `oper_freq`, so a sample just after a channel switch is
labelled by the report it came from); the operating channel's airtime counters; `iw station dump`;
and ath9k_tpcd's per-peer files (`peer_signal_reply` is how the peer hears this radio).
"""
import json
import math
import time

from radio_api import IW, LINKSTATE, RadioAPI, RadioError, ReplyError

SCHEMA = 1
TIMEOUT = 2.0  # s per request, as for the diagnostics poll
STATIONS = "/sys/kernel/debug/ieee80211/{phy}/netdev:{device}/stations"
PEER_FILES = ("peer_signal", "peer_signal_reply", "fixed_txpower")


class Collector:
    def __init__(self, radios, interval, instance="", vehicle="", *, clock=time.time_ns,
                 monotonic=time.monotonic):
        self.radios = radios  # role ("vehicle", "gcs") -> RadioAPI
        self.interval = interval
        self.instance, self.vehicle = instance, vehicle
        self.clock, self.monotonic = clock, monotonic
        self.sequence = 0

    @classmethod
    def from_config(cls, config, password, instance="", vehicle=""):
        """None unless characterization is enabled."""
        settings = config.get("characterization") or {}
        if not settings.get("enabled"):
            return None
        radios = {"vehicle": RadioAPI.from_config(config["connection"], password, timeout=TIMEOUT)}
        if settings.get("gcs") is not None:
            radios["gcs"] = RadioAPI.from_config(settings["gcs"], password, timeout=TIMEOUT)
        return cls(radios, settings["poll_interval_s"], instance, vehicle)

    def poll(self):
        """One snapshot of every radio."""
        started = self.monotonic()
        self.sequence += 1
        snapshot = {"schema": SCHEMA, "instance": self.instance, "vehicle_id": self.vehicle,
                    "sequence": self.sequence, "stamp_unix_ns": self.clock(),
                    "radios": {role: read(radio) for role, radio in self.radios.items()}}
        snapshot["poll_s"] = round(self.monotonic() - started, 3)
        return snapshot

    def poll_json(self):
        """The snapshot as strict JSON: a non-finite number becomes null instead of breaking it."""
        return json.dumps(finite(self.poll()), allow_nan=False, default=str,
                          separators=(",", ":"))

    def close(self):
        for radio in self.radios.values():
            radio.close()


def finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [finite(item) for item in value]
    return value


def read(radio):
    """Everything one radio reports about its link."""
    found = {}
    try:
        found["rf_up"] = radio.rf_up()
    except ReplyError as error:
        found["rf_up"] = {"error": str(error)}
    except RadioError as error:  # unreachable or login refused: nothing else would answer either
        return {"unreachable": str(error)}

    def attempt(name, action):
        try:
            found[name] = action()
        except RadioError as error:
            found[name] = {"error": str(error)}

    attempt("wireless", radio.wireless)
    wireless = found["wireless"] if "error" not in found["wireless"] else {}
    attempt("linkstate", lambda: link_report(radio))
    attempt("survey", lambda: radio.survey(required(wireless, "frequency")))
    attempt("station_dump", lambda: station_dump(radio))
    attempt("peer_files", lambda: peer_files(radio, required(wireless, "phy")))
    return found


def required(wireless, key):
    if wireless.get(key) is None:
        raise ReplyError(f"iwinfo info gave no {key}")
    return wireless[key]


def link_report(radio):
    report = radio.read_json(LINKSTATE)
    if not isinstance(report, dict):
        raise ReplyError(f"{LINKSTATE} is not a JSON object")
    return {key: value for key, value in report.items() if key != "sysinfo"}  # wrong radio clock


def station_dump(radio):
    reply = radio.call("file", "exec", {"command": IW,
                                        "params": ["dev", radio.device, "station", "dump"]})
    if reply.get("code") != 0:
        detail = str(reply.get("stderr") or "").strip()[:200]
        raise ReplyError(f"iw station dump exited {reply.get('code')}" +
                         (f": {detail}" if detail else ""))
    return str(reply.get("stdout") or "")


def peer_files(radio, phy):
    stations = STATIONS.format(phy=phy, device=radio.device)
    found = {}
    for station in radio.call("file", "list", {"path": stations}).get("entries") or []:
        name = station.get("name")
        if not isinstance(name, str) or "/" in name:
            continue
        found[name] = {}
        for item in PEER_FILES:
            try:
                found[name][item] = radio.read_text(f"{stations}/{name}/{item}").strip()
            except RadioError as error:
                found[name][item] = {"error": str(error)}
    return found
