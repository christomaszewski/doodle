"""The two-radio bench against two simulated radios. `chswitch` semantics here are invented: the
real ones are what the tool exists to find out. These tests pin its safety behavior instead:
read-only stages stay read-only, nothing moves past a refusal, and the radios always go home."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker/entrypoints"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
from fake_radio import FakeRadio, MESH_KEY  # noqa: E402
from radio_api import RadioAPI, RadioError  # noqa: E402
import radio_bench  # noqa: E402
from radio_bench import (Bench, Log, Node, inventory, passwords, redact, run,  # noqa: E402
                         switch_args)

HOME, TEST = 2450, 2480
TO = {"frequency": TEST, "bandwidth": "10", "count": 5}
BACK = {"frequency": HOME, "bandwidth": "10", "count": 5}
WIZARD_KEY = "FIXTURE-WIZARD-KEY"


class BenchRadio(FakeRadio):
    """A radio with a peer: a link exists while both are up on one frequency."""

    def __init__(self, mac):
        super().__init__()
        self.mac, self.peer, self.extra_peers = mac, None, []
        self.saved_frequency, self.config_mtime = HOME, 1000
        self.persists = False           # `chswitch` writes the saved config
        self.deaf = False               # `chswitch` is accepted and does nothing
        self.ignores_home = False       # ...only on the way back
        self.comes_home_after = None    # polls until a stranded radio returns by itself
        self.switch_resets_power = False
        self.down_polls = 0             # rebooting: this many requests fail first
        self.rssi = -61
        self.link_broken = False        # up on one frequency, but they never peer
        self.no_data_on = set()         # frequencies that peer but pass no traffic
        self.assoclist_empty = False    # mesh peers may not be listed there at all
        self.reached_through = None     # radio b on a one-wire bench: reachable only when linked
        self.misses, self.heard = set(), 0  # which of the peer's switches this radio fails to hear
        self.firmware = "firmware-2026-03.1"
        self.exec_replies = {}          # command fragment -> its `file exec` reply

    def linked(self):
        return (self.peer is not None and self.rf_up and self.peer.rf_up
                and self.frequency == self.peer.frequency and not self.link_broken)

    def _tune(self, frequency):
        self.frequency, self.channel = frequency, frequency - 2399
        if self.switch_resets_power:
            self.txpower = 32

    def __call__(self, payload, timeout=None):
        if self.down_polls:
            self.down_polls -= 1
            raise RadioError("radio unreachable at fake: rebooting")
        if self.reached_through is not None and not self.linked():
            raise RadioError("radio unreachable at fake: no route through the mesh")
        return super().__call__(payload, timeout)

    def _method(self, obj, method, args):
        if (obj, method) == ("message-system", "chswitch"):
            if self.deaf or (self.ignores_home and args["frequency"] == HOME):
                return {}
            hearing = self.linked()
            self._tune(args["frequency"])
            if hearing:
                self.peer.heard += 1
                if self.peer.heard not in self.peer.misses:
                    self.peer._tune(args["frequency"])
            if self.persists:
                self.saved_frequency = args["frequency"]
                self.config_mtime += 1
            return {}
        if obj == "iwinfo" and method == "assoclist":
            if self.frequency == TEST and not self.linked():
                self._drift_home()
            listed = self.linked() and not self.assoclist_empty
            return {"results": [{"mac": self.peer.mac}] if listed else []}
        if (obj, method) == ("system", "board"):
            board = super()._method(obj, method, args)
            board["release"]["version"] = self.firmware
            return board
        if obj == "iwinfo" and method == "freqlist":
            return {"results": [{"channel": f - 2399, "mhz": f} for f in range(2400, 2506)]}
        if (obj, method) == ("file", "stat"):
            return {"mtime": self.config_mtime, "size": 2048, "mode": 33188}
        if (obj, method) == ("uci", "changes"):
            return {"changes": {}}
        if (obj, method) == ("uci", "get"):
            if args.get("config") != "wireless":
                return {"values": {"settings": {"enable": "1", "mesh_key": MESH_KEY}}}
            return {"values": {"channel": str(self.saved_frequency - 2399), "chanbw": "10",
                               "dynamic_txpower": "1", "dynamic_txpower_aggr": "0"}}
        if (obj, method) == ("file", "exec") and args.get("command") == "/bin/sh":
            command = args["params"][1]
            if command.startswith("ping"):
                passes = self.linked() and self.frequency not in self.no_data_on
                return {"code": 0 if passes else 1}
            for fragment, reply in self.exec_replies.items():
                if fragment in command:
                    if isinstance(reply, Exception):
                        raise reply
                    return reply
            return {"code": 0, "stdout": "chswitch count model frequency\n"
                                         f"wizard -n mesh -k {WIZARD_KEY} -c 51\n"}
        if (obj, method) == ("system", "reboot"):
            self.frequency, self.channel = self.saved_frequency, self.saved_frequency - 2399
            self.txpower, self.down_polls = 32, 4
            self.sessions.clear()
            return {}
        if (obj, method) == ("network.wireless", "up"):
            self.frequency, self.channel = self.saved_frequency, self.saved_frequency - 2399
        if (obj, method) == ("file", "read") and "linkstate" in args.get("path", ""):
            entry = {"mac": self.peer.mac, "inactive": 10, "rssi": self.rssi, "pl_ratio": 2,
                     "rssi_ant": [-62, -64], "tx_retries": 3, "tx_failed": 0, "mcs": 7}
            self.sta_stats = ([entry] if self.linked() else []) + self.extra_peers
            self.mesh_stats = ([{"orig_address": self.peer.mac, "tq": 204, "hop_status": "direct",
                                 "last_seen_msecs": 120}] if self.linked() else [])
        return super()._method(obj, method, args)

    def _drift_home(self):
        if self.comes_home_after is not None:
            self.comes_home_after -= 1
            if self.comes_home_after <= 0:
                self.frequency, self.channel = self.saved_frequency, self.saved_frequency - 2399


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.01  # every look at the clock costs a little, so loops always end
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class BenchCase(unittest.TestCase):
    def setUp(self):
        self.a, self.b = BenchRadio("00:30:1a:00:00:0a"), BenchRadio("00:30:1a:00:00:0b")
        self.a.peer, self.b.peer = self.b, self.a
        for radio in (self.a, self.b):
            radio._tune(HOME)
            radio.txpower = 25
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = Clock()
        self.answers = ["yes"]
        self.separate = True  # each radio wired on its own; OneWire cases switch this off

    def one_wire(self):
        self.separate, self.b.reached_through = False, self.a

    def bench(self, **overrides):
        nodes = [Node(name, RadioAPI("fake", "user", "fixture-password", transport=radio), host)
                 for name, radio, host in (("a", self.a, "radio-a"), ("b", self.b, "radio-b"))]
        options = {"to": TO, "home": BACK, "timeout": 30.0, "ask": lambda _: self.answers.pop(0),
                   "clock": self.clock, "sleep": self.clock.sleep, "separate": self.separate,
                   **overrides}
        return Bench(*nodes, Log(self.directory.name, self.clock), **options)

    def changes(self, radio):
        """Every call that could change the radio (`/bin/sh` runs only ping and read commands)."""
        return [call for call in radio.calls
                if call[:2] in (("message-system", "chswitch"), ("network.wireless", "up"),
                                ("network.wireless", "down"), ("system", "reboot"))
                or (call[:2] == ("file", "exec") and call[2]["command"] != "/bin/sh")]

    def rf_toggles(self, radio):
        return [call for call in radio.calls if call[0] == "network.wireless"
                and call[1] in ("up", "down")]

    def stage(self, name, **values):
        defaults = {"hop": {"repeats": 2, "dwell": 1.0}, "missed": {"watch": 20.0},
                    "recover": {"skip_reboot": False}, "capture": {"seconds": 3.0}}
        args = argparse.Namespace(stage=name, yes=False, **{**defaults.get(name, {}), **values})
        return run(self.bench(), args), self.report()

    def report(self):
        return json.loads((Path(self.directory.name) / "report.json").read_text())

    def switches(self, radio):
        return [call for call in radio.calls if call[:2] == ("message-system", "chswitch")]

    def assertHome(self, power=25):
        for radio in (self.a, self.b):
            self.assertEqual(radio.frequency, HOME)
            self.assertEqual(radio.txpower, power)


class RedactionTests(unittest.TestCase):
    def test_secrets_in_the_shapes_the_firmware_uses(self):
        self.assertNotIn("hunter2", redact("wireless.wifi0.key='hunter2'"))
        self.assertNotIn("hunter2", redact("option password 'hunter2'"))
        self.assertNotIn("hunter2", redact('"mesh_key": "hunter2 and more"'))
        self.assertNotIn("hunter2", redact("command='-n mesh -k hunter2 -c 100'"))
        self.assertEqual(redact({"mesh_key": "hunter2", "nested": [{"psk": "hunter2"}]}),
                         {"mesh_key": "<redacted>", "nested": [{"psk": "<redacted>"}]})

    def test_the_lines_we_came_for_survive(self):
        line = """ubus call message-system chswitch '{"count":3,"frequency":2450}'"""
        self.assertEqual(redact(line), line)
        self.assertEqual(redact("first\nsecond"), "first\nsecond")


class ArgumentTests(unittest.TestCase):
    def test_a_switch_must_name_its_frequency(self):
        self.assertEqual(switch_args('{"frequency": 2480}'), {"frequency": 2480})
        for text in ("nope", "[]", "{}", '{"frequency": "2480"}'):
            with self.assertRaises(argparse.ArgumentTypeError):
                switch_args(text)

    def test_passwords_never_come_from_the_command_line(self):
        options = radio_bench.parser().parse_args(["--a", "x", "--b", "y", "discover"])
        self.assertFalse([name for name in vars(options) if "pass" in name.lower()])

    def test_password_sources(self):
        self.assertEqual(passwords({"DOODLE_BENCH_PASSWORD": "one"}), ("one", "one"))
        self.assertEqual(passwords({"DOODLE_BENCH_PASSWORD": "one",
                                    "DOODLE_BENCH_PASSWORD_B": "two"}), ("one", "two"))
        answers = iter(["one", ""])
        self.assertEqual(passwords({}, lambda _: next(answers)), ("one", "one"))


class InventoryTests(unittest.TestCase):
    def test_reports_what_the_service_would_publish_and_what_it_would_not(self):
        result = inventory([({"mac": "00:30:1a:00:00:0b", "rssi": -60}, -94.0)], "link")
        self.assertEqual(result["published"]["link.snr_db"], {"min": 34.0, "max": 34.0})
        self.assertEqual(result["not_published"], ["link.loss_pct"])
        self.assertEqual(result["fields"]["rssi"], ["int"])

    def test_a_vendor_scale_rssi_shows_up_as_not_published(self):
        result = inventory([({"mac": "00:30:1a:00:00:0b", "rssi": 45}, -94.0)], "link")
        self.assertIn("link.signal_dbm", result["not_published"])

    def test_an_entry_without_an_address_is_counted(self):
        self.assertEqual(inventory([({"rssi": -60}, None)], "link")["unidentified"], 1)


class ReadOnlyStageTests(BenchCase):
    def assertUntouched(self):
        for radio in (self.a, self.b):
            self.assertEqual(radio.writes(), [c for c in radio.calls
                                              if c[:2] == ("file", "exec")
                                              and c[2]["command"] == "/bin/sh"])
            self.assertEqual(self.switches(radio), [])
            self.assertFalse([c for c in radio.calls if c[:2] == ("system", "reboot")])

    def test_discover_changes_nothing_and_leaks_no_secret(self):
        status, report = self.stage("discover")
        self.assertEqual(status, 0)
        self.assertUntouched()
        self.assertTrue(report["findings"]["callers"]["a"].startswith("found in /usr/sbin"))
        self.assertNotIn("failed", report["findings"]["callers"]["b"])
        self.assertEqual(report["findings"]["commands_run"], {"a": True, "b": True})
        self.assertIn("chswitch", report["radios"]["a"]["vendor/callers /www"]["stdout"])
        written = "".join(path.read_text() for path in Path(self.directory.name).iterdir())
        self.assertNotIn(MESH_KEY, written)
        self.assertNotIn(WIZARD_KEY, written)
        self.assertNotIn("fixture-password", written)

    def test_every_radio_command_gets_a_path(self):
        self.stage("discover")
        shells = [c for c in self.a.calls if c[:2] == ("file", "exec")]
        self.assertTrue(shells)
        for call in shells:
            self.assertIn("/usr/bin", call[2]["env"]["PATH"])

    def test_discover_says_when_commands_cannot_run(self):
        for radio in (self.a, self.b):  # what a missing PATH would look like
            radio.exec_replies = {"": {"code": 0, "stdout": "", "stderr": "sh: grep: not found"}}
        status, report = self.stage("discover")
        self.assertEqual(status, 1)
        self.assertEqual(report["findings"]["commands_run"], {"a": False, "b": False})
        self.assertTrue(report["findings"]["callers"]["a"].startswith(
            "failed: /usr/sbin: exit 0: sh: grep: not found"))

    def test_discover_tells_nothing_found_from_a_failure(self):
        for radio in (self.a, self.b):
            radio.exec_replies = {"ubus -v list": {"code": 0, "stdout": '"chswitch":{"count"}'},
                                  "grep -rn": {"code": 0, "stdout": ""}}
        status, report = self.stage("discover")
        self.assertEqual(status, 0)
        self.assertEqual(report["findings"]["callers"], {"a": "none", "b": "none"})
        self.assertEqual(report["findings"]["signature"], {"a": "found", "b": "found"})

    def test_discover_reports_a_refused_exec(self):
        self.a.fail["file.exec"] = 6
        status, report = self.stage("discover")
        self.assertEqual(status, 1)
        self.assertIn("permission denied", report["findings"]["callers"]["a"])
        self.assertTrue(report["findings"]["callers"]["b"].startswith("found"))

    def test_a_slow_place_times_out_alone_and_says_so(self):
        timeout = RadioError("radio unreachable at fake: TimeoutError: timed out")
        self.a.exec_replies = {"/www": timeout}
        status, report = self.stage("discover")
        callers = report["findings"]["callers"]["a"]
        self.assertEqual(status, 0)
        self.assertTrue(callers.startswith("found in /usr/sbin"))
        self.assertIn("/www: no answer within", callers)
        self.assertIn("slow command", callers)

    def test_every_search_limits_itself_on_the_radio(self):
        self.stage("discover")
        searches = [c[2]["params"][1] for c in self.a.calls
                    if c[:2] == ("file", "exec") and "grep -rn" in c[2]["params"][1]]
        self.assertEqual(len(searches), len(radio_bench.CALLER_PLACES))
        for command in searches:
            self.assertIn(f"timeout {radio_bench.COMMAND_LIMIT} grep", command)
        self.assertLess(radio_bench.COMMAND_LIMIT, radio_bench.EXEC_TIMEOUT)

    def test_capture_shows_peer_fields_through_the_service_mapping(self):
        status, report = self.stage("capture")
        self.assertEqual(status, 0)
        self.assertUntouched()
        link = report["peers"]["link"]["a"]
        self.assertEqual(link["fields"]["rssi_ant"], ["list"])
        self.assertEqual(link["published"]["link.signal_dbm"], {"min": -61, "max": -61})
        self.assertEqual(link["not_published"], [])
        self.assertEqual(report["peers"]["mesh"]["b"]["published"]["mesh.quality_pct"]["max"], 80)
        self.assertEqual(report["findings"]["too_strong"], [])

    def test_capture_says_when_a_peer_is_too_strong(self):
        self.a.rssi = -25
        _, report = self.stage("capture")
        self.assertEqual(report["findings"]["too_strong"], ["a"])

    def test_capture_without_a_peer_is_a_failure_not_an_empty_success(self):
        self.b.frequency = 2470
        status, report = self.stage("capture")
        self.assertEqual(status, 1)
        self.assertFalse(report["findings"]["peers_seen_on_both"])


class RefusalTests(BenchCase):
    def assertRefused(self, reason):
        status, report = self.stage("hop")
        self.assertEqual(status, 1)
        self.assertIn(reason, " ".join(report["refused"]))
        self.assertEqual(self.switches(self.a) + self.switches(self.b), [])

    def test_a_third_node_on_the_mesh(self):
        self.a.extra_peers = [{"mac": "00:30:1a:00:00:0c", "rssi": -70}]
        self.assertRefused("mesh-wide")

    def test_a_radio_that_is_off(self):
        self.b.rf_up = False
        self.assertRefused("not up")

    def test_no_peer(self):
        self.a.link_broken = self.b.link_broken = True
        self.assertRefused("hears no peer")

    def test_not_starting_from_home(self):
        self.a._tune(2470)
        self.assertRefused("not home")

    def test_a_frequency_the_radio_does_not_list(self):
        bench_to = dict(TO, frequency=2600)
        status = run(self.bench(to=bench_to), argparse.Namespace(stage="hop", yes=False))
        self.assertEqual(status, 1)
        self.assertIn("does not list", " ".join(self.report()["refused"]))

    def test_the_same_frequency_twice(self):
        status = run(self.bench(to=BACK), argparse.Namespace(stage="hop", yes=False))
        self.assertEqual(status, 1)
        self.assertEqual(self.switches(self.a), [])

    def test_no_data_path(self):
        original = self.a._method

        def no_ping(obj, method, args):
            if method == "exec":
                return {"code": 1}
            return original(obj, method, args)
        self.a._method = no_ping
        self.assertRefused("cannot ping")

    def test_answering_no_moves_nothing(self):
        self.answers = ["no"]
        status = self.run_hop()
        self.assertEqual(status, 1)
        self.assertEqual(self.answers, [])
        self.assertEqual(self.switches(self.a) + self.switches(self.b), [])

    def test_answering_yes_is_what_starts_it(self):
        self.answers = ["yes"]
        self.assertEqual(self.run_hop(), 0)
        self.assertEqual(self.answers, [])
        self.assertTrue(self.switches(self.a))

    def run_hop(self):  # a complete stage, so a refusal cannot pass by crashing instead
        return run(self.bench(), argparse.Namespace(stage="hop", yes=False, repeats=1, dwell=1.0))

    def test_a_too_strong_link_warns_but_is_allowed(self):
        self.a.rssi = -25
        status, report = self.stage("hop", repeats=1)
        self.assertEqual(status, 0)
        self.assertIn("too strong", " ".join(report["warnings"]))


class HopTests(BenchCase):
    def test_both_ways_with_either_radio_sending(self):
        status, report = self.stage("hop")
        self.assertEqual(status, 0)
        self.assertEqual(report["findings"]["switches_completed"], "4/4")
        self.assertEqual([(r["sender"], r["to"]) for r in report["rounds"]],
                         [("a", TEST), ("b", HOME), ("b", TEST), ("a", HOME)])
        self.assertIn("data", report["findings"]["timing"])
        self.assertEqual(report["findings"]["saved_config_rewritten"], {"a": False, "b": False})
        self.assertHome()
        self.assertTrue(report["restored"]["ok"])

    def test_arguments_reach_the_radio_unchanged(self):
        self.stage("hop", repeats=1)
        self.assertEqual(self.switches(self.a)[0][2], TO)

    def test_a_switch_that_writes_the_saved_config_is_reported(self):
        self.a.persists = True
        _, report = self.stage("hop", repeats=1)
        self.assertTrue(report["findings"]["saved_config_rewritten"]["a"])

    def test_a_switch_that_resets_power_is_reported_and_undone(self):
        self.a.switch_resets_power = True
        _, report = self.stage("hop", repeats=1)
        self.assertEqual(report["findings"]["tx_power_changed"]["a"], {"from": 25, "to": 32})
        self.assertHome()

    def test_a_switch_that_does_nothing_stops_the_run(self):
        self.a.deaf = self.b.deaf = True
        status, report = self.stage("hop")
        self.assertEqual(status, 1)
        self.assertEqual(len(report["rounds"]), 1)
        self.assertEqual(report["findings"]["switches_completed"], "0/4")
        self.assertNotIn("data", report["rounds"][0]["events"])
        self.assertHome()

    def test_a_rejected_switch_is_recorded(self):
        self.a.fail["message-system.chswitch"] = 6
        status, report = self.stage("hop", repeats=1)
        self.assertEqual(status, 1)
        self.assertIn("permission denied", report["rounds"][0]["events"]["rejected"])

    def test_peering_without_traffic_is_not_a_completed_switch(self):
        self.a.no_data_on = {TEST}
        status, report = self.stage("hop", repeats=1)
        events = report["rounds"][0]["events"]
        self.assertEqual(status, 1)
        self.assertIn("a_peer", events)
        self.assertNotIn("data", events)
        self.assertEqual(report["findings"]["switches_completed"], "0/2")
        self.assertHome()

    def test_data_decides_even_if_assoclist_never_lists_a_peer(self):
        self.a.assoclist_empty = self.b.assoclist_empty = True
        status, report = self.stage("hop", repeats=1)
        self.assertEqual(status, 0)
        self.assertEqual(report["findings"]["switches_completed"], "2/2")
        self.assertNotIn("a_peer", report["rounds"][0]["events"])

    def test_a_lost_reply_is_not_a_rejection(self):
        original = self.a._method

        def answer_lost(obj, method, args):
            result = original(obj, method, args)
            if method == "chswitch":
                raise RadioError("radio unreachable at fake: timed out")
            return result
        self.a._method = answer_lost
        _, report = self.stage("hop", repeats=1)
        self.assertIn("data", report["rounds"][0]["events"])
        self.assertHome()


class MissedTests(BenchCase):
    def test_a_stranded_radio_is_reported_and_brought_home(self):
        status, report = self.stage("missed")
        findings = report["findings"]
        self.assertEqual(status, 0)
        self.assertIsNotNone(findings["a_moved_without_b_after_s"])
        self.assertEqual(findings["b_landed_on"], HOME)
        self.assertIsNone(findings["rejoined_by_itself"])
        self.assertEqual(findings["a_ended_on"], TEST)
        self.assertHome(power=25)

    def test_a_radio_that_comes_home_by_itself(self):
        self.a.comes_home_after = 3
        _, report = self.stage("missed")
        self.assertEqual(report["findings"]["rejoined_by_itself"]["frequency"], HOME)
        self.assertEqual(self.switches(self.a), [self.switches(self.a)[0]])  # no restoring switch


class RecoverTests(BenchCase):
    def test_saved_settings_win(self):
        status, report = self.stage("recover")
        findings = report["findings"]
        self.assertEqual(status, 0)
        self.assertTrue(findings["rf_toggle_returns_a_home"])
        self.assertTrue(findings["rf_toggle_returns_b_home"])
        self.assertTrue(findings["reboot_seen"])
        self.assertTrue(findings["reboot_returns_home"])
        self.assertHome()

    def test_a_switch_that_survives_recovery_is_the_finding_and_still_comes_home(self):
        self.a.persists = True
        status, report = self.stage("recover")
        self.assertEqual(status, 1)
        self.assertFalse(report["findings"]["rf_toggle_returns_a_home"])
        self.assertHome()

    def test_reboot_can_be_skipped(self):
        status, report = self.stage("recover", skip_reboot=True)
        self.assertEqual(status, 0)
        self.assertNotIn("reboot_returns_home", report["findings"])
        self.assertFalse([c for c in self.a.calls if c[:2] == ("system", "reboot")])


class WiringTests(unittest.TestCase):
    def paths(self, outputs, platform):
        def run(command, **_):
            if outputs[command[-1]] is None:
                raise OSError("no such command")
            return SimpleNamespace(stdout=outputs[command[-1]])
        return radio_bench.wired_interfaces(list(outputs), run=run, platform=platform)

    def test_macos_route_output(self):
        found = self.paths({"radio-a": "   route to: 10.1.0.1\n  interface: en5\n",
                            "radio-b": "  interface: en7\n"}, "darwin")
        self.assertEqual(found, {"radio-a": "en5", "radio-b": "en7"})

    def test_linux_route_output(self):
        found = self.paths({"10.1.0.1": "10.1.0.1 dev eth1 src 10.1.0.9 uid 501\n"}, "linux")
        self.assertEqual(found, {"10.1.0.1": "eth1"})

    def test_an_unknown_path_is_none_so_the_safe_mode_applies(self):
        found = self.paths({"radio-a": None, "radio-b": "nothing useful"}, "darwin")
        self.assertEqual(found, {"radio-a": None, "radio-b": None})

    def test_only_two_known_different_interfaces_count_as_separate(self):
        self.assertTrue(radio_bench.separate_paths("en5", "en7"))
        for pair in (("en5", "en5"), ("en5", None), (None, "en7"), (None, None)):
            self.assertFalse(radio_bench.separate_paths(*pair), pair)


class RadioShellTests(unittest.TestCase):
    """The search commands' own text, run by a real /bin/sh (dash on CI, close to the radio's
    ash) against stub directories and a stub `timeout`. The fake radio never runs them."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name, text in (("hit/caller.sh", "ubus call message-system chswitch {}\n"),
                           ("miss/other.sh", "nothing here\n")):
            (self.root / name).parent.mkdir()
            (self.root / name).write_text(text)
        self.stubs = {"passes": '#!/bin/sh\nshift\nexec "$@"\n', "expires": "#!/bin/sh\nexit 124\n"}

    def search(self, place, timeout=None):
        bin_dir = self.root / f"bin-{timeout}"
        bin_dir.mkdir(exist_ok=True)
        if timeout:
            stub = bin_dir / "timeout"
            stub.write_text(self.stubs[timeout])
            stub.chmod(0o755)
        command = radio_bench.CALLER_SEARCH.format(limit=radio_bench.COMMAND_LIMIT,
                                                   places=self.root / place)
        done = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True,
                              env={"PATH": f"{bin_dir}:/usr/bin:/bin"})
        return radio_bench.searched({"code": done.returncode, "stdout": done.stdout,
                                     "stderr": done.stderr})

    def test_found_and_none(self):
        self.assertEqual(self.search("hit", "passes"), "found")
        self.assertEqual(self.search("miss", "passes"), "none")

    def test_a_timeout_is_a_failure_even_with_nothing_found(self):
        self.assertEqual(self.search("miss", "expires"), "failed: exit 124: timed out after 25 s")

    def test_a_missing_timeout_command_is_a_failure_not_none(self):
        self.assertEqual(self.search("miss"), "failed: exit 127: timeout or grep is missing on "
                                              "the radio")


