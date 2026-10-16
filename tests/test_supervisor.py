"""Power control against the simulated radio: targets, recovery, shutdown, requests."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker/entrypoints"))
sys.path.insert(0, str(ROOT / "tests"))
import doodle_config  # noqa: E402
import supervisor  # noqa: E402
from radio_api import RadioAPI  # noqa: E402
from fake_radio import FakeRadio  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target_file = Path(self.tmp.name, "target.json")
        self.radio = FakeRadio()
        self.clock = FakeClock()

    def controller(self, **tx_power):
        settings = doodle_config.tx_power(tx_power)
        stop = threading.Event()
        # Pauses advance the fake clock instead of waiting.
        stop.wait = lambda seconds: (self.clock.sleep(seconds), stop.is_set())[1]
        return supervisor.Controller(RadioAPI("fake", "user", "fixture-password",
                                              transport=self.radio),
                                     settings, self.target_file, clock=self.clock,
                                     stop_event=stop, sleep=self.clock.sleep)

    def test_unchanged_only_observes(self):
        controller = self.controller()
        snapshot = controller.reconcile()
        self.assertTrue(snapshot["converged"])
        self.assertEqual((snapshot["rf"], snapshot["tx_power_dbm"], snapshot["range_dbm"]),
                         ("on", 32, [19, 32]))
        controller.status()
        self.assertEqual(self.radio.writes(), [])

    def test_ready_is_cached_even_while_reconciliation_holds_lock(self):
        controller = self.controller()
        self.assertFalse(controller.ready()['reachable'])
        controller.reconcile()
        with controller.lock, patch.object(controller, 'observe', side_effect=AssertionError('network IO')):
            result = controller.ready()
        self.assertTrue(result['reachable'])
        self.assertTrue(result['busy'])
        self.assertTrue(result['converged'])

    def test_startup_level_is_applied_and_read_back(self):
        snapshot = self.controller(startup="medium").reconcile()
        self.assertEqual((snapshot["target_dbm"], snapshot["tx_power_dbm"]), (27, 27))
        self.assertTrue(snapshot["converged"])
        self.assertEqual(len(self.radio.writes()), 1)

    def test_saved_runtime_request_beats_the_configured_startup(self):
        self.target_file.write_text(json.dumps({"target": 21}))
        self.assertEqual(self.controller(startup="medium").reconcile()["tx_power_dbm"], 21)
        self.target_file.write_text("not json")
        self.assertEqual(self.controller(startup="medium").target, "medium")

    def test_reboot_wifi_restart_and_rf_toggle_are_corrected(self):
        controller = self.controller(startup="low")
        controller.reconcile()
        self.assertEqual(self.radio.txpower, 23)
        for disturb in (self.radio.reboot, self.radio.wifi_restart):
            disturb()
            self.assertEqual(self.radio.txpower, 32)
            self.assertTrue(controller.reconcile()["converged"])
            self.assertEqual(self.radio.txpower, 23)
        self.radio.rf_up = False  # someone turned RF off behind our back
        self.assertTrue(controller.reconcile()["converged"])
        self.assertEqual((self.radio.rf_up, self.radio.txpower), (True, 23))

    def test_off_then_back_on_reapplies_power(self):
        controller = self.controller(startup="standby")
        controller.reconcile()
        self.assertEqual(controller.request("off")["rf"], "off")
        self.assertFalse(self.radio.rf_up)
        self.radio.rf_up_after = 3  # RF takes a few polls to come back
        snapshot = controller.request("high")
        self.assertEqual((snapshot["rf"], snapshot["tx_power_dbm"]), ("on", 32))
        snapshot = controller.request("standby")
        self.assertEqual(self.radio.txpower, 19)

    def test_requests_persist_and_startup_reverts(self):
        controller = self.controller(startup="low")
        controller.request("25")
        self.assertEqual(json.loads(self.target_file.read_text()), {"target": 25})
        self.assertEqual(self.controller(startup="low").target, 25)  # survives a restart
        controller.request("startup")
        self.assertFalse(self.target_file.exists())
        self.assertEqual(self.radio.txpower, 23)

    def test_impossible_requests_are_rejected_and_not_saved(self):
        controller = self.controller(startup="low", max_dbm=30)
        controller.request("medium")
        for value in ("15", "31", "loud", "max_dbm"):
            with self.assertRaises((RuntimeError, ValueError), msg=value):
                controller.request(value)
            self.assertEqual(controller.target, "medium")
            self.assertEqual(json.loads(self.target_file.read_text()), {"target": "medium"})
        self.assertEqual(controller.request("max")["tx_power_dbm"], 30)

    def test_a_radio_that_does_not_apply_is_reported(self):
        self.radio.ignore_txpower = True
        snapshot = self.controller(startup="standby").reconcile()
        self.assertFalse(snapshot["converged"])
        self.assertIn("did not reach 19 dBm", snapshot["error"])

    def test_unreachable_radio_keeps_the_target_and_recovers(self):
        controller = self.controller(startup="low")
        self.radio.unreachable = True
        snapshot = controller.reconcile()
        self.assertFalse(snapshot["converged"])
        self.assertIn("unreachable", snapshot["error"])
        with self.assertRaisesRegex(Exception, "unreachable"):
            controller.request("medium")
        self.assertEqual(controller.target, "medium")  # accepted; applied once reachable
        self.radio.unreachable = False
        self.assertTrue(controller.reconcile()["converged"])
        self.assertEqual(self.radio.txpower, 27)

    def test_missing_permissions_are_named(self):
        self.radio.acls = {"ubus": {"*": ["*"]}, "file": {}}
        snapshot = self.controller(startup="low").reconcile()
        self.assertIn("lacks permission for: file /usr/sbin/iw exec", snapshot["error"])
        self.assertEqual(self.radio.writes(), [])

    def test_shutdown_applies_policy_despite_stop_and_keeps_runtime_target(self):
        controller = self.controller(startup="high", shutdown="standby")
        controller.request("medium")
        controller.shutdown()
        self.assertTrue(controller.stop_event.is_set())
        self.assertEqual(self.radio.txpower, 19)
        self.assertEqual(json.loads(self.target_file.read_text()), {"target": "medium"})
        off = self.controller(startup="high", shutdown=False)
        off.shutdown()
        self.assertFalse(self.radio.rf_up)

    def test_shutdown_unchanged_writes_nothing_and_failure_raises(self):
        self.controller(startup="unchanged").shutdown()
        self.assertEqual(self.radio.writes(), [])
        self.radio.ignore_txpower = True
        with self.assertRaisesRegex(Exception, "did not reach 19 dBm"):
            self.controller(shutdown="standby").shutdown()

    def test_requests_do_not_queue_forever_behind_a_change(self):
        controller = self.controller()
        controller.lock.acquire()
        try:
            with patch.object(supervisor, "LOCK_WAIT", 0.01):
                with self.assertRaisesRegex(RuntimeError, "in progress"):
                    controller.request("low")
            self.assertTrue(controller.status()["busy"])
        finally:
            controller.lock.release()


if __name__ == "__main__":
    unittest.main()
