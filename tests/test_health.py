"""Diagnostics without ROS: observations, metric states, failures, peers, limits, the service
component and the tx_power parameter rules."""
import json
import logging
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

    def poll_survey(self, at, active, rx, tx, busy=0):
        self.clock.now = at
        self.radio.survey = {"active_time": active, "busy_time": busy, "rx_time": rx,
                             "tx_time": tx}
        self.reporter.poll(self.api)
        return self.rows()["radio"]

    def test_transmit_airtime_from_survey_counters(self):
        self.radio.survey_step = dict.fromkeys(self.radio.survey_step, 0)
        radio = self.poll_survey(0, 3_600_000, 360_000, 36_000, 72_000)
        # First reading after start: only a baseline, never the average since the radio booted.
        self.assertEqual(radio["level"], OK, radio["message"])
        self.assertIsNone(radio["values"]["rf.tx_airtime_pct"])
        self.assertEqual(radio["values"]["health.metric.rf.tx_airtime_pct.state"], "unavailable")
        values = self.poll_survey(5, 3_605_000, 360_025, 36_002, 72_040)["values"]
        self.assertEqual((values["rf.tx_airtime_pct"], values["rf.rx_airtime_pct"],
                          values["rf.busy_pct"], values["rf.airtime_window_s"]),
                         (0.04, 0.5, 0.8, 5.0))
        # A window shorter than a second keeps the baseline instead of falling back to the
        # lifetime counters (which reported 1.01 % for a window that was half transmitting).
        values = self.poll_survey(5.9, 3_605_900, 360_025, 36_452, 72_040)["values"]
        self.assertIsNone(values["rf.tx_airtime_pct"])
        values = self.poll_survey(6.2, 3_606_200, 360_025, 36_602, 72_040)["values"]
        self.assertEqual((values["rf.tx_airtime_pct"], values["rf.airtime_window_s"]), (50.0, 1.2))
        # Counters restarted since the previous reading: what accumulated since then.
        values = self.poll_survey(11.2, 2000, 10, 20, 10)["values"]
        self.assertEqual((values["rf.tx_airtime_pct"], values["rf.busy_pct"],
                          values["rf.airtime_window_s"]), (1.0, 0.5, 2.0))
        # ... but not when they cover more time than has passed: no known window.
        values = self.poll_survey(12.5, 60_000, 0, 0)["values"]
        self.assertIsNone(values["rf.tx_airtime_pct"])
        values = self.poll_survey(17.5, 65_000, 50, 5)["values"]
        self.assertEqual((values["rf.tx_airtime_pct"], values["rf.airtime_window_s"]), (0.1, 5.0))

    def test_survey_without_the_method_is_unsupported(self):
        self.radio.fail["iwinfo.survey"] = 3  # method not found: older firmware
        self.reporter.poll(self.api)
        radio = self.rows()["radio"]
        self.assertEqual(radio["level"], OK, radio["message"])
        self.assertEqual(radio["values"]["health.metric.rf.tx_airtime_pct.state"], "unsupported")
        reads = len(self.radio.calls)
        self.reporter.poll(self.api)
        self.assertNotIn(("iwinfo", "survey"), [c[:2] for c in self.radio.calls[reads:]])
        self.radio.fail["iwinfo.survey"] = 7  # other failures are a temporary gap
        self.radio.reboot()
        self.reporter.poll(self.api)
        self.assertIn("iwinfo.survey: timeout", self.rows()["radio"]["message"])

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
        self.radio.fail["iwinfo.assoclist"] = 7  # and the station table: no peer source at all
        self.clock.now = 5
        self.reporter.poll(self.api)
        rows = self.rows()
        radio = rows["radio"]
        self.assertEqual(radio["level"], WARN)
        self.assertIn("measurement unavailable", radio["message"])
        self.assertIsNone(radio["values"]["rf.noise_peak_dbm"])
        self.assertEqual(radio["values"]["health.metric.rf.noise_peak_dbm.state"], "unavailable")
        self.assertEqual(radio["values"]["rf.noise_dbm"], -94)  # the driver's own reading
        self.assertEqual(radio["values"]["rf.tx_power_dbm"], 32)  # from another call: still there
        links = rows["links"]
        self.assertEqual(links["values"]["health.availability"], "retrying")
        self.assertEqual(links["values"]["health.sample_id"], links_before)
        self.assertIn("not found", links["values"]["health.error"])

        del self.radio.fail["iwinfo.assoclist"]  # the station table alone still lists peers
        self.radio.stations = [{"mac": PEER, "signal": -60}]
        self.clock.now = 6
        self.reporter.poll(self.api)
        rows = self.rows()
        links = rows["links"]
        self.assertEqual((links["level"], links["values"]["health.availability"]),
                         (WARN, "current"))
        self.assertIn("measurement unavailable: file.read", links["message"])
        self.assertEqual((links["values"]["links.stations"], links["values"]["links.mesh_nodes"]),
                         (1, None))
        self.assertEqual(links["values"]["health.metric.links.mesh_nodes.state"], "unavailable")
        self.assertTrue(rows[f"link/{PEER.lower()}"]["values"]["link.present"])

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

        self.radio.noise = None  # no noise floor anywhere: no derived SNR
        self.radio.driver_noise = 0
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