class OneWireTests(BenchCase):
    """Radio b reached only through radio a's mesh link: nothing may cut b off."""

    def setUp(self):
        super().setUp()
        self.one_wire()

    def test_hop_works_over_one_wire(self):
        status, report = self.stage("hop")
        self.assertEqual(status, 0)
        self.assertEqual(report["findings"]["switches_completed"], "4/4")
        self.assertHome()

    def test_a_radio_b_left_behind_is_fetched_by_sending_a_to_it(self):
        self.b.misses = {2}  # b misses the last switch home, sent from a
        status, report = self.stage("hop", repeats=2)
        self.assertEqual(status, 1)
        self.assertTrue(report["restored"]["ok"])
        self.assertIn(f"a: chswitch {TEST} to find b", report["restored"]["steps"])
        self.assertHome()
        self.assertEqual(self.rf_toggles(self.b), [])

    def test_radio_b_is_never_rf_toggled_even_when_it_cannot_be_fetched(self):
        self.b.misses = set(range(2, 20))
        status, report = self.stage("hop", repeats=2)
        self.assertEqual(status, 3)
        self.assertEqual(self.b.frequency, TEST)
        self.assertEqual(self.a.frequency, HOME)
        self.assertEqual(self.rf_toggles(self.b), [])

    def test_radio_b_is_never_rf_toggled_while_it_is_still_reachable(self):
        # Both stuck on the test channel: a's RF toggle reloads a saved config the switch wrote,
        # so b stays reachable there. Toggling b now would cut it off until a power cycle.
        self.a.persists = True
        self.a.ignores_home = self.b.ignores_home = True
        status, report = self.stage("hop", repeats=1)
        self.assertEqual(status, 3)
        self.assertTrue(self.rf_toggles(self.a))
        self.assertEqual(self.rf_toggles(self.b), [])

    def test_missed_and_recover_need_a_wire_to_b(self):
        for stage in ("missed", "recover"):
            status, report = self.stage(stage)
            self.assertEqual(status, 1)
            self.assertIn("wired path", report["refused"][0])
        self.assertEqual(self.changes(self.a) + self.changes(self.b), [])


