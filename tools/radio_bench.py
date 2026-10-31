#!/usr/bin/env python3
"""Two-radio bench: the peer data and channel-switch behavior the service has never seen.

  radio_bench.py --a HOST --b HOST discover | capture | hop | missed | recover

Read docs/RADIO_BENCH.md first; this is for two radios on a bench, never a flying vehicle.

- `discover` and `capture` only read.
- `hop`, `missed` and `recover` move real radios. They ask first, refuse a mesh with anyone else on
  it, and always finish by bringing both radios back to the home channel at their starting power.

`message-system chswitch` is vendor code with undocumented arguments, so they are never guessed:
`discover` shows how the firmware calls it, and --to/--home pass that JSON through unchanged.
This tool commits nothing; whether `chswitch` itself writes flash is one of the questions.
Passwords come from DOODLE_BENCH_PASSWORD (and _B if the radios differ) or a prompt, never argv.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import signal
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "docker" / "entrypoints"))
from health import link_quality, peer_address  # noqa: E402
from radio_api import LINKSTATE, RadioAPI, RadioError, ReplyError  # noqa: E402

POLL = 0.25               # s between looks while a switch settles
SAMPLE = 1.0              # s between link reports in `capture`
SETTLE = 20.0             # s a radio gets to obey a restoring `chswitch`
RF_TIMEOUT = 90.0         # s for RF to come back (docs/RADIO_API.md: observed < 40)
REBOOT_TIMEOUT = 240.0    # s for a radio to reboot and bring RF up
EXEC_TIMEOUT = 30.0       # s for one `file exec`
TOO_STRONG_DBM = -30      # Doodle's vehicle validation guide: may saturate the receiver
EXPECTED = {"link": ("link.signal_dbm", "link.snr_db", "link.loss_pct"),
            "mesh": ("mesh.quality_pct",)}
SAVED_WIRELESS = "/etc/config/wireless"  # only ever `file stat`: its contents include the mesh key
VENDOR_CONFIGS = ("message-system", "central_acs", "acs_multiband", "acs_manual_switch",
                  "link-recovery")
VENDOR_SHELL = {
    "signature": "ubus -v list message-system",
    "callers": "grep -rn -E 'chswitch|mode_change|channel_idx' /usr/sbin /usr/bin /usr/share "
               "/usr/lib/lua /etc/init.d /www 2>/dev/null | cut -c1-240 | head -200",
    "strings": "strings /usr/sbin/message-system | grep -i -E "
               "'chswitch|channel|bandwidth|mode_change|model' | head -80"}
ONE_SHOTS = {
    "station_dump": 'iw dev "$0" station dump',
    "peer_debugfs": 'for f in /sys/kernel/debug/ieee80211/*/netdev:"$0"/stations/*/'
                    'peer_signal* /sys/kernel/debug/ieee80211/*/netdev:"$0"/stations/*/'
                    'fixed_txpower; do echo "$f: $(cat "$f" 2>&1)"; done',
    "batman": "batctl n; batctl o"}
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

    def shell(self, command, *arguments, timeout=EXEC_TIMEOUT):
        """Arguments arrive as $0, $1…, never inside the command text."""
        reply = self.api.call("file", "exec", {"command": "/bin/sh",
                                               "params": ["-c", command, *arguments]},
                              timeout=timeout)
        return {"code": reply.get("code"), "stdout": redact(str(reply.get("stdout") or ""))}

    def ping(self, host):
        """Data through the mesh, not just a peer entry. The radios' wired ports are on separate
        segments on the bench (docs/RADIO_BENCH.md), so this can only cross the air."""
        try:
            return self.shell('ping -c 1 -W 1 "$0"', host, timeout=5)["code"] == 0
        except RadioError:
            return False

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
    def __init__(self, a, b, log, *, to=None, home=None, timeout=90.0, ask=input,
                 clock=time.monotonic, sleep=time.sleep):
        self.a, self.b, self.nodes = a, b, (a, b)
        self.log, self.to, self.home, self.timeout = log, to, home, timeout
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
            if settled and self.a.ping(self.b.host):
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
        if not problems and not self.a.ping(self.b.host):
            problems.append(f"radio a cannot ping {self.b.host} across the mesh")
        return power, problems, warnings

    def confirm(self, stage):
        print(f"\n{stage}: moves both radios between {self.home['frequency']} and "
              f"{self.to['frequency']} MHz with `message-system chswitch`.\n"
              "  - nothing is flying, and these two radios are the whole mesh\n"
              "  - you are authorized to transmit on both frequencies\n"
              "  - each radio has its own wired path; no shared switch (docs/RADIO_BENCH.md)\n"
              "  - the doodle service is stopped on both radios")
        return self.ask("Type yes to continue: ").strip().lower() == "yes"

    def restore(self, power):
        """Both radios home at their starting power, by the least invasive way that works."""
        home, steps = self.home["frequency"], []

        def attempt(what, action):
            try:
                action()
                steps.append(what)
            except RadioError as error:
                steps.append(f"{what} failed: {error}")

        for node in self.nodes:
            if self.look(node)["rf"] is False:
                attempt(f"{node.name}: RF on", lambda: self.rf(node, True))
        for node in self.nodes:  # `chswitch` first: it brings the mesh back together
            if not self.on(node, home):
                attempt(f"{node.name}: chswitch home", lambda: node.switch(self.home))
                self.until(lambda: self.on(node, home), SETTLE)
        for node in self.nodes:  # an RF toggle reloads the saved config
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
        at_home = all(seen["frequency"] == home for seen in state.values())
        return {"ok": at_home and all(seen["tx_power"] == power.get(name)
                                      for name, seen in state.items()),
                "at_home": at_home, "steps": steps, "state": state}


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
        for name, command in VENDOR_SHELL.items():
            read(f"vendor/{name}", lambda: node.shell(command)["stdout"])
        radios[node.name] = found
    boards = [found["board"].get("firmware") for found in radios.values()]
    return {"ok": True, "radios": radios, "findings": {
        "firmware": boards, "same_firmware": len(set(boards)) == 1,
        "callers_found": {name: isinstance(found["vendor/callers"], str)
                          and bool(found["vendor/callers"]) for name, found in radios.items()}}}


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
        for name, command in ONE_SHOTS.items():
            try:
                extras[node.name][name] = node.shell(command, node.api.device)["stdout"]
            except RadioError as error:
                extras[node.name][name] = f"failed: {error}"
    peers = {kind: {node.name: inventory(entries[kind].get(node.name, []), kind)
                    for node in bench.nodes} for kind in entries}
    heard = all(peers["link"][node.name]["entries"] or peers["mesh"][node.name]["entries"]
                for node in bench.nodes)
    return {"ok": heard, "peers": peers, "extras": extras, "findings": {
        "peers_seen_on_both": heard, "too_strong": sorted(too_strong),
        "not_published": {kind: {name: result["not_published"]
                                 for name, result in peers[kind].items()} for kind in peers}}}


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
        if seen_a["frequency"] == seen_b["frequency"] and a.ping(b.host):
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


STAGES = {"discover": discover, "capture": capture, "hop": hop, "missed": missed,
          "recover": recover}
ACTIVE = ("hop", "missed", "recover")


def run(bench, args):
    """-> exit status: 0 done, 1 a finding or a refusal, 3 radios not restored."""
    stage = STAGES[args.stage]
    if args.stage not in ACTIVE:
        report = stage(bench, args)
        bench.log.report(report)
        print(json.dumps(report["findings"], indent=2, default=str))
        print(f"report: {bench.log.directory}")
        return 0 if report["ok"] else 1
    power, problems, warnings = bench.preflight()
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if problems:
        bench.log.report({"ok": False, "refused": problems})
        print("refused, nothing was changed:", *problems, sep="\n  ", file=sys.stderr)
        return 1
    if not (args.yes or bench.confirm(args.stage)):
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
    restored = report["restored"]
    if restored["ok"]:
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
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    bench = Bench(a, b, Log(Path(args.out) / f"{stamp}-{args.stage}"),
                  to=getattr(args, "to", None), home=getattr(args, "home", None),
                  timeout=getattr(args, "timeout", 90.0))
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