class CharacterizationTests(unittest.TestCase):
    """What a network characterization flight needs from the poll: current (not repeated) link
    data, the kernel's station table, the reverse link, settings, names and read times."""
    setUp, make_controller, rows = HealthTests.setUp, HealthTests.make_controller, HealthTests.rows

    def test_a_link_report_that_stops_changing_is_not_presented_as_new(self):
        self.radio.fail["iwinfo.assoclist"] = 3  # link report only (older firmware)
        self.reporter.poll(self.api)
        first = self.rows()["links"]["values"]
        self.assertEqual(first["links.report_age_s"], 0)
        self.radio.linkstate_stopped = True
        self.clock.now = 1  # read again before the radio's 2 s rewrite would have happened
        self.reporter.poll(self.api)  # the same report: no new sample, its age keeps growing
        held = self.rows()["links"]["values"]
        self.assertEqual(held["health.sample_id"], first["health.sample_id"])
        self.assertEqual((held["health.sample_age_s"], held["health.availability"]), (1, "current"))
        self.clock.now = 8  # far past the 2 s rewrite interval: the radio's service stopped
        self.reporter.poll(self.api)
        rows = self.rows()
        links = rows["links"]["values"]
        self.assertEqual(links["health.availability"], "retrying")
        self.assertIn("not updated for 8 s", links["health.error"])
        radio = rows["radio"]
        self.assertEqual(radio["level"], WARN)
        self.assertIsNone(radio["values"]["rf.noise_peak_dbm"])  # not current: not published
        self.radio.linkstate_stopped = False
        self.clock.now = 9
        self.reporter.poll(self.api)
        links = self.rows()["links"]["values"]
        self.assertEqual((links["health.availability"], links["links.report_age_s"]),
                         ("current", 0))

    def test_a_stale_link_report_beside_a_live_station_table(self):
        self.radio.stations = [{"mac": PEER, "signal": -60}]
        self.radio.mesh_stats = [{"orig_address": "00:30:1a:00:00:02", "tq": 200}]
        self.reporter.poll(self.api)
        self.radio.linkstate_stopped = True
        self.clock.now = 4
        self.reporter.poll(self.api)  # station table is new: a new sample, report age visible
        links = self.rows()["links"]
        self.assertEqual((links["level"], links["values"]["links.report_age_s"]), (OK, 4))
        self.clock.now = 10
        self.reporter.poll(self.api)
        rows = self.rows()
        links = rows["links"]
        self.assertEqual(links["level"], WARN)
        self.assertIn("not updated for 10 s", links["message"])
        self.assertTrue(rows[f"link/{PEER.lower()}"]["values"]["link.present"])
        mesh = rows["mesh/00:30:1a:00:00:02"]["values"]  # unknown, not departed
        self.assertIsNone(mesh["mesh.present"])
        self.assertEqual(mesh["health.metric.mesh.present.state"], "unavailable")

    def test_station_table_supplies_signal_rate_and_counters(self):
        self.radio.sta_stats = [{"mac": PEER, "rssi": -61, "pl_ratio": 3},
                                {"mac": "00:30:1a:00:00:03", "rssi": -70, "snr": 21}]
        self.radio.stations = [
            {"mac": PEER, "signal": -63, "signal_avg": -62, "noise": -95, "inactive": 120,
             "connected_time": 300, "thr": 50000, "mesh plink": "ESTAB",
             "rx": {"rate": 65000, "mcs": 7, "40mhz": False, "packets": 1000, "bytes": 120000,
                    "drop_misc": 0},
             "tx": {"rate": 58500, "mcs": 6, "40mhz": False, "packets": 900, "bytes": 100000,
                    "retries": 30, "failed": 2}},
            {"mac": "00:30:1A:00:00:03", "signal": -72, "noise": 0, "tx": {"rate": 0}},
            {"mac": "00:30:1A:00:00:04", "signal": -80, "tx": {"rate": 6500}}]
        self.reporter.poll(self.api)
        rows = self.rows()
        near = rows[f"link/{PEER.lower()}"]["values"]
        self.assertEqual((near["link.signal_dbm"], near["link.snr_db"], near["link.rate_mbps"]),
                         (-62, 33, 58.5))  # the kernel's average signal, its own noise reading
        self.assertEqual((near["link.iw_tx_retries"], near["link.iw_tx_failed"],
                          near["link.iw_rx_bytes"], near["link.iw_mesh_plink"],
                          near["link.iw_inactive"]), (30, 2, 120000, "ESTAB", 120))
        self.assertEqual((near["link.rssi"], near["link.loss_pct"]), (-61, 3))  # vendor fields stay
        reported = rows["link/00:30:1a:00:00:03"]["values"]
        self.assertEqual(reported["link.snr_db"], 21)  # the radio's own SNR beats a derived one
        self.assertEqual(reported["link.signal_dbm"], -72)
        self.assertNotIn("link.rate_mbps", reported)  # 0 kbit/s is no rate
        only_table = rows["link/00:30:1a:00:00:04"]["values"]
        self.assertEqual((only_table["link.present"], only_table["link.rate_mbps"],
                          only_table["link.snr_db"]), (True, 6.5, 14))
        links = rows["links"]["values"]
        self.assertEqual((links["links.stations"], links["links.associated"]), (2, 3))

    def test_station_table_missing_from_firmware_is_not_asked_again(self):
        self.radio.fail["iwinfo.assoclist"] = 3
        self.reporter.poll(self.api)
        self.assertEqual(self.rows()["links"]["level"], OK)
        self.assertNotIn("links.associated", self.rows()["links"]["values"])
        reads = len(self.radio.calls)
        self.clock.now = 5
        self.reporter.poll(self.api)
        self.assertNotIn(("iwinfo", "assoclist"), [c[:2] for c in self.radio.calls[reads:]])

    def test_reverse_link_from_per_station_files(self):
        self.reporter = HealthReporter("mesh", settings={
            "peers": {"00:30:1a:00:00:09": "gcs"}, "reverse_link_peers": 2}, clock=self.clock)
        self.radio.stations = [{"mac": m, "signal": -60} for m in
                               ("00:30:1A:00:00:01", "00:30:1A:00:00:02", "00:30:1A:00:00:09")]
        self.radio.station_files = {
            "00:30:1a:00:00:09": {"fixed_txpower": "24\n", "peer_signal_reply": "-71 30\n"},
            "00:30:1a:00:00:01": {"fixed_txpower": "255\n", "peer_signal_reply": "-50 19\n"}}
        self.reporter.poll(self.api)
        rows = self.rows()
        gcs = rows["link/00:30:1a:00:00:09"]["values"]  # the named peer is read first
        self.assertEqual((gcs["link.apc_limited"], gcs["link.apc_limit_dbm"],
                          gcs["link.peer_signal_reply"]), (True, 24, "-71 30"))
        other = rows["link/00:30:1a:00:00:01"]["values"]
        self.assertEqual(other["link.apc_limited"], False)
        self.assertNotIn("link.apc_limit_dbm", other)
        self.assertNotIn("link.apc_limited", rows["link/00:30:1a:00:00:02"]["values"])  # over 2

    def test_station_files_missing_for_every_peer_become_unsupported(self):
        self.radio.stations = [{"mac": PEER, "signal": -60}]
        for at in range(3):
            self.clock.now = at
            self.reporter.poll(self.api)
        reads = len(self.radio.calls)
        self.clock.now = 4
        self.reporter.poll(self.api)
        self.assertFalse(any("stations/" in str(c) for c in self.radio.calls[reads:]))
        self.assertEqual(self.rows()["radio"]["level"], OK)

    def test_radio_settings_are_published(self):
        self.reporter.poll(self.api)
        values = self.rows()["radio"]["values"]
        self.assertEqual((values["rf.country"], values["rf.htmode"], values["rf.distance_m"],
                          values["rf.saved_tx_power_dbm"]), ("US", "HT20", 1000, 30))
        self.assertNotIn("rf.tx_antennas", values)  # not set on this radio: not invented

    def test_peers_carry_their_configured_names(self):
        self.reporter = HealthReporter("mesh", settings={"peers": {PEER: "gcs"}},
                                       clock=self.clock)
        self.radio.stations = [{"mac": PEER, "signal": -60}]
        self.reporter.poll(self.api)
        link = self.rows()[f"link/{PEER.lower()}"]
        self.assertEqual((link["message"], link["values"]["link.name"]), ("gcs: reported", "gcs"))
        self.radio.stations = []
        self.clock.now = 7
        self.reporter.poll(self.api)
        link = self.rows()[f"link/{PEER.lower()}"]
        self.assertEqual((link["message"], link["values"]["link.name"]),
                         ("gcs: not reported for 7 s", "gcs"))

    def test_long_or_invalid_nested_fields_are_flagged_not_cut(self):
        self.radio.sta_stats = [{"mac": PEER, "path": ["00:30:1a:00:00:0%d" % (i % 10)
                                                       for i in range(200)],
                                 "note": "x" * 3000, "short": [1, 2]}]
        self.reporter.poll(self.api)
        values = self.rows()[f"link/{PEER.lower()}"]["values"]
        self.assertNotIn("link.path", values)
        self.assertTrue(values["link.path_truncated"])
        self.assertEqual((len(values["link.note"]), values["link.note_truncated"]), (2048, True))
        self.assertEqual(values["link.short"], "[1, 2]")

    def test_an_entry_with_several_addresses_is_not_guessed(self):
        self.radio.mesh_stats = [{"dest": "00:30:1a:00:00:05", "next_hop": "00:30:1a:00:00:06"},
                                 {"dest": "00:30:1a:00:00:07", "tq": 100}]
        self.reporter.poll(self.api)
        rows = self.rows()
        self.assertEqual(rows["links"]["values"]["links.unidentified"], 1)
        self.assertIn("mesh/00:30:1a:00:00:07", rows)
        self.assertNotIn("mesh/00:30:1a:00:00:06", rows)

    def test_each_group_is_stamped_when_its_reads_returned(self):
        transport = self.radio

        def slow(payload, timeout=None):
            self.clock.now += 0.25  # every radio request takes a quarter of a second
            return transport(payload, timeout)

        api = RadioAPI("fake", "user", "fixture-password", transport=slow)
        revision = self.reporter.revision
        self.reporter.poll(api)
        self.assertEqual(self.reporter.revision, revision + 3)
        rows = self.rows()
        ages = {name: rows[name]["values"]["health.sample_age_s"]
                for name in ("system", "radio", "links")}
        self.assertGreater(ages["system"], ages["radio"])  # read first: oldest
        self.assertGreater(ages["radio"], ages["links"])
        self.assertGreater(ages["links"], 0)  # not stamped at the end of the pass


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


