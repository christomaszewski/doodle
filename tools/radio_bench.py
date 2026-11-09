#!/usr/bin/env python3
"""Two-radio bench: the peer data and channel-switch behavior the service has never seen.

  radio_bench.py --a HOST --b HOST check | discover | capture | hop | missed | recover

Read docs/RADIO_BENCH.md first; this is for two radios on a bench, never a flying vehicle.

- `check` answers "is everything working?", even from a machine wired to radio a alone: read-only
  checks of both radios and of the service's own code against them, then (unless --read-only) a
  power change on radio a and, given --to/--home, one mesh-wide channel switch each way.
- `discover` and `capture` only read.
- `hop`, `missed` and `recover` move real radios. They ask first, refuse a mesh with anyone else on
  it, and always finish by bringing both radios back to the home channel at their starting power.
  `missed` and `recover` also need a wired path of its own to radio b.

`message-system chswitch` is vendor code with undocumented arguments, so they are never guessed:
`discover` shows how the firmware calls it, and --to/--home pass that JSON through unchanged.
This tool commits nothing; whether `chswitch` itself writes flash is one of the questions.
Real radios refuse `file exec` of a shell for the `user` login whatever its ACL says, so nothing
here needs one: firmware is searched with `file list` and `file read`, and programs run directly.
Passwords come from DOODLE_BENCH_PASSWORD (and _B if the radios differ) or a prompt, never argv.
"""
import argparse
import base64
import getpass
import json
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "docker" / "entrypoints"))
import doodle_config  # noqa: E402
from health import ERROR, OK, STALE, WARN, HealthReporter, link_quality, peer_address  # noqa: E402
from radio_api import IW, LINKSTATE, RadioAPI, RadioError, ReplyError, UbusError  # noqa: E402
import supervisor  # noqa: E402

POLL = 0.25               # s between looks while a switch settles
SAMPLE = 1.0              # s between link reports in `capture`
SETTLE = 20.0             # s a radio gets to obey a restoring `chswitch`
RF_TIMEOUT = 90.0         # s for RF to come back (docs/RADIO_API.md: observed < 40)
REBOOT_TIMEOUT = 240.0    # s for a radio to reboot and bring RF up
EXEC_TIMEOUT = 30.0       # s for one `file exec`
TOO_STRONG_DBM = -30      # Doodle's vehicle validation guide: may saturate the receiver
WEAK_DBM, LOW_SNR_DB = -85, 10  # the same guide: a "bad" link below either
OLDEST_FIRMWARE = (2025, 6)     # docs/RADIO_API.md: the oldest firmware the service supports
FIRMWARE = re.compile(r"firmware-(\d{4})-(\d{2})")
LEVEL_NAMES = {OK: "OK", WARN: "WARN", ERROR: "ERROR", STALE: "STALE"}
NEEDS_WIRED_B = ("missed", "recover")  # they turn radio b's RF off or reboot it
EXPECTED = {"link": ("link.signal_dbm", "link.snr_db", "link.loss_pct"),
            "mesh": ("mesh.quality_pct",)}
SAVED_WIRELESS = "/etc/config/wireless"  # only ever `file stat`: its contents include the mesh key
VENDOR_CONFIGS = ("message-system", "central_acs", "acs_multiband", "acs_manual_switch",
                  "link-recovery")
STDERR_CHARS = 400        # of a radio command's error text kept in reports
NOT_FOUND, PERMISSION_DENIED = 4, 6  # ubus statuses (docs/RADIO_API.md)
PING = ("/bin/ping", "/usr/bin/ping")  # run directly: there is no shell to find it
BATCTL = "/usr/sbin/batctl"
# The firmware search: directories walked with `file list`, files read with `file read`.
SEARCH_PLACES = ("/usr/sbin", "/etc/init.d", "/usr/share/rpcd", "/usr/share/luci",
                 "/usr/share/ucode", "/usr/lib/lua", "/www", "/usr/bin", "/sbin", "/usr/libexec",
                 "/lib/functions", "/lib/netifd", "/lib/wifi", "/etc/hotplug.d",
                 "/etc/uci-defaults")
CALLER_PATTERN = re.compile(rb"chswitch|mode_change|channel_idx")
STRINGS_OF = ("/usr/sbin/message-system", "/usr/sbin/dl-central-acs", "/usr/sbin/acs_multiband",
              "/usr/sbin/acs_manual_switch")
STRINGS_PATTERN = re.compile(rb"chswitch|channel|bandwidth|mode_change|model|freq", re.I)
PRINTABLE = re.compile(rb"[\x20-\x7e\t]{5,}")  # what `strings` would print from a binary
MAX_READ_BYTES = 256 * 1024  # larger files (busybox and friends) are skipped, and counted
MAX_HITS = 200
SEARCH_BUDGET = 300.0     # s for the whole search on one radio; what it did not reach is listed
SECRET = re.compile(r"key|pass|pswd|psk|secret|token", re.I)
SECRET_VALUE = re.compile(r"((?:key|pass\w*|pswd|psk|secret|token)\w*['\"]?\s*[=: ]\s*).*", re.I)
SECRET_FLAG = re.compile(r"(\s-k\s+)\S+")  # how the Setup Wizard's command line holds the mesh key


