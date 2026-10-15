"""The installed supervisor with real ROS: /diagnostics arrive, the tx_power parameter changes the
radio, refuses off, and follows changes made through the control socket. Needs rclpy (CI runs it
inside the runtime image); skipped elsewhere."""
import importlib.util
import json
import os
from pathlib import Path
import random
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


@unittest.skipUnless(importlib.util.find_spec("rclpy"), "requires ROS 2 (runs in the image)")
class RosHealthTests(unittest.TestCase):
    def setUp(self):
        import rclpy
        from rclpy.context import Context
        from rclpy.signals import SignalHandlerOptions

        # One domain per run and localhost-only discovery: no multicast needed, no crosstalk.
        domain = str(random.randint(100, 200))
        os.environ.update(ROS_DOMAIN_ID=domain, ROS_AUTOMATIC_DISCOVERY_RANGE="LOCALHOST")
        self.tmp = tempfile.TemporaryDirectory(prefix="doodle-ros-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.radio = FakeRadio()
        self.server = Server(self.radio).__enter__()
        self.addCleanup(lambda: self.server.__exit__(None, None, None))
        tmp = Path(self.tmp.name)
        (tmp / "radio_password").write_text(self.radio.password)
        config = doodle_config.load({
            "name": "mesh", "tx_power": {"startup": "medium", "shutdown": "standby"},
            "health": {"interval_s": 0.5, "poll_interval_s": 1},
            "connection": {"ubus": {"host": "127.0.0.1", "scheme": "http",
                                    "port": self.server.server_address[1],
                                    "password": self.radio.password}}})
        self.env = dict(os.environ, DOODLE_CONFIG=doodle_config.runtime_json(config),
                        DOODLE_NAME="mesh", DOODLE_NAMESPACE="/mesh", VEHICLE_ID="testcar",
                        DOODLE_SECRET_FILE=str(tmp / "radio_password"),
                        DOODLE_CONTROL_SOCKET=str(tmp / "control.sock"),
                        DOODLE_TARGET_FILE=str(tmp / "target.json"))
        self.env.pop("DOODLE_DIAGNOSTICS", None)
        self.log = open(tmp / "supervisor.log", "w+")
        self.addCleanup(self.log.close)
        self.process = subprocess.Popen([sys.executable, str(SUPERVISOR), "serve"], env=self.env,
                                        stdout=self.log, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: self.process.poll() is None and self.process.kill())

        self.context = Context()
        rclpy.init(context=self.context, signal_handler_options=SignalHandlerOptions.NO)
        self.addCleanup(self.context.try_shutdown)
        self.node = rclpy.create_node("doodle_test", context=self.context)
        self.addCleanup(self.node.destroy_node)
        from rclpy.executors import SingleThreadedExecutor
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)

    def output(self):
        self.log.flush()
        self.log.seek(0)
        return self.log.read()

    def ctl(self, *args):
        return subprocess.run([sys.executable, str(SUPERVISOR), *args], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def spin_until(self, condition, timeout, what):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.executor.spin_once(timeout_sec=0.1)
            result = condition()
            if result:
                return result
        self.fail(f"timed out waiting for {what}\n{self.output()}")

    def call(self, client, request, timeout=30):
        self.spin_until(lambda: client.service_is_ready(), 30, f"service {client.srv_name}")
        future = client.call_async(request)
        return self.spin_until(lambda: future.done() and future.result(), timeout,
                               f"reply from {client.srv_name}")

    def set_param(self, value):
        from rcl_interfaces.srv import SetParameters
        from rclpy.parameter import Parameter
        client = self.node.create_client(SetParameters, "/mesh/radio/set_parameters")
        request = SetParameters.Request()
        request.parameters = [Parameter("tx_power", value=value).to_parameter_msg()]
        return self.call(client, request).results[0]

    def get_param(self):
        from rcl_interfaces.srv import GetParameters
        from rclpy.parameter import parameter_value_to_python
        client = self.node.create_client(GetParameters, "/mesh/radio/get_parameters")
        request = GetParameters.Request(names=["tx_power"])
        return parameter_value_to_python(self.call(client, request).values[0])

    def test_diagnostics_and_parameter(self):
        from diagnostic_msgs.msg import DiagnosticArray
        arrays = []
        self.node.create_subscription(DiagnosticArray, "/diagnostics", arrays.append, 10)

        def converged():
            names = {}
            for array in arrays[-3:]:
                for row in array.status:
                    names[row.name] = row
            radio = names.get("mesh: radio")
            ready = (names.get("mesh: service") and radio
                     and {kv.key: kv.value for kv in radio.values}.get("rf.tx_power_dbm") == "27")
            return names if ready else None

        rows = self.spin_until(converged, 60, "diagnostics showing the startup power")
        self.assertEqual(sorted(rows), ["mesh: links", "mesh: radio", "mesh: service",
                                        "mesh: system", "mesh: temperature"])
        service = rows["mesh: service"]
        values = {kv.key: kv.value for kv in service.values}
        self.assertEqual(service.level, b"\x00")
        self.assertEqual((values["rf.target"], values["rf.converged"], values["health.vehicle_id"],
                          values["health.instance"]), ("medium", "true", "testcar", "mesh"))
        self.assertEqual(rows["mesh: temperature"].message, "unsupported by hardware")
        self.assertEqual(rows["mesh: radio"].hardware_id, "EXAMPLE-SUB smartradio-example")
        status = json.loads(self.ctl("status").stdout)
        self.assertEqual(status["diagnostics"], "publishing")
        self.assertEqual(self.ctl("health").returncode, 0)

        self.assertEqual(self.get_param(), "medium")
        result = self.set_param("low")
        self.assertTrue(result.successful, result.reason)
        self.assertEqual(self.radio.txpower, 23)  # applied before the reply
        result = self.set_param("off")
        self.assertFalse(result.successful)
        self.assertIn("refused over ROS", result.reason)
        self.assertTrue(self.radio.rf_up)
        self.assertFalse(self.set_param(15).successful)  # below the radio's range
        self.assertEqual(self.radio.txpower, 23)

        self.assertEqual(self.ctl("set", "25").returncode, 0)  # the launcher path
        self.spin_until(lambda: self.get_param() == 25, 15, "the parameter to follow 25 dBm")

        self.process.send_signal(signal.SIGTERM)
        self.assertEqual(self.process.wait(timeout=60), 0, self.output())
        self.assertEqual(self.radio.txpower, 19)  # shutdown power applied
        log = self.output()
        self.assertIn("publishing /diagnostics", log)
        self.assertNotIn(self.radio.password, log)
        self.assertNotIn(MESH_KEY, log)


if __name__ == "__main__":
    unittest.main()