class PollLoopTests(unittest.TestCase):
    """The poll schedule: fixed rate, no catch-up after an overrun, publish after new data."""

    def run_loop(self, durations, interval=1.0, new=lambda index: True):
        clock = FakeClock()
        waits, published = [], []

        class Reporter:
            revision, calls = 0, 0

            def poll(self, _radio):
                clock.now += durations[self.calls]
                if new(self.calls):
                    self.revision += 1
                self.calls += 1

        reporter = Reporter()

        class Stop:
            @staticmethod
            def is_set():
                return reporter.calls >= len(durations)

            @staticmethod
            def wait(seconds):
                waits.append(round(seconds, 3))
                clock.now += seconds

        health_ros.poll_loop(reporter, None, Stop, interval, lambda: published.append(clock.now),
                             clock=clock)
        return waits, published

    def test_slow_polls_do_not_stretch_the_period(self):
        waits, _ = self.run_loop([0.3, 0.6, 0.1])
        self.assertEqual(waits, [0.7, 0.4, 0.9])  # polls start at 0, 1 and 2 s

    def test_an_overrun_is_followed_at_once_without_catching_up(self):
        waits, _ = self.run_loop([2.5, 0.2, 0.2])
        self.assertEqual(waits, [0, 0.8, 0.8])

    def test_publishes_after_polls_with_new_observations_only(self):
        _, published = self.run_loop([0.2, 0.2, 0.2], new=lambda index: index != 1)
        self.assertEqual(published, [0.2, 2.2])