def redact(value):
    """Vendor configs and scripts end up in files people pass around: secrets stay behind."""
    if isinstance(value, dict):
        return {k: "<redacted>" if SECRET.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return "\n".join(SECRET_FLAG.sub(r"\1<redacted>", SECRET_VALUE.sub(r"\1<redacted>", line))
                         for line in value.split("\n"))
    return value


def switch_args(text):
    """--to/--home: `chswitch` JSON, which must name its frequency so landing can be checked."""
    try:
        value = json.loads(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"not JSON: {error}") from None
    if not isinstance(value, dict) or not isinstance(value.get("frequency"), int):
        raise argparse.ArgumentTypeError('needs a JSON object with an integer "frequency" in MHz')
    return value


def peer_count(report):
    return max(len(report.get(field) or []) for field in ("sta_stats", "mesh_stats"))


def inventory(entries, kind):
    """What real peer entries hold, and what the service would publish from them."""
    fields, published, unidentified = {}, {}, 0
    for entry, noise in entries:
        unidentified += peer_address(entry) is None
        for name, value in entry.items():
            fields.setdefault(name, set()).add(type(value).__name__)
        for key, value in link_quality(kind, entry, noise).items():
            published.setdefault(key, []).append(value)
    return {"entries": len(entries), "unidentified": unidentified,
            "fields": {name: sorted(types) for name, types in sorted(fields.items())},
            "published": {key: {"min": min(values), "max": max(values)}
                          for key, values in sorted(published.items())},
            "not_published": [key for key in EXPECTED[kind] if key not in published]}


def timing(rounds):
    seen = {}
    for one in rounds:
        for event, seconds in one["events"].items():
            if event != "rejected":
                seen.setdefault(event, []).append(seconds)
    return {event: {"median_s": statistics.median(values), "max_s": max(values),
                    "count": len(values)} for event, values in sorted(seen.items())}


def wired_interfaces(hosts, run=subprocess.run, platform=sys.platform):
    """-> {host: the local interface that reaches it, or None}. Two radios behind one interface
    share a path (one wire, or a shared switch), so radio b then counts as reachable only through
    the mesh. An unknown answer counts the same way: it is the safe assumption."""
    command = ["route", "-n", "get"] if platform == "darwin" else ["ip", "route", "get"]
    found = {}
    for host in hosts:
        try:
            output = run([*command, host], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            output = ""
        match = re.search(r"interface:\s*(\S+)", output) or re.search(r"\bdev\s+(\S+)", output)
        found[host] = match.group(1) if match else None
    return found


def separate_paths(a_path, b_path):
    return None not in (a_path, b_path) and a_path != b_path


class Log:
    """samples.jsonl and report.json in one run's directory. Replies are reduced or redacted
    before they get here. Peer MAC addresses stay in, so runs live under the gitignored var/."""

    def __init__(self, directory, clock=time.monotonic):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.clock, self.started = clock, clock()
        self.samples = (self.directory / "samples.jsonl").open("a")

    def sample(self, kind, **fields):
        line = {"t": round(self.clock() - self.started, 3), "kind": kind, **fields}
        self.samples.write(json.dumps(line, default=str) + "\n")
        self.samples.flush()

    def report(self, report):
        (self.directory / "report.json").write_text(json.dumps(report, indent=2, default=str))


class Node:
    """One radio. Mid-switch failures are observations here, not errors."""

    def __init__(self, name, api, host):
        self.name, self.api, self.host = name, api, host
        self.ping_refused = False  # learned once, so polling loops do not keep asking

    def observe(self):
        seen = {"rf": None, "frequency": None, "channel": None, "tx_power": None, "peers": None}
        try:
            seen["rf"] = self.api.rf_up()
            if seen["rf"]:
                wireless = self.api.wireless()
                seen.update(frequency=wireless.get("frequency"), channel=wireless.get("channel"),
                            tx_power=wireless.get("txpower"))
                seen["peers"] = len(self.api.call("iwinfo", "assoclist", {
                    "device": self.api.device}).get("results") or [])
        except RadioError as error:
            seen["error"] = str(error)
        return seen

    def run(self, command, *params, timeout=EXEC_TIMEOUT):
        """`file exec` of one program, never through a shell. Exit status and stderr come back
        with stdout, so an empty answer is never mistaken for "found nothing"."""
        try:
            reply = self.api.call("file", "exec", {"command": command, "params": list(params)},
                                  timeout=timeout)
        except RadioError as error:
            if "timed out" not in str(error):
                raise
            raise RadioError(f"no answer within {timeout:g} s: probably a slow command on a radio "
                             f"that is otherwise fine ({error})") from None
        return {"code": reply.get("code"), "stdout": redact(str(reply.get("stdout") or "")),
                "stderr": redact(str(reply.get("stderr") or ""))[:STDERR_CHARS]}

    def can_run(self, command, *params):
        """allowed, or why not: a radio can refuse `file exec` whatever its ACL grants."""
        try:
            result = self.run(command, *params, timeout=10)
        except UbusError as error:
            return f"{'refused' if error.status == PERMISSION_DENIED else 'failed'}: {error}"
        except RadioError as error:
            return f"failed: {error}"
        if result["code"] != 0:
            return f"failed: exit {result['code']}: {result['stderr'] or 'no error text'}"
        return "allowed"

    def ping(self, host):
        """Data through the mesh, not just a peer entry: True or False, or None when this radio
        will not run ping. On a two-wire bench the radios' wired ports are on separate segments
        (docs/RADIO_BENCH.md), so a ping between them can only cross the air."""
        if self.ping_refused:
            return None
        for command in PING:
            try:
                return self.run(command, "-c", "1", "-W", "1", host, timeout=5)["code"] == 0
            except UbusError as error:
                if error.status == NOT_FOUND:
                    continue
                if error.status == PERMISSION_DENIED:
                    break
                return False
            except RadioError:
                return False
        self.ping_refused = True
        return None

    def read_bytes(self, path):
        data = self.api.call("file", "read", {"path": path, "base64": True}).get("data")
        if not isinstance(data, str):
            raise ReplyError(f"file.read {path}: malformed reply")
        return base64.b64decode(data)

    def switch(self, arguments):
        self.api.call("message-system", "chswitch", arguments)

    def report(self):
        return self.api.read_json(LINKSTATE)

    def frequencies(self):
        results = self.api.call("iwinfo", "freqlist", {"device": self.api.device}).get("results")
        return sorted({r["mhz"] for r in results or [] if isinstance(r, dict) and "mhz" in r})

    def saved(self):
        """What would survive a power cycle, as far as the API shows without reading the key."""
        evidence = {}
        try:
            stat = self.api.call("file", "stat", {"path": SAVED_WIRELESS})
            evidence.update(config_mtime=stat.get("mtime"), config_size=stat.get("size"))
            section = {"config": "wireless", "section": self.api.uci_radio}
            values = self.api.call("uci", "get", section).get("values") or {}
            evidence.update(channel=values.get("channel"), chanbw=values.get("chanbw"))
            changes = self.api.call("uci", "changes", {"config": "wireless"}).get("changes")
            evidence["uncommitted"] = bool(changes)
        except RadioError as error:
            evidence["error"] = str(error)
        return evidence


class Bench:
    """Both radios, and how this machine reaches them. Unless `separate`, radio b is reached only
    through radio a's mesh link, so nothing here may cut b off: its RF is never toggled, and a
    radio b left on another channel is fetched by sending radio a there."""

    def __init__(self, a, b, log, *, to=None, home=None, timeout=90.0, separate=False,
                 ask=input, clock=time.monotonic, sleep=time.sleep):
        self.a, self.b, self.nodes = a, b, (a, b)
        self.log, self.to, self.home, self.timeout = log, to, home, timeout
        self.separate, self.wired = separate, (a, b) if separate else (a,)
        self.ask, self.clock, self.sleep = ask, clock, sleep

    def look(self, node):
        seen = node.observe()
        self.log.sample("radio", radio=node.name, **seen)
        return seen

    def until(self, check, timeout):
        """-> seconds until check() held, or None."""
        started = self.clock()
        while True:
            if check():
                return round(self.clock() - started, 3)
            if self.clock() - started >= timeout:
                return None
            self.sleep(POLL)

    def on(self, node, frequency):
        return self.look(node)["frequency"] == frequency

    def rf(self, node, up):
        """Toggle RF and wait for it. -> seconds, or None."""
        node.api.set_rf(up)
        return self.until(lambda: self.look(node)["rf"] is up, RF_TIMEOUT)

    def crosses(self):
        """Does data cross the mesh? -> True, False, or None when it cannot be told. Radio a pings
        radio b; when radios will not run ping, radio b's API answering is the proof, but only
        when b is reached through the mesh (one wire)."""
        result = self.a.ping(self.b.host)
        if result is None and not self.separate:
            return self.reachable(self.b)
        return result

    # --- a switch and what follows ---------------------------------------------------------------

    def watch(self, frequency):
        """Both radios on `frequency` with data crossing the mesh. -> seconds to each event; one
        that never happened is absent, not zero. Peer entries are recorded but decide nothing:
        `iwinfo assoclist` has not been seen with a peer yet, and data is what counts."""
        started, events = self.clock(), {}
        while True:
            elapsed, settled = round(self.clock() - started, 3), True
            for node in self.nodes:
                seen = self.look(node)
                if seen["frequency"] != frequency:
                    settled = False
                    continue
                events.setdefault(f"{node.name}_on_channel", elapsed)
                if seen["peers"]:
                    events.setdefault(f"{node.name}_peer", elapsed)
            if settled and self.crosses():
                events["data"] = round(self.clock() - started, 3)
                return events
            if self.clock() - started >= self.timeout:
                return events
            self.sleep(POLL)

    def move(self, sender, target):
        self.log.sample("switch", sender=sender.name, arguments=target)
        try:
            sender.switch(target)
        except ReplyError as error:
            return {"rejected": str(error)}
        except RadioError as error:  # only the answer was lost: the switch may well be happening
            self.log.sample("switch_unconfirmed", error=str(error))
        return self.watch(target["frequency"])

    # --- safety ----------------------------------------------------------------------------------

    def preflight(self):
        """-> (starting power by radio, problems, warnings). Only these two radios, together, at
        home, pass: anything else is a problem and nothing moves."""
        problems, warnings, power = [], [], {}
        home, target = self.home["frequency"], self.to["frequency"]
        if home == target:
            problems.append("--to and --home are the same frequency")
        for node in self.nodes:
            seen = self.look(node)
            power[node.name] = seen["tx_power"]
            if seen["rf"] is not True or "error" in seen:
                problems.append(f"radio {node.name} is not up: {seen.get('error', 'RF off')}")
                continue
            if seen["frequency"] != home:
                problems.append(f"radio {node.name} is on {seen['frequency']} MHz, not home {home}")
            report = node.report()
            peers = peer_count(report)
            if peers == 0:
                problems.append(f"radio {node.name} hears no peer")
            elif peers > 1:
                problems.append(f"radio {node.name} hears {peers} peers: `chswitch` is mesh-wide "
                                "and would move them too")
            for entry in report.get("sta_stats") or []:
                signal_dbm = link_quality("link", entry).get("link.signal_dbm")
                if signal_dbm is not None and signal_dbm >= TOO_STRONG_DBM:
                    warnings.append(f"radio {node.name} hears its peer at {signal_dbm} dBm: too "
                                    "strong, add attenuation or separation")
            supported = node.frequencies()
            if supported and target not in supported:
                problems.append(f"radio {node.name} does not list {target} MHz")
        if not problems:
            crosses = self.crosses()
            if crosses is None:
                problems.append("cannot tell whether data crosses the mesh: radio a will not run "
                                "ping, and radio b has its own wired path")
            elif not crosses:
                problems.append(f"no data from radio a to {self.b.host} across the mesh")
        return power, problems, warnings

    def confirm(self, stage, changes):
        wiring = ("each radio has its own wired path, and there is no shared switch"
                  if self.separate else
                  "only radio a is wired to this machine: b is reached through the mesh, and "
                  "there is no shared switch")
        print(f"\n{stage} will change real radios:", *(f"  * {line}" for line in changes),
              "Agree only if:", "  - nothing is flying, and these two radios are the whole mesh",
              "  - you are authorized to transmit on every frequency involved", f"  - {wiring}",
              "  - the doodle service is stopped on both radios", sep="\n")
        return self.ask("Type yes to continue: ").strip().lower() == "yes"

    def reachable(self, node):
        return "error" not in self.look(node)

    def controller(self, node, directory):
        """The supervisor's own Controller on this radio, with a throwaway target file."""
        stop = threading.Event()
        stop.wait = lambda seconds: (self.sleep(seconds), stop.is_set())[1]
        return supervisor.Controller(node.api, doodle_config.tx_power({}),
                                     Path(directory) / "target.json", clock=self.clock,
                                     stop_event=stop, sleep=self.sleep)

    def restore(self, power):
        """Both radios home at their starting power, by the least invasive way that works. RF is
        toggled only on a radio with a wired path of its own: through the mesh it would strand it.
        With no --home (a `check` without a channel switch) only the power is put back."""
        steps = []

        def attempt(what, action):
            try:
                action()
                steps.append(what)
            except RadioError as error:
                steps.append(f"{what} failed: {error}")

        if self.home is not None:
            home = self.home["frequency"]
            for node in self.wired:
                if self.look(node)["rf"] is False:
                    attempt(f"{node.name}: RF on", lambda: self.rf(node, True))
            for node in self.nodes:  # `chswitch` first: it brings the mesh back together
                seen = self.look(node)
                if seen["frequency"] != home and "error" not in seen:
                    attempt(f"{node.name}: chswitch home", lambda: node.switch(self.home))
                    self.until(lambda: self.on(node, home), SETTLE)
            if not self.separate and not self.reachable(self.b):
                self.rendezvous(attempt)
            for node in self.wired:  # an RF toggle reloads the saved config
                if not self.on(node, home):
                    def toggle():
                        self.rf(node, False)
                        self.rf(node, True)
                    attempt(f"{node.name}: RF toggle", toggle)
                    self.until(lambda: self.on(node, home), RF_TIMEOUT)
        for node in self.nodes:
            wanted = power.get(node.name)
            if isinstance(wanted, int) and self.look(node)["tx_power"] != wanted:
                attempt(f"{node.name}: tx power {wanted}", lambda: node.api.set_txpower(wanted))
        state = {node.name: self.look(node) for node in self.nodes}
        at_home = self.home is None or all(seen["frequency"] == self.home["frequency"]
                                           for seen in state.values())
        return {"ok": at_home and all(seen["tx_power"] == power.get(name)
                                      for name, seen in state.items()),
                "at_home": at_home, "steps": steps, "state": state}

    def rendezvous(self, attempt):
        """Radio b is reached only through the mesh, and the mesh is split: radio a goes to the
        channel b was sent to, then brings both home from there."""
        if self.to is None:
            return
        home = self.home["frequency"]
        attempt(f"a: chswitch {self.to['frequency']} to find b", lambda: self.a.switch(self.to))
        found = self.until(lambda: self.reachable(self.b), self.timeout) is not None
        attempt("a: chswitch home" + ("" if found else " without b"),
                lambda: self.a.switch(self.home))
        self.until(lambda: self.on(self.a, home) and (not found or self.on(self.b, home)),
                   SETTLE)


# --- stages --------------------------------------------------------------------------------------

def discover(bench, _args):
    """How the firmware calls `chswitch`, plus everything needed to build --to and --home."""
    radios = {}
    for node in bench.nodes:
        found = {"observed": bench.look(node)}

        def read(name, action):
            try:
                found[name] = action()
            except RadioError as error:
                found[name] = {"error": str(error)}

        read("board", node.api.board)
        read("frequencies_mhz", node.frequencies)
        read("saved", node.saved)
        read("link_report", lambda: {k: v for k, v in node.report().items()
                                     if k not in ("sta_stats", "mesh_stats", "sysinfo")})
        for config in VENDOR_CONFIGS:
            read(f"config/{config}",
                 lambda: redact(node.api.call("uci", "get", {"config": config}).get("values")))
        found["acls"] = node.api.acls  # what the login grants; a radio may still refuse more
        ping = node.ping("127.0.0.1")
        found["exec"] = {"iw (how the service sets TX power)":
                         node.can_run(IW, "dev", node.api.device, "info"),
                         "ping (the bench's data check)":
                         {True: "allowed", False: "failed", None: "refused or missing"}[ping]}
        radios[node.name] = found
    boards = [found["board"].get("firmware") for found in radios.values()]
    same = len(set(boards)) == 1
    results = {}
    for node in bench.nodes[:1] if same else bench.nodes:  # one firmware: one radio's files do
        print(f"searching radio {node.name}'s firmware for chswitch callers, at most "
              f"{SEARCH_BUDGET:g} s...", file=sys.stderr, flush=True)
        results[node.name] = radios[node.name]["search"] = search(node, bench.clock)
    return {"ok": all(result["read"] for result in results.values()), "radios": radios,
            "findings": {
                "firmware": boards, "same_firmware": same,
                "exec": {name: found["exec"] for name, found in radios.items()},
                "callers": {name: verdict(result) for name, result in results.items()},
                "strings_from": {name: sorted(result["strings"])
                                 for name, result in results.items()}}}


def search(node, clock, budget=None):
    """chswitch callers through `file list` and `file read`, which need read access only. Text is
    matched line by line, a binary by its printable strings (what `strings` would show)."""
    deadline = clock() + (SEARCH_BUDGET if budget is None else budget)
    result = {"hits": [], "strings": {}, "read": 0, "skipped": 0, "failed": [],
              "not_reached": []}
    queue = list(SEARCH_PLACES)
    while queue:
        directory = queue.pop(0)
        if clock() > deadline:
            result["not_reached"] = [directory, *queue]
            break
        try:
            entries = node.api.call("file", "list", {"path": directory}).get("entries") or []
        except UbusError as error:
            if error.status != NOT_FOUND:  # a directory this firmware lacks is not a gap
                result["failed"].append(f"{directory}: {error}")
            continue
        except RadioError as error:
            result["failed"].append(f"{directory}: {error}")
            continue
        for index, entry in enumerate(entries):
            if clock() > deadline:
                result["not_reached"] = [f"{directory} (after {index} entries)", *queue]
                return result
            path = f"{directory.rstrip('/')}/{entry.get('name')}"
            if entry.get("type") == "directory":
                queue.append(path)
                continue
            if entry.get("type") != "file" or (entry.get("size") or 0) > MAX_READ_BYTES:
                result["skipped"] += 1
                continue
            try:
                data = node.read_bytes(path)
            except RadioError as error:
                result["failed"].append(f"{path}: {error}")
                continue
            result["read"] += 1
            texts = PRINTABLE.findall(data) if b"\0" in data else data.splitlines()
            for number, text in enumerate(texts, 1):
                if CALLER_PATTERN.search(text) and len(result["hits"]) < MAX_HITS:
                    line = text.decode(errors="replace").strip()[:240]
                    result["hits"].append(redact(f"{path}:{number}: {line}"))
            if path in STRINGS_OF:
                result["strings"][path] = redact("\n".join(
                    text.decode(errors="replace") for text in PRINTABLE.findall(data)
                    if STRINGS_PATTERN.search(text))[:4000])
    return result


def verdict(result):
    """found, none, or what kept the search from being complete: a short answer hides no gap."""
    files = sorted({hit.split(":", 1)[0] for hit in result["hits"]})
    if files:
        head = "found in " + ", ".join(files[:6]) + (" and more" if len(files) > 6 else "")
    elif result["read"]:
        head = f"none in {result['read']} files"
    else:
        head = "failed: no file could be read"
    gaps = []
    if result["not_reached"]:
        gaps.append("ran out of time before " + ", ".join(result["not_reached"][:3]))
    if result["failed"]:
        gaps.append(f"{len(result['failed'])} unreadable, e.g. {result['failed'][0]}")
    if result["skipped"]:
        gaps.append(f"{result['skipped']} skipped: links, devices, files over "
                    f"{MAX_READ_BYTES // 1024} KB")
    return head + (f" ({'; '.join(gaps)})" if gaps else "")


def capture(bench, args):
    """The per-peer format and whether the service's mapping survives it. Read-only."""
    entries = {"link": {}, "mesh": {}}
    extras = {node.name: {} for node in bench.nodes}
    too_strong = set()
    for node in bench.nodes:
        extras[node.name]["survey_start"] = survey(node, bench)
    for _ in range(max(1, int(args.seconds / SAMPLE))):
        for node in bench.nodes:
            try:
                report = node.report()
                stations = node.api.call("iwinfo", "assoclist", {"device": node.api.device})
            except RadioError as error:
                bench.log.sample("error", radio=node.name, error=str(error))
                continue
            bench.log.sample("linkstate", radio=node.name, report=report, assoclist=stations)
            for kind, field in (("link", "sta_stats"), ("mesh", "mesh_stats")):
                for entry in report.get(field) or []:
                    entries[kind].setdefault(node.name, []).append((entry, report.get("noise")))
                    signal_dbm = link_quality(kind, entry).get("link.signal_dbm")
                    if signal_dbm is not None and signal_dbm >= TOO_STRONG_DBM:
                        too_strong.add(node.name)
        bench.sleep(SAMPLE)
    for node in bench.nodes:
        extras[node.name]["survey_end"] = survey(node, bench)
        extras[node.name].update(one_shots(node))
    peers = {kind: {node.name: inventory(entries[kind].get(node.name, []), kind)
                    for node in bench.nodes} for kind in entries}
    heard = all(peers["link"][node.name]["entries"] or peers["mesh"][node.name]["entries"]
                for node in bench.nodes)
    return {"ok": heard, "peers": peers, "extras": extras, "findings": {
        "peers_seen_on_both": heard, "too_strong": sorted(too_strong),
        "not_published": {kind: {name: result["not_published"]
                                 for name, result in peers[kind].items()} for kind in peers}}}


def one_shots(node):
    """Per-peer details the link report leaves out, read without a shell."""
    extras = {}

    def attempt(name, action):
        try:
            extras[name] = action()
        except RadioError as error:
            extras[name] = f"failed: {error}"

    def command(*argv):
        result = node.run(*argv)
        return result["stdout"] or f"exit {result['code']}: {result['stderr'] or 'no output'}"

    attempt("station_dump", lambda: command(IW, "dev", node.api.device, "station", "dump"))
    attempt("batman_originators", lambda: command(BATCTL, "o"))
    attempt("peer_debugfs", lambda: peer_debugfs(node))
    return extras


def peer_debugfs(node):
    """ath9k_tpcd's per-peer files: how each side hears the other, and any power limit on it."""
    phy = node.api.wireless().get("phy")
    stations = f"/sys/kernel/debug/ieee80211/{phy}/netdev:{node.api.device}/stations"
    found = {}
    for station in node.api.call("file", "list", {"path": stations}).get("entries") or []:
        for name in ("peer_signal", "peer_signal_reply", "fixed_txpower"):
            path = f"{stations}/{station.get('name')}/{name}"
            try:
                found[path] = node.api.read_text(path).strip()
            except RadioError as error:
                found[path] = f"failed: {error}"
    return found


def survey(node, bench):
    """The operating channel's counters: do they reset, and on what?"""
    frequency = bench.look(node)["frequency"]
    try:
        results = node.api.call("iwinfo", "survey", {"device": node.api.device}).get("results")
    except RadioError as error:
        return {"error": str(error)}
    return next((r for r in results or [] if r.get("mhz") == frequency), None)


def hop(bench, args):
    """Timing both ways with either radio as sender, and whether a switch sticks anywhere."""
    nodes, rounds, during, power_changed = bench.nodes, [], {}, {}
    power = {node.name: bench.look(node)["tx_power"] for node in nodes}
    before = {node.name: node.saved() for node in nodes}

    def leg(sender, target):
        events = bench.move(sender, target)
        rounds.append({"to": target["frequency"], "sender": sender.name, "events": events})
        return "data" in events

    for index in range(args.repeats):
        if not leg(nodes[index % 2], bench.to):
            break
        bench.sleep(args.dwell)
        during = {node.name: node.saved() for node in nodes}
        for node in nodes:  # the supervisor must re-apply its target if a switch resets the power
            power_now = bench.look(node)["tx_power"]
            if power_now != power[node.name]:
                power_changed[node.name] = {"from": power[node.name], "to": power_now}
        if not leg(nodes[(index + 1) % 2], bench.home):
            break
    after = {node.name: node.saved() for node in nodes}
    completed = sum("data" in one["events"] for one in rounds)
    wanted = args.repeats * 2
    return {"ok": completed == wanted, "rounds": rounds, "saved": {
        "before": before, "away": during, "after": after}, "findings": {
        "switches_completed": f"{completed}/{wanted}", "timing": timing(rounds),
        "tx_power_changed": power_changed,
        "saved_config_rewritten": {name: before[name].get("config_mtime")
                                   != after[name].get("config_mtime") for name in before},
        "saved_channel_changed_while_away": {name: before[name].get("channel")
                                             != during[name].get("channel") for name in during}}}


def missed(bench, args):
    """One radio misses the switch. Does the other come home, and how long is the mesh split?"""
    a, b = bench.a, bench.b
    if bench.rf(b, False) is None:
        return {"ok": False, "findings": {"error": "radio b did not turn RF off"}}
    bench.log.sample("switch", sender="a", arguments=bench.to)
    a.switch(bench.to)
    moved = bench.until(lambda: bench.on(a, bench.to["frequency"]), bench.timeout)
    back_on = bench.rf(b, True)
    landed = bench.look(b)["frequency"]
    # With b's copy of the message gone, watch for the firmware's own link recovery.
    rejoined = None
    started = bench.clock()
    while bench.clock() - started < args.watch:
        seen_a, seen_b = bench.look(a), bench.look(b)
        if seen_a["frequency"] == seen_b["frequency"] and bench.crosses():
            rejoined = {"after_s": round(bench.clock() - started, 3),
                        "frequency": seen_a["frequency"]}
            break
        bench.sleep(POLL)
    return {"ok": moved is not None, "findings": {
        "a_moved_without_b_after_s": moved, "b_rf_back_after_s": back_on, "b_landed_on": landed,
        "rejoined_by_itself": rejoined, "a_ended_on": bench.look(a)["frequency"],
        "watched_s": args.watch}}


def recover(bench, args):
    """Do the saved settings really win? The flight's safety case rests on this answer."""
    a, home, findings = bench.a, bench.home["frequency"], {}
    if "data" not in bench.move(a, bench.to):
        return {"ok": False, "findings": {"error": "the first switch did not complete"}}
    for node in bench.nodes:
        bench.rf(node, False)
        bench.rf(node, True)
        findings[f"rf_toggle_returns_{node.name}_home"] = bench.on(node, home)
    findings["link_after_toggles_s"] = bench.watch(home).get("data")
    if not args.skip_reboot:
        if "data" not in bench.move(a, bench.to):
            findings["error"] = "the second switch did not complete"
            return {"ok": False, "findings": findings}
        try:
            a.api.call("system", "reboot")
        except RadioError as error:  # the reply can lose the race with the reboot
            bench.log.sample("reboot", error=str(error))
        went_down = bench.until(lambda: bench.look(a)["rf"] is not True, RF_TIMEOUT)
        back = bench.until(lambda: bench.look(a)["frequency"] is not None, REBOOT_TIMEOUT)
        landed = bench.look(a)["frequency"]
        findings.update(reboot_seen=went_down is not None, reboot_back_after_s=back,
                        reboot_landed_on=landed, reboot_returns_home=landed == home)
    ok = all(value for key, value in findings.items() if key.endswith("_home"))
    return {"ok": ok, "findings": findings}


# --- check: is everything working? ---------------------------------------------------------------

class Checklist:
    """PASS, WARN, FAIL, SKIP and NOTE lines: printed as they happen, kept for report.json."""

    def __init__(self):
        self.items, self.blocking = [], []

    def add(self, status, name, detail="", *, blocks=True):
        """A FAIL blocks changes unless `blocks` is false: a finding about one capability (the
        service cannot run iw) need not stop a test of another (chswitch)."""
        self.items.append({"status": status, "check": name, "detail": detail})
        if status == "FAIL" and blocks:
            self.blocking.append(name)
        print(f"{status:<4}  {name}" + (f": {detail}" if detail else ""), flush=True)

    def failed(self):
        return any(item["status"] == "FAIL" for item in self.items)


def check(bench, args):
    """Is everything working, from this machine? Read-only checks first. Changes come only after
    they all pass and you agree, and both radios are put back however it ends."""
    items = Checklist()
    if (bench.to is None) != (bench.home is None):
        items.add("FAIL", "arguments", "pass both --to and --home, or neither")
        return {"ok": False, "checks": items.items}
    items.add("NOTE", "wiring", "each radio has its own wired path" if bench.separate else
              "only radio a is wired here: radio b is reached through the mesh")
    seen = read_checks(bench, items)
    if args.read_only or items.blocking or len(seen) < 2:
        why = "--read-only" if args.read_only else "a check above failed"
        items.add("SKIP", "power and channel changes", why)
        return {"ok": not items.failed(), "checks": items.items}
    changes = ["radio a's transmit power moves one supported step and back"]
    if bench.to is not None:
        changes.append(f"both radios switch to {bench.to['frequency']} MHz and back, with one "
                       "`message-system chswitch` request each way")
    if not (args.yes or bench.confirm("check", changes)):
        items.add("SKIP", "power and channel changes", "not confirmed")
        return {"ok": not items.failed(), "checks": items.items}
    power = {name: view["tx_power"] for name, view in seen.items()}
    try:
        power_round_trip(bench, items)
        if bench.to is not None:
            channel_round_trip(bench, items)
        else:
            items.add("SKIP", "mesh-wide channel switch",
                      "pass --to and --home, built from what `discover` shows")
    except (Exception, KeyboardInterrupt) as error:  # whatever happened, the radios go home
        items.add("FAIL", "changes", f"{type(error).__name__}: {error}")
    restored = bench.restore(power)
    items.add("PASS" if restored["ok"] else "FAIL", "both radios back where they started",
              "; ".join(restored["steps"]))
    return {"ok": not items.failed(), "checks": items.items, "restored": restored}


def read_checks(bench, items):
    """-> {radio: how it looks now}, for each radio that is reachable with RF on."""
    seen, firmware, reports = {}, {}, {}
    for node in bench.nodes:
        try:
            missing = node.api.missing_permissions("observe", "set_power", "rf_toggle")
            board = node.api.board()
        except RadioError as error:
            hint = ("" if bench.separate or node is bench.a else
                    " (radio b is reached through the mesh: is the link up?)")
            items.add("FAIL", f"{node.name}: reach and log in", f"{error}{hint}")
            continue
        items.add("FAIL" if missing else "PASS", f"{node.name}: API login and permissions",
                  ", ".join(missing))
        firmware[node.name] = board.get("firmware") or ""
        match = FIRMWARE.search(firmware[node.name])
        new_enough = bool(match) and (int(match[1]), int(match[2])) >= OLDEST_FIRMWARE
        items.add("PASS" if new_enough else "FAIL", f"{node.name}: firmware 2025-06 or later",
                  firmware[node.name] or "unknown")
        view = bench.look(node)
        if view["rf"] is not True:
            items.add("FAIL", f"{node.name}: RF on", view.get("error", "RF off"))
            continue
        seen[node.name] = view
        try:
            reports[node.name] = node.report()
        except RadioError as error:
            items.add("FAIL", f"{node.name}: link report", str(error))
    if len(seen) < 2:
        return seen
    if len(set(firmware.values())) > 1:
        items.add("WARN", "same firmware", ", ".join(f"{k} {v}" for k, v in firmware.items()))
    widths = {name: str(report.get("chan_width")) for name, report in reports.items()}
    same = seen["a"]["frequency"] == seen["b"]["frequency"] and len(set(widths.values())) < 2
    items.add("PASS" if same else "FAIL", "same channel", ", ".join(
        f"{name} {view['frequency']} MHz, {widths.get(name)} MHz wide"
        for name, view in seen.items()))
    for name, report in reports.items():
        link_checks(name, report, items)
    for source, target in ((bench.a, bench.b), (bench.b, bench.a)):
        name = f"data {source.name} -> {target.name} across the mesh"
        result = source.ping(target.host)
        if result is None and not bench.separate:
            items.add("PASS", name, "radio b's API answers through the mesh (these radios will "
                      "not run ping)")
        elif result is None:
            items.add("WARN", name, "cannot tell: the radio will not run ping, and radio b has "
                      "its own wired path")
        else:
            items.add("PASS" if result else "FAIL", name)
    for node in bench.nodes:
        iw = node.can_run(IW, "dev", node.api.device, "info")
        items.add("PASS" if iw == "allowed" else "FAIL",
                  f"{node.name}: the service can run iw (how it sets transmit power)",
                  "" if iw == "allowed" else iw, blocks=False)
        service_checks(bench, node, seen[node.name], items)
    return seen


def link_checks(name, report, items):
    peers = peer_count(report)
    items.add("PASS" if peers == 1 else "FAIL" if peers == 0 else "WARN", f"{name}: peers",
              f"hears {peers}; expected the other radio only")
    for entry in report.get("sta_stats") or []:
        quality = link_quality("link", entry, report.get("noise"))
        signal_dbm, snr = quality.get("link.signal_dbm"), quality.get("link.snr_db")
        detail = f"{signal_dbm} dBm" + (f", SNR {snr} dB" if snr is not None else "")
        if signal_dbm is None:
            status, detail = "WARN", "no signal the service can map from " + ", ".join(entry)
        elif signal_dbm >= TOO_STRONG_DBM:
            status, detail = "WARN", detail + " (too strong: may saturate the receiver)"
        elif signal_dbm < WEAK_DBM or (snr is not None and snr < LOW_SNR_DB):
            status, detail = "WARN", detail + " (weak)"
        else:
            status = "PASS"
        items.add(status, f"{name}: signal from {peer_address(entry) or 'a peer'}", detail)
    for kind, field in (("link", "sta_stats"), ("mesh", "mesh_stats")):
        entries = [(entry, report.get("noise")) for entry in report.get(field) or []]
        if entries:
            missing = inventory(entries, kind)["not_published"]
            items.add("WARN" if missing else "PASS", f"{name}: {kind} keys the dashboard reads",
                      "missing " + ", ".join(missing) if missing else ", ".join(EXPECTED[kind]))


def service_checks(bench, node, view, items):
    """The service's own read path against this radio: the supervisor's observation and the
    diagnostics it would publish, without ROS (docs/PLAN.md, hardware checks for Phase 3)."""
    try:
        with tempfile.TemporaryDirectory() as directory:
            controller = bench.controller(node, directory)
            snapshot = controller.status()
        reporter = HealthReporter(node.name, clock=bench.clock)
        reporter.poll(node.api)
        rows = reporter.snapshot(controller.health_state())
    except Exception as error:  # the service's code failing against real replies is the finding
        items.add("FAIL", f"{node.name}: service code", f"{type(error).__name__}: {error}")
        return
    items.add("FAIL" if snapshot.get("error") else "PASS", f"{node.name}: supervisor read path",
              snapshot.get("error") or f"RF {snapshot['rf']}, {snapshot['tx_power_dbm']} dBm, "
              f"supported {snapshot['range_dbm']}")
    components = {row["name"].split(": ", 1)[-1]: row for row in rows}
    not_ok = [f"{name} {LEVEL_NAMES[row['level']]}: {row['message']}"
              for name, row in components.items() if row["level"] != OK]
    worst = max((row["level"] for row in components.values()), default=STALE)
    items.add("FAIL" if worst in (ERROR, STALE) else "WARN" if not_ok else "PASS",
              f"{node.name}: diagnostics the service would publish",
              "; ".join(not_ok) or ", ".join(sorted(components)))
    radio = components.get("radio", {}).get("values", {})
    links = components.get("links", {}).get("values", {})

    def measured_or_unsupported(key):
        value = radio.get(key)
        return isinstance(value, (int, float)) or \
            radio.get(f"health.metric.{key}.state") == "unsupported"

    expectations = {
        "rf.tx_power_dbm matches": radio.get("rf.tx_power_dbm") == view["tx_power"],
        "rf.noise_dbm": isinstance(radio.get("rf.noise_dbm"), (int, float)),
        "rf.ack_power_dbm": measured_or_unsupported("rf.ack_power_dbm"),
        "rf.tx_airtime_pct": measured_or_unsupported("rf.tx_airtime_pct"),
        "links.stations": (links.get("links.stations") or 0) >= 1,
        "link.signal_dbm": any("link.signal_dbm" in row["values"]
                               for name, row in components.items() if name.startswith("link/"))}
    wrong = [key for key, ok in expectations.items() if not ok]
    items.add("WARN" if wrong else "PASS", f"{node.name}: values the dashboard depends on",
              "not as expected: " + ", ".join(wrong) if wrong else ", ".join(expectations))


def power_round_trip(bench, items):
    """The supervisor's write path on radio a, which is always wired: one supported step down
    (or up), read back, the link checked, then back. The same calls as `doodle-up … tx-power`."""
    a = bench.a
    iw = a.can_run(IW, "dev", a.api.device, "info")
    if iw != "allowed":
        items.add("SKIP", "a: transmit power change", f"radio a will not run iw: {iw}")
        return
    with tempfile.TemporaryDirectory() as directory:
        controller = bench.controller(a, directory)
        before = controller.status()["tx_power_dbm"]
        supported = a.api.txpower_list()
        if not isinstance(before, int) or len(supported) < 2:
            items.add("SKIP", "a: transmit power change", f"at {before} dBm of {supported}")
            return
        lower = [dbm for dbm in supported if dbm < before]
        step = lower[-1] if lower else min(dbm for dbm in supported if dbm > before)
        changed = controller.request(step)
        applied = changed["converged"] and changed["tx_power_dbm"] == step
        items.add("PASS" if applied else "FAIL", "a: transmit power change",
                  f"{before} -> {changed['tx_power_dbm']} dBm, read back")
        crossed = bench.until(lambda: bench.crosses() is not False, 10)
        if bench.crosses() is None:
            items.add("WARN", f"link at {step} dBm", "cannot tell: radio a will not run ping, and "
                      "radio b has its own wired path")
        else:
            items.add("PASS" if crossed is not None else "WARN", f"link at {step} dBm",
                      f"data after {crossed} s" if crossed is not None else
                      "no data for 10 s: a live power change may interrupt the link")
        back = controller.request(before)
        items.add("PASS" if back["tx_power_dbm"] == before else "FAIL",
                  "a: transmit power restored", f"{back['tx_power_dbm']} dBm")


def channel_round_trip(bench, items):
    """One `chswitch` request to radio a should move both radios; one to radio b, both back."""
    _, problems, _ = bench.preflight()
    if problems:
        items.add("SKIP", "mesh-wide channel switch", "; ".join(problems))
        return
    for sender, target in ((bench.a, bench.to), (bench.b, bench.home)):
        events = bench.move(sender, target)
        name = f"one request to {sender.name} moves both radios to {target['frequency']} MHz"
        if "data" not in events:
            items.add("FAIL", name, json.dumps(events))
            return
        items.add("PASS", name, f"data after {events['data']} s")


STAGES = {"check": check, "discover": discover, "capture": capture, "hop": hop,
          "missed": missed, "recover": recover}
ACTIVE = ("hop", "missed", "recover")


def run(bench, args):
    """-> exit status: 0 done, 1 a finding or a refusal, 3 radios not restored."""
    stage = STAGES[args.stage]
    if args.stage == "check":
        report = stage(bench, args)
        bench.log.report(report)
        print(f"report: {bench.log.directory}")
        return outcome(report)
    if args.stage not in ACTIVE:
        report = stage(bench, args)
        bench.log.report(report)
        print(json.dumps(report["findings"], indent=2, default=str))
        print(f"report: {bench.log.directory}")
        return 0 if report["ok"] else 1
    if args.stage in NEEDS_WIRED_B and not bench.separate:
        problem = (f"{args.stage} turns radio b's RF off or reboots it, so it needs a wired path "
                   "of its own to radio b; here b is reached only through the mesh")
        bench.log.report({"ok": False, "refused": [problem]})
        print("refused, nothing was changed:", problem, sep="\n  ", file=sys.stderr)
        return 1
    power, problems, warnings = bench.preflight()
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if problems:
        bench.log.report({"ok": False, "refused": problems})
        print("refused, nothing was changed:", *problems, sep="\n  ", file=sys.stderr)
        return 1
    move = (f"both radios move between {bench.home['frequency']} and {bench.to['frequency']} MHz "
            "with `message-system chswitch`")
    if not (args.yes or bench.confirm(args.stage, [move])):
        print("cancelled, nothing was changed", file=sys.stderr)
        return 1
    try:
        report = stage(bench, args)
    except (Exception, KeyboardInterrupt) as error:  # whatever happened, the radios go home
        report = {"ok": False, "findings": {"error": f"{type(error).__name__}: {error}"}}
    report["warnings"], report["restored"] = warnings, bench.restore(power)
    bench.log.report(report)
    print(json.dumps(report["findings"], indent=2, default=str))
    print(f"report: {bench.log.directory}")
    return outcome(report)


def outcome(report):
    restored = report.get("restored")
    if restored is None or restored["ok"]:
        return 0 if report["ok"] else 1
    print(f"NOT RESTORED: {json.dumps(restored['state'], default=str)}\n"
          "See 'If a radio is stranded' in docs/RADIO_BENCH.md. A power cycle reloads the saved "
          "config.", file=sys.stderr)
    return 3


def parser():
    top = argparse.ArgumentParser(description="Two-radio bench; see docs/RADIO_BENCH.md")
    top.add_argument("--a", required=True, help="first radio, by its own wired path")
    top.add_argument("--b", required=True, help="second radio, by its own wired path")
    top.add_argument("--username", default="user")
    top.add_argument("--device", default="wlan0")
    top.add_argument("--uci-radio", default="radio0")
    top.add_argument("--out", default="var/bench", help="directory for runs (gitignored default)")
    stages = top.add_subparsers(dest="stage", required=True)
    stage = stages.add_parser("check", help="is everything working: read-only checks, then a "
                              "power change and, with --to/--home, one channel switch each way")
    stage.add_argument("--to", type=switch_args, help="chswitch JSON to the test channel")
    stage.add_argument("--home", type=switch_args, help="chswitch JSON back home")
    stage.add_argument("--read-only", action="store_true", help="stop after the read-only checks")
    stage.add_argument("--timeout", type=float, default=90, help="s to wait for one switch")
    stage.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    stages.add_parser("discover", help="read-only: firmware, frequencies, how chswitch is called")
    stage = stages.add_parser("capture", help="read-only: real per-peer data")
    stage.add_argument("--seconds", type=float, default=60)
    for name, text in (("hop", "switch timing and whether it sticks"),
                       ("missed", "one radio misses the switch"),
                       ("recover", "RF toggle and reboot against a switched channel")):
        stage = stages.add_parser(name, help=text)
        stage.add_argument("--to", type=switch_args, required=True, help="chswitch JSON")
        stage.add_argument("--home", type=switch_args, required=True, help="chswitch JSON")
        stage.add_argument("--timeout", type=float, default=90, help="s to wait for one switch")
        stage.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
        if name == "hop":
            stage.add_argument("--repeats", type=int, default=3)
            stage.add_argument("--dwell", type=float, default=10, help="s on the test channel")
        elif name == "missed":
            stage.add_argument("--watch", type=float, default=60,
                               help="s to watch for the radios rejoining by themselves")
        else:
            stage.add_argument("--skip-reboot", action="store_true")
    return top


def passwords(environ=os.environ, prompt=getpass.getpass):
    first = environ.get("DOODLE_BENCH_PASSWORD") or prompt("radio a password: ")
    second = environ.get("DOODLE_BENCH_PASSWORD_B")
    if second is None and "DOODLE_BENCH_PASSWORD" not in environ:
        second = prompt("radio b password (Enter if the same): ")
    return first, second or first


def interrupted(*_):
    raise KeyboardInterrupt


def main(argv=None):
    args = parser().parse_args(argv)
    signal.signal(signal.SIGTERM, interrupted)  # restore() runs on the way out
    first, second = passwords()
    a, b = [Node(name, RadioAPI(host, args.username, password, device=args.device,
                                uci_radio=args.uci_radio), host)
            for name, host, password in (("a", args.a, first), ("b", args.b, second))]
    paths = wired_interfaces((args.a, args.b))
    separate = separate_paths(paths[args.a], paths[args.b])
    print(f"wiring: a via {paths[args.a] or '?'}, b via {paths[args.b] or '?'}: "
          + ("separate paths" if separate else "radio b counts as reachable only through the mesh"))
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    bench = Bench(a, b, Log(Path(args.out) / f"{stamp}-{args.stage}"),
                  to=getattr(args, "to", None), home=getattr(args, "home", None),
                  timeout=getattr(args, "timeout", 90.0), separate=separate)
    try:
        return run(bench, args)
    except KeyboardInterrupt:  # only reachable by interrupting preflight or restore() itself
        print("interrupted: check that both radios are on the home channel", file=sys.stderr)
        return 130
    except RadioError as error:
        print(f"radio_bench: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
