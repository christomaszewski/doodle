"""Characterization mode without ROS: what a snapshot holds, that it only reads, that a GCS radio
out of reach costs one bounded request per poll, and how the supervisor hands it to ROS."""
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker/entrypoints"))
sys.path.insert(0, str(ROOT / "tests"))
import characterize  # noqa: E402
import doodle_config  # noqa: E402
import health_ros  # noqa: E402
from radio_api import LINKSTATE, RadioAPI  # noqa: E402
import supervisor  # noqa: E402
from fake_radio import FakeRadio, MESH_KEY  # noqa: E402

PEER = "00:30:1a:00:00:0b"
STATIONS = "/sys/kernel/debug/ieee80211/phy0/netdev:wlan0/stations"
PEER_FILES = {"peer_signal": "-61\n", "peer_signal_reply": "-63 25\n", "fixed_txpower": "255\n"}


class PeerRadio(FakeRadio):
    """FakeRadio plus what characterization reads beyond the diagnostics poll."""

    def __init__(self):
        super().__init__()
        self.sta_stats = [{"mac": PEER, "rssi": -61, "pl_ratio": 0}]

    def _method(self, obj, method, args):
        if (obj, method) == ("file", "exec") and \
                (args.get("params") or [])[2:] == ["station", "dump"]:
            return {"code": 0, "stdout": f"Station {PEER} (on wlan0)\n\tsignal: -61 dBm\n"}
        if (obj, method) == ("file", "list") and args.get("path") == STATIONS:
            return {"entries": [{"name": PEER, "type": "directory", "size": 0}]}
        path = args.get("path", "")
        if (obj, method) == ("file", "read") and path.startswith(f"{STATIONS}/{PEER}/"):
            return {"data": PEER_FILES[path.rsplit("/", 1)[1]]}
        return super()._method(obj, method, args)


def collector(**radios):
    apis = {role: RadioAPI("fake", "user", "fixture-password", transport=radio)
            for role, radio in radios.items()}
    return characterize.Collector(apis, 1.0, "mesh", "testcar",
                                  clock=lambda: 1_700_000_000_000_000_000)


def changes(radio):
    return [call for call in radio.calls
            if call[:2] in (("network.wireless", "up"), ("network.wireless", "down"),
                            ("message-system", "chswitch"), ("system", "reboot"))
            or (call[:2] == ("file", "exec") and (call[2].get("params") or [])[2:3] == ["set"])]


def strict(text):
    def refuse(constant):
        raise ValueError(f"not strict JSON: {constant}")
    return json.loads(text, parse_constant=refuse)