class RosoutForwarderTests(unittest.TestCase):
    class Logger:
        def __init__(self):
            self.lines = []

        def __getattr__(self, level):
            return lambda message: self.lines.append((level, message))

    def test_records_before_ros_starts_are_sent_once_it_has(self):
        forwarder = health_ros.RosoutForwarder(capacity=2)
        log = logging.getLogger("doodle.test-rosout")
        log.addHandler(forwarder)
        self.addCleanup(log.removeHandler, forwarder)
        log.propagate = False
        self.addCleanup(setattr, log, "propagate", True)
        log.setLevel(logging.INFO)  # as the supervisor's logging.basicConfig sets it
        self.addCleanup(log.setLevel, logging.NOTSET)
        for text in ("dropped: over capacity", "radio rebooted", "setting 23 dBm"):
            log.warning(text)
        ros = self.Logger()
        forwarder.attach(ros)
        log.error("shutdown failed")
        log.info("recovered")
        forwarder.detach()
        log.info("after ROS stopped")
        self.assertEqual(ros.lines, [
            ("warning", "(before ROS started) doodle.test-rosout: radio rebooted"),
            ("warning", "(before ROS started) doodle.test-rosout: setting 23 dBm"),
            ("error", "doodle.test-rosout: shutdown failed"),
            ("info", "doodle.test-rosout: recovered")])

    def test_a_failing_ros_logger_never_raises(self):
        forwarder = health_ros.RosoutForwarder()

        class Broken:
            def info(self, message):
                raise RuntimeError("rosout publisher gone")

        forwarder.attach(Broken())
        forwarder.emit(logging.LogRecord("doodle", logging.INFO, __file__, 1, "x", (), None))


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
