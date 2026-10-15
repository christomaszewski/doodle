"""The supervisor process end to end: real HTTP to a simulated radio, the control socket, the
health check, SIGTERM shutdown, exit status, and logs free of secrets."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker/entrypoints"))
sys.path.insert(0, str(ROOT / "tests"))
import doodle_config  # noqa: E402
from fake_radio import FakeRadio, MESH_KEY, Server  # noqa: E402

ENTRYPOINTS = Path(os.environ.get("DOODLE_TEST_ENTRYPOINT_DIR", ROOT / "docker/entrypoints"))
SUPERVISOR = ENTRYPOINTS / "supervisor.py"


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="doodle-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.radio = FakeRadio()
        self.server = Server(self.radio).__enter__()
        self.addCleanup(lambda: self.server.__exit__(None, None, None))

    def start(self, **tx_power):
        tmp = Path(self.tmp.name)
        secret = tmp / "radio_password"  # what the launcher writes and compose mounts
        secret.write_text(self.radio.password)
        config = doodle_config.load({"name": "mesh", "tx_power": tx_power, "connection": {"ubus": {
            "host": "127.0.0.1", "scheme": "http", "port": self.server.server_address[1],
            "password": self.radio.password}}})
        self.env = dict(os.environ, DOODLE_CONFIG=doodle_config.runtime_json(config),
                        DOODLE_SECRET_FILE=str(secret),
                        DOODLE_CONTROL_SOCKET=str(tmp / "control.sock"),
                        DOODLE_TARGET_FILE=str(tmp / "target.json"))
        self.log = open(tmp / "supervisor.log", "w+")
        self.addCleanup(self.log.close)
        self.process = subprocess.Popen([sys.executable, str(SUPERVISOR), "serve"], env=self.env,
                                        stdout=self.log, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: self.process.poll() is None and self.process.kill())
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = self.ctl("status")
            if result.returncode == 0 and json.loads(result.stdout)["converged"]:
                return json.loads(result.stdout)
            time.sleep(0.2)
        self.fail(f"supervisor never converged: {self.output()}")

    def ctl(self, *args):
        return subprocess.run([sys.executable, str(SUPERVISOR), *args], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def output(self):
        self.log.flush()
        self.log.seek(0)
        return self.log.read()

    def stop(self):
        self.process.send_signal(signal.SIGTERM)
        return self.process.wait(timeout=60)

    def test_startup_requests_health_and_shutdown(self):
        status = self.start(startup="medium", shutdown="standby")
        self.assertEqual((status["tx_power_dbm"], status["range_dbm"]), (27, [19, 32]))
        result = self.ctl("status")
        self.assertIn('"converged": true', result.stdout)  # what `doodle-up up -d` waits for
        result = self.ctl("set", "low")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["tx_power_dbm"], 23)
        self.assertEqual(self.ctl("health").returncode, 0)
        rejected = self.ctl("set", "15")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("not supported", rejected.stderr)
        self.assertEqual(self.radio.txpower, 23)
        self.assertEqual(self.stop(), 0, self.output())
        self.assertEqual(self.radio.txpower, 19)
        log = self.output()
        self.assertIn("shutdown complete", log)
        self.assertNotIn(self.radio.password, log)
        self.assertNotIn(MESH_KEY, log)

    def test_unconfirmed_shutdown_exits_nonzero(self):
        self.start(startup="unchanged", shutdown="off")
        self.radio.unreachable = True  # the radio disappears before shutdown
        self.assertNotEqual(self.stop(), 0)
        self.assertIn("radio power is NOT confirmed", self.output())

    def test_unhealthy_while_the_radio_cannot_reach_the_target(self):
        self.start(startup="unchanged")
        self.radio.ignore_txpower = True
        result = self.ctl("set", "standby")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.ctl("health").returncode, 1)
        self.assertEqual(self.stop(), 0)


if __name__ == "__main__":
    unittest.main()
