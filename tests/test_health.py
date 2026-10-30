"""Diagnostics without ROS: observations, metric states, failures, peers, limits, the service
component and the tx_power parameter rules."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker/entrypoints"))
sys.path.insert(0, str(ROOT / "tests"))
import doodle_config  # noqa: E402
import supervisor  # noqa: E402
from health import ERROR, OK, STALE, WARN, HealthReporter  # noqa: E402
import health_ros  # noqa: E402
from health_ros import OFF_REFUSED, ParameterBridge, parameter_value, ros_values  # noqa: E402
from radio_api import RadioAPI  # noqa: E402
from fake_radio import FakeRadio, MESH_KEY  # noqa: E402
from test_supervisor import FakeClock  # noqa: E402

PEER = "00:30:1A:00:00:01"


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.radio = FakeRadio()
        self.clock = FakeClock()
        self.api = RadioAPI("fake", "user", "fixture-password", transport=self.radio)
        self.reporter = HealthReporter("mesh", "vehicle", clock=self.clock)
        self.controller = self.make_controller()

    def make_controller(self, **tx_power):
        stop = threading.Event()
        stop.wait = lambda seconds: (self.clock.sleep(seconds), stop.is_set())[1]
        api = RadioAPI("fake", "user", "fixture-password", transport=self.radio)
        return supervisor.Controller(api, doodle_config.tx_power(tx_power),
                                     Path(self.tmp.name, "target.json"), clock=self.clock,
                                     stop_event=stop, sleep=self.clock.sleep)

    def rows(self):
        rows = self.reporter.snapshot(self.controller.health_state())
        json.dumps(rows, allow_nan=False)  # what the ROS adapter serializes must be valid JSON
        return {row["name"].split(": ", 1)[1]: row for row in rows}

    def test_before_any_observation_everything_is_waiting_not_ok(self):
        rows = self.rows()
        self.assertEqual(sorted(rows), ["links", "radio", "service", "system", "temperature"])
        self.assertTrue(all(row["level"] == STALE for row in rows.values()))
        self.assertEqual(rows["service"]["message"], "waiting for first observation")

    def test_a_healthy_radio(self):
        self.controller = self.make_controller(startup="medium")
        self.controller.reconcile()
        self.reporter.poll(self.api)
        rows = self.rows()
        self.assertEqual({name: row["level"] for name, row in rows.items()},
                         {"service": OK, "radio": OK, "links": OK, "system": OK,
                          "temperature": OK})
        service = rows["service"]["values"]
        self.assertEqual((service["rf.target"], service["rf.target_dbm"],
                          service["rf.tx_power_dbm"], service["rf.converged"]),
                         ("medium", 27, 27, True))
        self.assertEqual((service["rf.tx_power_min_dbm"], service["rf.tx_power_max_dbm"]),
                         (19, 32))
        radio = rows["radio"]
        self.assertEqual(radio["message"], "27 dBm, channel 51 (2450 MHz, 20 MHz wide)")
        values = radio["values"]
        self.assertEqual((values["rf.state"], values["rf.tx_power_dbm"], values["rf.bandwidth_mhz"],
                          values["rf.noise_dbm"], values["rf.channel_activity_pct"]),
                         ("on", 27, 20, -94.0, 1))
        # No override (255): ACK and broadcast frames use the level itself.
        self.assertEqual((values["rf.ack_power_dbm"], values["rf.ack_power_override"]), (27, False))
        self.assertTrue(values["rf.auto_power_control"])
        system = rows["system"]["values"]
        self.assertEqual((system["system.firmware"], system["system.load_1m"],
                          system["system.memory_free_mb"]), ("firmware-2026-03.1", 0.03, 12.0))
        self.assertIsInstance(system["uptime_s"], int)
        self.assertEqual(rows["system"]["hardware_id"], "EXAMPLE-SUB smartradio-example")
        temperature = rows["temperature"]["values"]
        self.assertIsNone(temperature["temp.radio_c"])
        self.assertEqual(temperature["health.metric.temp.radio_c.state"], "unsupported")
        meta = rows["radio"]["values"]
        self.assertEqual((meta["health.instance"], meta["health.vehicle_id"],
                          meta["health.service"], meta["health.availability"]),
                         ("mesh", "vehicle", "doodle", "current"))
        self.assertEqual(self.radio.writes(), [(  # only the controller wrote, once
            "file", "exec", {"command": "/usr/sbin/iw",
                             "params": ["dev", "wlan0", "set", "txpower", "fixed", "2700"]})])

    def test_ack_or_broadcast_power_above_the_level_warns(self):
        self.radio.txpower = 19
        self.radio.debugfs["txpower_selfgen"] = "29\n"
        self.reporter.poll(self.api)
        radio = self.rows()["radio"]
        self.assertEqual(radio["level"], WARN)
        self.assertIn("ACK power 29 dBm is above the 19 dBm", radio["message"])
        self.assertEqual((radio["values"]["rf.ack_power_dbm"],
                          radio["values"]["rf.ack_power_override"]), (29, True))
        self.assertEqual(radio["values"]["rf.broadcast_power_dbm"], 19)

    def test_unreadable_debugfs_is_unsupported_not_a_warning(self):
        self.radio.debugfs = {"txpower_mcast": ""}  # selfgen missing; mcast reads empty
        self.reporter.poll(self.api)
        radio = self.rows()["radio"]
        self.assertEqual(radio["level"], OK, radio["message"])
        for key in ("rf.ack_power_dbm", "rf.broadcast_power_dbm"):
            self.assertIsNone(radio["values"][key])
            self.assertEqual(radio["values"][f"health.metric.{key}.state"], "unsupported")
        reads = len(self.radio.calls)
        self.reporter.poll(self.api)  # not asked again until the radio reboots
        self.assertFalse(any("ath9k" in str(c) for c in self.radio.calls[reads:]))

    def test_a_failed_read_blanks_only_what_it_provides(self):
        self.reporter.poll(self.api)
        links_before = self.rows()["links"]["values"]["health.sample_id"]
        self.radio.unreadable.add("/tmp/linkstate_current.json")
        self.clock.now = 5
        self.reporter.poll(self.api)
        rows = self.rows()
        radio = rows["radio"]
        self.assertEqual(radio["level"], WARN)
        self.assertIn("measurement unavailable", radio["message"])
        self.assertIsNone(radio["values"]["rf.noise_dbm"])
        self.assertEqual(radio["values"]["health.metric.rf.noise_dbm.state"], "unavailable")
        self.assertEqual(radio["values"]["rf.tx_power_dbm"], 32)  # from another call: still there
        links = rows["links"]
        self.assertEqual(links["values"]["health.availability"], "retrying")
        self.assertEqual(links["values"]["health.sample_id"], links_before)
        self.assertIn("not found", links["values"]["health.error"])

    def test_outage_keeps_last_values_then_goes_stale_and_recovers(self):
        self.reporter.poll(self.api)
        first = self.rows()["radio"]
        self.radio.unreachable = True
        self.clock.now = 4
        self.reporter.poll(self.api)
        held = self.rows()["radio"]
        self.assertEqual(held["level"], OK)
        self.assertEqual(held["values"]["health.availability"], "retrying")
        self.assertEqual(held["values"]["health.sample_id"], first["values"]["health.sample_id"])
        self.assertEqual(held["values"]["health.sample_age_s"], 4)
        self.assertEqual(held["values"]["rf.tx_power_dbm"], 32)  # last known, marked as such
        self.assertIn("unreachable", held["values"]["health.error"])
        self.clock.now = 20
        stale = self.rows()["radio"]
        self.assertEqual((stale["level"], stale["values"]["health.last_level"]), (STALE, OK))
        self.assertEqual(stale["values"]["health.availability"], "stale")
        self.radio.unreachable = False
        self.reporter.poll(self.api)
        back = self.rows()["radio"]
        self.assertEqual((back["level"], back["values"]["health.sample_age_s"]), (OK, 0))
        self.assertNotEqual(back["values"]["health.sample_id"],
                            first["values"]["health.sample_id"])

    def test_rf_off_is_reported_without_rf_measurements(self):
        self.radio.rf_up = False
        self.reporter.poll(self.api)
        radio = self.rows()["radio"]
        self.assertEqual((radio["level"], radio["message"]), (OK, "RF off"))
        self.assertEqual(radio["values"]["rf.state"], "off")
        self.assertNotIn("rf.tx_power_dbm", radio["values"])

    def test_peers_appear_and_departures_are_explicit(self):
        self.radio.sta_stats = [{"mac": PEER, "rssi": -61, "pl_ratio": 0, "tx_mcs": 7,
                                 "nested": {"a": 1}},
                                {"rssi": -70}]  # no address: counted, not published
        self.radio.mesh_stats = [{"orig_address": "00:30:1a:00:00:02", "tq": 255}]
        self.reporter.poll(self.api)
        rows = self.rows()
        link = rows[f"link/{PEER.lower()}"]
        self.assertEqual((link["level"], link["values"]["link.rssi"],
                          link["values"]["link.present"]), (OK, -61, True))
        self.assertEqual(link["values"]["link.nested"], '{"a": 1}')
        self.assertNotIn("link.mac", [k for k, v in link["values"].items() if v is None])
        self.assertEqual(rows["mesh/00:30:1a:00:00:02"]["values"]["mesh.tq"], 255)
        self.assertEqual((rows["links"]["values"]["links.stations"],
                          rows["links"]["values"]["links.mesh_nodes"],
                          rows["links"]["values"]["links.unidentified"]), (2, 1, 1))
        self.radio.sta_stats = []
        self.clock.now = 30
        self.reporter.poll(self.api)
        gone = self.rows()[f"link/{PEER.lower()}"]
        self.assertEqual(gone["level"], OK)
        self.assertEqual(gone["values"]["link.present"], False)
        self.assertEqual(gone["values"]["link.last_seen_s"], 30)
        self.assertNotIn("link.rssi", gone["values"])  # never a stale reading presented as current
        self.assertEqual(gone["values"]["health.availability"], "current")

    def test_peers_carry_vendor_neutral_link_quality(self):
        self.radio.sta_stats = [{"mac": PEER, "rssi": -61, "pl_ratio": 3, "tx_mcs": 7},
                                {"mac": "00:30:1a:00:00:03", "rssi": "-70", "snr": 21},
                                {"mac": "00:30:1a:00:00:04", "rssi": 60}]  # a vendor scale, not dBm
        self.radio.mesh_stats = [{"orig_address": "00:30:1a:00:00:02", "tq": 204}]
        self.reporter.poll(self.api)
        rows = self.rows()
        near = rows[f"link/{PEER.lower()}"]["values"]
        self.assertEqual((near["link.signal_dbm"], near["link.snr_db"], near["link.loss_pct"]),
                         (-61, 33.0, 3))  # SNR from this report's -94 dBm noise floor
        self.assertEqual(near["link.rssi"], -61)  # the vendor field stays alongside
        reported = rows["link/00:30:1a:00:00:03"]["values"]
        self.assertEqual((reported["link.signal_dbm"], reported["link.snr_db"]), (-70, 21))
        vendor = rows["link/00:30:1a:00:00:04"]["values"]
        self.assertNotIn("link.signal_dbm", vendor)
        self.assertNotIn("link.snr_db", vendor)
        self.assertEqual(rows["mesh/00:30:1a:00:00:02"]["values"]["mesh.quality_pct"], 80)
        self.assertNotIn("link.quality_pct", near)

        self.radio.noise = None  # no noise floor in the report: no derived SNR
        self.clock.now = 10
        self.reporter.poll(self.api)
        near = self.rows()[f"link/{PEER.lower()}"]["values"]
        self.assertEqual(near["link.signal_dbm"], -61)
        self.assertNotIn("link.snr_db", near)

        self.radio.sta_stats = []  # departed: no measurement presented as current
        self.clock.now = 20
        self.reporter.poll(self.api)
        gone = self.rows()[f"link/{PEER.lower()}"]["values"]
        self.assertNotIn("link.signal_dbm", gone)
        self.assertEqual(gone["link.present"], False)

    def test_link_limits_apply_per_peer(self):
        self.reporter = HealthReporter("mesh", settings={"limits": {
            "link.signal_dbm": {"warn_below": -80, "error_below": -90}}}, clock=self.clock)
        self.radio.sta_stats = [{"mac": PEER, "rssi": -85}, {"mac": "00:30:1a:00:00:03", "rssi": -50}]
        self.reporter.poll(self.api)
        rows = self.rows()
        weak = rows[f"link/{PEER.lower()}"]
        self.assertEqual(weak["level"], WARN)
        self.assertIn("link.signal_dbm -85 crossed warn_below -80", weak["message"])
        self.assertEqual(rows["link/00:30:1a:00:00:03"]["level"], OK)

    def test_configured_limits_raise_the_level(self):
        self.reporter = HealthReporter("mesh", settings={"limits": {
            "links.stations": {"warn_below": 1}, "rf.noise_dbm": {"error_above": -95}}},
            clock=self.clock)
        self.reporter.poll(self.api)
        rows = self.rows()
        self.assertEqual(rows["links"]["level"], WARN)
        self.assertIn("links.stations 0 crossed warn_below 1", rows["links"]["message"])
        self.assertEqual(rows["radio"]["level"], ERROR)

    def test_reboot_reidentifies_the_radio(self):
        self.reporter.poll(self.api)
        self.radio.reboot()
        board_reads = sum(c[:2] == ("system", "board") for c in self.radio.calls)
        self.reporter.poll(self.api)
        self.assertEqual(sum(c[:2] == ("system", "board") for c in self.radio.calls),
                         board_reads + 1)

    def test_service_component_follows_the_controller(self):
        self.controller = self.make_controller(startup="low")
        self.controller.reconcile()
        self.assertEqual(self.rows()["service"]["level"], OK)
        self.radio.ignore_txpower = True
        self.radio.wifi_restart()
        self.controller.reconcile()
        service = self.rows()["service"]
        self.assertEqual(service["level"], ERROR)
        self.assertIn("did not reach 23 dBm", service["message"])
        self.controller.changing = "setting 23 dBm"
        self.clock.now += 100  # a long change in progress is not a stale observation
        self.assertEqual(self.rows()["service"]["values"]["health.availability"], "current")
        self.controller.changing = ""
        self.assertEqual(self.rows()["service"]["level"], STALE)

    def test_nothing_secret_is_published(self):
        self.reporter.poll(self.api)
        text = json.dumps(self.rows())
        self.assertNotIn(MESH_KEY, text)
        self.assertNotIn("fixture-password", text)

    def test_ros_values_keep_null_and_booleans(self):
        self.assertEqual(ros_values({"missing": None, "on": False, "zero": 0, "s": "x"}),
                         {"missing": "null", "on": "false", "zero": "0", "s": "x"})
        self.assertEqual((parameter_value(25), parameter_value("medium")), (25, "medium"))


class ParameterTests(unittest.TestCase):
    def setUp(self):
        tests = HealthTests("test_a_healthy_radio")
        tests.setUp()
        self.addCleanup(tests.tmp.cleanup)
        self.radio = tests.radio
        self.controller = tests.make_controller(startup="medium")
        self.controller.reconcile()
        self.bridge = ParameterBridge(self.controller)

    def test_levels_and_dbm_apply_and_off_is_refused(self):
        self.assertEqual(self.bridge.set("low"), (True, ""))
        self.assertEqual(self.radio.txpower, 23)
        self.assertEqual(self.bridge.set(25), (True, ""))
        self.assertEqual((self.radio.txpower, self.bridge.wanted()), (25, 25))
        for value in ("off", False, "OFF"):
            self.assertEqual(self.bridge.set(value), (False, OFF_REFUSED))
        self.assertTrue(self.radio.rf_up)

    def test_invalid_and_impossible_values_are_refused(self):
        for value in (15, "loud", 25.0, True, None):
            accepted, reason = self.bridge.set(value)
            self.assertFalse(accepted, value)
            self.assertTrue(reason)
        self.assertEqual(self.controller.target, "medium")

    def test_startup_and_following_the_launcher(self):
        self.bridge.set("high")
        self.assertEqual(self.bridge.set("startup"), (True, ""))
        self.assertEqual((self.controller.target, self.radio.txpower), ("medium", 27))
        self.controller.request("off")  # from the launcher: the parameter follows
        self.assertEqual(self.bridge.wanted(), "off")
        self.assertEqual(self.bridge.set("off"), (True, ""))  # the follow-up write is a no-op
        self.assertEqual(self.bridge.set("standby"), (True, ""))  # back on from ROS is fine
        self.assertEqual((self.radio.rf_up, self.radio.txpower), (True, 19))

    def test_unreachable_radio_accepts_and_keeps_trying(self):
        self.radio.unreachable = True
        with self.assertLogs("doodle.health", "WARNING"):
            accepted, reason = self.bridge.set("low")
        self.assertTrue(accepted)
        self.assertIn("not applied yet", reason)
        self.assertEqual(self.controller.target, "low")


class StartupTests(unittest.TestCase):
    """ROS starts in the background: a failing or hanging start never holds up power control."""

    def run_monitor(self, start, **kwargs):
        controller = type("C", (), {"diagnostics": "off", "target": "unchanged"})()
        config = {"health": doodle_config.health(None)}
        with patch.object(health_ros.RosHealth, "start", start), \
                patch.object(health_ros.RosHealth, "stop") as stop:
            with health_ros.ros_health(controller, config, threading.Event(), None, **kwargs):
                yield_state = controller.diagnostics  # the body runs at once, whatever ROS does
                deadline = time.monotonic() + 5
                while controller.diagnostics == "starting" and time.monotonic() < deadline:
                    time.sleep(0.01)
        return yield_state, controller.diagnostics, stop

    def test_failed_start_is_reported(self):
        def start(_):
            raise RuntimeError("rmw init failed")
        with self.assertLogs("doodle.health", "ERROR"):
            _, state, _ = self.run_monitor(start)
        self.assertEqual(state, "failed: rmw init failed")

    def test_hanging_start_is_reported_and_abandoned(self):
        release = threading.Event()
        self.addCleanup(release.set)
        with self.assertLogs("doodle.health", "ERROR"):
            first, state, stop = self.run_monitor(lambda _: release.wait(10), start_timeout=0.05)
        self.assertEqual(first, "starting")
        self.assertIn("did not start within 0.05s", state)
        stop.assert_not_called()

    def test_successful_start_publishes_and_stops(self):
        _, state, stop = self.run_monitor(lambda _: None)
        self.assertEqual(state, "publishing")
        stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