class CheckTests(BenchCase):
    """From a Mac wired to radio a only, with radio b on and meshed."""

    def setUp(self):
        super().setUp()
        self.one_wire()

    def check(self, read_only=False, **overrides):
        options = {"to": None, "home": None, **overrides}
        args = argparse.Namespace(stage="check", read_only=read_only, yes=False)
        status = run(self.bench(**options), args)
        return status, {item["check"]: item for item in self.report()["checks"]}

    @staticmethod
    def having(checks, status):
        return [name for name, item in checks.items() if item["status"] == status]

    def test_read_only_passes_on_a_healthy_pair_and_changes_nothing(self):
        status, checks = self.check(read_only=True)
        self.assertEqual(status, 0)
        self.assertEqual(self.having(checks, "FAIL") + self.having(checks, "WARN"), [])
        for name in ("a: diagnostics the service would publish",
                     "b: values the dashboard depends on", "data b -> a across the mesh",
                     "b: supervisor read path", "same channel"):
            self.assertEqual(checks[name]["status"], "PASS", name)
        self.assertIn("through the mesh", checks["wiring"]["detail"])
        self.assertEqual(checks["power and channel changes"]["detail"], "--read-only")
        self.assertEqual(self.changes(self.a) + self.changes(self.b), [])

    def test_radio_b_unreachable_through_the_mesh_is_a_failure_and_nothing_changes(self):
        self.a.link_broken = self.b.link_broken = True
        status, checks = self.check()
        self.assertEqual(status, 1)
        self.assertIn("is the link up?", checks["b: reach and log in"]["detail"])
        self.assertEqual(checks["power and channel changes"]["status"], "SKIP")
        self.assertEqual(self.changes(self.a), [])

    def test_old_firmware_and_missing_permissions_fail(self):
        self.b.firmware = "firmware-2023-09.03"
        self.a.acls = {"ubus": {"system": ["info", "board"]}}
        status, checks = self.check(read_only=True)
        self.assertEqual(status, 1)
        self.assertEqual(checks["a: API login and permissions"]["status"], "FAIL")
        self.assertEqual(checks["b: firmware 2025-06 or later"]["status"], "FAIL")
        self.assertEqual(checks["same firmware"]["status"], "WARN")

    def test_a_too_strong_signal_warns_without_failing(self):
        self.a.rssi = -25
        status, checks = self.check(read_only=True)
        self.assertEqual(status, 0)
        self.assertIn("too strong", checks["a: signal from 00:30:1a:00:00:0b"]["detail"])

    def test_peer_fields_the_service_cannot_map_are_called_out(self):
        self.b.rssi = 45  # a vendor scale, not dBm
        status, checks = self.check(read_only=True)
        self.assertEqual(status, 0)
        self.assertIn("no signal the service can map",
                      checks["b: signal from 00:30:1a:00:00:0a"]["detail"])
        self.assertIn("link.signal_dbm", checks["b: link keys the dashboard reads"]["detail"])
        self.assertIn("link.signal_dbm", checks["b: values the dashboard depends on"]["detail"])

    def test_power_moves_one_step_through_the_supervisor_and_back(self):
        status, checks = self.check()
        self.assertEqual(status, 0)
        self.assertEqual(checks["a: transmit power change"]["detail"], "25 -> 24 dBm, read back")
        self.assertEqual(checks["link at 24 dBm"]["status"], "PASS")
        self.assertEqual(checks["a: transmit power restored"]["status"], "PASS")
        self.assertEqual(checks["mesh-wide channel switch"]["status"], "SKIP")
        self.assertEqual([call[2]["params"][-1] for call in self.changes(self.a)], ["2400", "2500"])
        self.assertEqual(self.changes(self.b), [])
        self.assertEqual(checks["both radios back where they started"]["detail"], "")
        self.assertHome()

    def test_any_failure_stops_before_changes(self):
        self.b.firmware = "firmware-2023-09.03"
        status, checks = self.check()
        self.assertEqual(status, 1)
        self.assertEqual(checks["power and channel changes"]["detail"], "a check above failed")
        self.assertEqual(self.answers, ["yes"])  # never asked
        self.assertEqual(self.changes(self.a) + self.changes(self.b), [])

    def test_a_link_that_carries_no_data_fails(self):
        self.a.no_data_on = {HOME}
        status, checks = self.check(read_only=True)
        self.assertEqual(status, 1)
        self.assertEqual(checks["data a -> b across the mesh"]["status"], "FAIL")
        self.assertEqual(checks["data b -> a across the mesh"]["status"], "PASS")

    def test_a_service_component_that_would_be_stale_fails(self):
        self.a.fail["system.info"] = 9
        status, checks = self.check(read_only=True)
        self.assertEqual(status, 1)
        self.assertEqual(checks["a: diagnostics the service would publish"]["status"], "FAIL")
        self.assertIn("system STALE", checks["a: diagnostics the service would publish"]["detail"])

    def test_a_failed_return_is_put_right_by_restore(self):
        self.b.ignores_home = True  # b takes no request home, but still follows a's
        status, checks = self.check(to=TO, home=BACK)
        self.assertEqual(status, 1)
        self.assertEqual(checks[f"one request to b moves both radios to {HOME} MHz"]["status"],
                         "FAIL")
        self.assertIn("a: chswitch home", checks["both radios back where they started"]["detail"])
        self.assertHome()

    def test_declining_leaves_everything_alone(self):
        self.answers = ["no"]
        status, checks = self.check()
        self.assertEqual(status, 0)
        self.assertEqual(self.answers, [])
        self.assertEqual(checks["power and channel changes"]["detail"], "not confirmed")
        self.assertEqual(self.changes(self.a) + self.changes(self.b), [])

    def test_one_request_from_either_end_moves_both_radios(self):
        status, checks = self.check(to=TO, home=BACK)
        self.assertEqual(status, 0)
        self.assertEqual(checks[f"one request to a moves both radios to {TEST} MHz"]["status"],
                         "PASS")
        self.assertEqual(checks[f"one request to b moves both radios to {HOME} MHz"]["status"],
                         "PASS")
        self.assertEqual(self.switches(self.a)[0][2], TO)
        self.assertEqual(self.switches(self.b)[0][2], BACK)
        self.assertHome()

    def test_a_switch_that_moves_nothing_fails_and_leaves_both_home(self):
        self.a.deaf = self.b.deaf = True
        status, checks = self.check(to=TO, home=BACK)
        self.assertEqual(status, 1)
        self.assertEqual(checks[f"one request to a moves both radios to {TEST} MHz"]["status"],
                         "FAIL")
        self.assertEqual(checks["both radios back where they started"]["status"], "PASS")
        self.assertHome()

    def test_a_power_change_the_radio_ignores_fails_and_is_cleaned_up(self):
        self.a.ignore_txpower = True
        status, checks = self.check()
        self.assertEqual(status, 1)
        self.assertEqual(checks["changes"]["status"], "FAIL")
        self.assertEqual(checks["both radios back where they started"]["status"], "PASS")

    def test_to_without_home_is_refused(self):
        status, checks = self.check(to=TO)
        self.assertEqual(status, 1)
        self.assertEqual(checks["arguments"]["status"], "FAIL")
        self.assertEqual(self.changes(self.a) + self.changes(self.b), [])


