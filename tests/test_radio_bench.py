"""The two-radio bench against two simulated radios. `chswitch` semantics here are invented: the
real ones are what the tool exists to find out. These tests pin its safety behavior instead:
read-only stages stay read-only, nothing moves past a refusal, and the radios always go home."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
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
        return super().__call__(payload, timeout)

    def _method(self, obj, method, args):
        if (obj, method) == ("message-system", "chswitch"):
            if self.deaf or (self.ignores_home and args["frequency"] == HOME):
                return {}
            hearing = self.linked()
            self._tune(args["frequency"])
            if hearing:
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

    def bench(self, **overrides):
        nodes = [Node(name, RadioAPI("fake", "user", "fixture-password", transport=radio), host)
                 for name, radio, host in (("a", self.a, "radio-a"), ("b", self.b, "radio-b"))]
        options = {"to": TO, "home": BACK, "timeout": 30.0, "ask": lambda _: self.answers.pop(0),
                   "clock": self.clock, "sleep": self.clock.sleep, **overrides}
        return Bench(*nodes, Log(self.directory.name, self.clock), **options)

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
        self.assertTrue(report["findings"]["callers_found"]["a"])
        self.assertIn("chswitch", report["radios"]["a"]["vendor/callers"])
        written = "".join(path.read_text() for path in Path(self.directory.name).iterdir())
        self.assertNotIn(MESH_KEY, written)
        self.assertNotIn(WIZARD_KEY, written)
        self.assertNotIn("fixture-password", written)

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