class SnapshotTests(unittest.TestCase):
    def test_a_snapshot_holds_what_both_radios_report(self):
        vehicle, gcs = PeerRadio(), PeerRadio()
        snapshots = collector(vehicle=vehicle, gcs=gcs)
        first, second = strict(snapshots.poll_json()), strict(snapshots.poll_json())
        self.assertEqual((first["schema"], first["instance"], first["vehicle_id"]),
                         (1, "mesh", "testcar"))
        self.assertEqual((first["sequence"], second["sequence"]), (1, 2))
        self.assertEqual(first["stamp_unix_ns"], 1_700_000_000_000_000_000)
        for role in ("vehicle", "gcs"):
            view = first["radios"][role]
            self.assertIs(view["rf_up"], True)
            self.assertEqual((view["wireless"]["frequency"], view["wireless"]["txpower"]),
                             (2450, 32))
            self.assertEqual(view["linkstate"]["oper_freq"], 2450)  # labels its own samples
            self.assertNotIn("sysinfo", view["linkstate"])  # the radio clock is wrong
            self.assertEqual(view["linkstate"]["sta_stats"][0]["rssi"], -61)
            self.assertIn("active_time", view["survey"])
            self.assertTrue(view["station_dump"].startswith(f"Station {PEER}"))
            self.assertEqual(view["peer_files"][PEER]["peer_signal_reply"], "-63 25")
        self.assertNotIn(MESH_KEY, snapshots.poll_json())

    def test_it_only_reads(self):
        vehicle, gcs = PeerRadio(), PeerRadio()
        snapshots = collector(vehicle=vehicle, gcs=gcs)
        for _ in range(3):
            snapshots.poll()
        self.assertEqual(changes(vehicle) + changes(gcs), [])

    def test_a_gcs_out_of_reach_costs_one_request_per_poll(self):
        gcs, attempts = FakeRadio(), []
        gcs.unreachable = True

        def transport(payload, timeout=None):
            attempts.append(payload)
            return gcs(payload, timeout)

        apis = {"vehicle": RadioAPI("fake", "user", "fixture-password", transport=PeerRadio()),
                "gcs": RadioAPI("fake", "user", "fixture-password", transport=transport)}
        snapshot = characterize.Collector(apis, 1.0).poll()
        self.assertIn("unreachable", snapshot["radios"]["gcs"]["unreachable"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(snapshot["radios"]["vehicle"]["wireless"]["frequency"], 2450)

    def test_a_failed_read_is_recorded_where_its_data_would_be(self):
        radio = FakeRadio()  # no station dump, no file list
        radio.unreadable.add(LINKSTATE)
        view = collector(vehicle=radio).poll()["radios"]["vehicle"]
        self.assertIn("unexpected command", view["station_dump"]["error"])
        self.assertIn("method not found", view["peer_files"]["error"])
        self.assertIn("not found", view["linkstate"]["error"])
        self.assertEqual(view["wireless"]["frequency"], 2450)  # the rest still arrives

    def test_rf_off_leaves_what_needs_rf_as_errors(self):
        radio = PeerRadio()
        radio.rf_up = False
        view = collector(vehicle=radio).poll()["radios"]["vehicle"]
        self.assertIs(view["rf_up"], False)
        self.assertIn("error", view["wireless"])
        self.assertIn("no frequency", view["survey"]["error"])

    def test_a_non_finite_number_becomes_null(self):
        radio = PeerRadio()
        radio.noise = float("nan")
        snapshot = strict(collector(vehicle=radio).poll_json())
        self.assertIsNone(snapshot["radios"]["vehicle"]["linkstate"]["noise"])


class WiringTests(unittest.TestCase):
    def config(self, characterization=None):
        return doodle_config.load({"name": "mesh", "characterization": characterization,
                                   "connection": {"ubus": {"host": "10.223.18.52",
                                                           "password": "Wiring-Pw"}}})

    def test_off_unless_enabled(self):
        self.assertIsNone(characterize.Collector.from_config(self.config(), "Wiring-Pw"))
        self.assertIsNone(characterize.Collector.from_config(
            self.config({"gcs": {"host": "10.223.18.60"}}), "Wiring-Pw"))

    def test_the_gcs_radio_is_reached_with_this_radios_password(self):
        built = characterize.Collector.from_config(
            self.config({"enabled": True, "poll_interval_s": 0.5,
                         "gcs": {"host": "10.223.18.60"}}), "Wiring-Pw", "mesh", "car")
        self.assertEqual(list(built.radios), ["vehicle", "gcs"])
        self.assertEqual([r.host for r in built.radios.values()], ["10.223.18.52", "10.223.18.60"])
        self.assertEqual({r._password for r in built.radios.values()}, {"Wiring-Pw"})
        self.assertEqual({r.timeout for r in built.radios.values()}, {characterize.TIMEOUT})
        self.assertEqual(built.interval, 0.5)

    def test_without_a_gcs_only_this_radio_is_read(self):
        built = characterize.Collector.from_config(self.config({"enabled": True}), "Wiring-Pw")
        self.assertEqual(list(built.radios), ["vehicle"])

    def test_the_supervisor_hands_the_collector_to_ros(self):
        with patch.dict("os.environ", {"DOODLE_DIAGNOSTICS": "on"}), \
                patch.object(health_ros, "ros_health") as ros:
            supervisor.health_monitor("controller", "config", "stop", "radio", collector="c")
        ros.assert_called_once_with("controller", "config", "stop", "radio", collector="c")

    def test_ros_health_gives_its_node_the_collector(self):
        seen = []
        controller = type("C", (), {"diagnostics": "off", "target": "unchanged"})()
        start = lambda node: seen.append(node.collector)  # noqa: E731
        with patch.object(health_ros.RosHealth, "start", start), \
                patch.object(health_ros.RosHealth, "stop"):
            with health_ros.ros_health(controller, {"health": doodle_config.health(None)},
                                       threading.Event(), None, collector="c"):
                pass
        self.assertEqual(seen, ["c"])


if __name__ == "__main__":
    unittest.main()