class CommandLineTests(BenchCase):
    def test_main_runs_a_read_only_stage_and_writes_its_run_directory(self):
        radios = {"radio-a": self.a, "radio-b": self.b}
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)

        def api(host, username, password, **options):
            return RadioAPI(host, username, password, transport=radios[host], **options)
        environment = {"DOODLE_BENCH_PASSWORD": "fixture-password"}
        with patch.object(radio_bench, "RadioAPI", api), patch.dict(os.environ, environment):
            status = radio_bench.main(["--a", "radio-a", "--b", "radio-b",
                                       "--out", self.directory.name, "discover"])
        self.assertEqual(status, 0)
        runs = list(Path(self.directory.name).glob("*-discover/report.json"))
        self.assertEqual(len(runs), 1)
        self.assertNotIn("fixture-password", runs[0].read_text())


class RestoreTests(BenchCase):
    def test_interrupting_a_stage_still_brings_the_radios_home(self):
        real = self.clock.sleep
        calls = []

        def interrupt(seconds):
            calls.append(seconds)
            if len(calls) == 1:
                raise KeyboardInterrupt
            real(seconds)
        bench = self.bench(sleep=interrupt)
        status = run(bench, argparse.Namespace(stage="hop", yes=True, repeats=2, dwell=1.0))
        self.assertEqual(status, 1)
        self.assertIn("KeyboardInterrupt", self.report()["findings"]["error"])
        self.assertHome()

    def test_a_bug_in_a_stage_still_brings_the_radios_home(self):
        bench = self.bench()
        bench.a.saved = lambda: 1 / 0
        status = run(bench, argparse.Namespace(stage="hop", yes=True, repeats=1, dwell=1.0))
        self.assertEqual(status, 1)
        self.assertIn("ZeroDivisionError", self.report()["findings"]["error"])
        self.assertHome()

    def test_rf_toggle_when_the_switch_back_is_ignored(self):
        self.a.ignores_home = self.b.ignores_home = True
        status, report = self.stage("hop", repeats=1)
        self.assertEqual(status, 1)
        self.assertTrue([s for s in report["restored"]["steps"] if "RF toggle" in s])
        self.assertHome()

    def test_a_radio_left_away_is_exit_three_and_says_so(self):
        self.a.persists = True
        self.a.ignores_home = self.b.ignores_home = True
        status, report = self.stage("hop", repeats=1)
        self.assertEqual(status, 3)
        self.assertFalse(report["restored"]["at_home"])

    def test_an_unreachable_radio_does_not_stop_the_other_restoring(self):
        original = self.a._method

        def die(obj, method, args):
            result = original(obj, method, args)
            if method == "chswitch":
                self.a.unreachable = True
            return result
        self.a._method = die
        self.b.deaf = True
        status, report = self.stage("hop", repeats=1)
        self.assertEqual(status, 3)
        self.assertEqual(report["restored"]["state"]["b"]["frequency"], HOME)


if __name__ == "__main__":
    unittest.main()
