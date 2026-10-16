"""doodle-up: validation before compose, secret handling, tx-power, up -d and down contracts."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
FILES = ("doodle-up", "tools/render_config.py", "tools/wait_ready.py", "docker/entrypoints/doodle_config.py",
         "docker/compose/compose.deploy.yaml", "sensors/doodle.example.yaml")
PASSWORD = "Launcher-Test-Password-9"

FAKE_DOCKER = r'''#!/usr/bin/env bash
echo "docker $*" >> "$ARGV_LOG"
env >> "$ENV_LOG"
case "$*" in
  "compose version") exit 0 ;;
  "inspect --format"*) echo "${STOP_EXIT_CODE:-0}" ;;
  "logs --tail 40"*) echo 'shutdown log evidence' ;;
  *" ps -q radio"|*" ps -aq radio") [ "${RUNNING:-}" = 1 ] && echo container; exit 0 ;;
  *"radioctl.sh status"|*"radioctl.sh ready")
    if [ -n "${FAKE_STATUS:-}" ]; then printf '%s\n' "$FAKE_STATUS";
    else printf '%s\n' '{"converged": true}'; fi ;;
  *"radioctl.sh set "*) printf '{"set": "%s"}\n' "${@: -1}" ;;
esac
exit 0
'''


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in FILES:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        self.config = self.root / "radios/mesh.yaml"
        self.config.parent.mkdir()
        self.secret = self.root / "var/run/mesh.radio_password"
        self.write_config()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "docker").write_text(FAKE_DOCKER)
        (bin_dir / "docker").chmod(0o755)
        self.argv, self.envlog = self.root / "argv", self.root / "env"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("DOODLE_", "RIG_", "COMPOSE_"))}
        self.env.update(PATH=f"{bin_dir}:{self.env['PATH']}", ARGV_LOG=str(self.argv),
                        ENV_LOG=str(self.envlog), DOODLE_SETTLE_TIMEOUT="2", DOODLE_CONNECT_TIMEOUT="2")

    def write_config(self, password=PASSWORD, **overrides):
        doc = yaml.safe_load((ROOT / "sensors/doodle.example.yaml").read_text())
        doc["connection"]["ubus"]["password"] = password
        doc.update(overrides)
        self.config.write_text(yaml.safe_dump(doc))

    def run_launcher(self, *args, **env):
        return subprocess.run([str(self.root / "doodle-up"), str(self.config), *args],
                              env=dict(self.env, **env), capture_output=True, text=True,
                              timeout=30)

    def calls(self):
        return self.argv.read_text() if self.argv.exists() else ""

    def test_config_carries_the_secret_path_never_the_password(self):
        result = self.run_launcher("config")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")  # fake docker printed nothing; launcher chatter → stderr
        self.assertFalse(self.secret.exists())  # only creating a container writes it
        env = self.envlog.read_text()
        self.assertIn(f"DOODLE_PASSWORD_FILE={self.secret}", env)
        self.assertIn("COMPOSE_PROJECT_NAME=doodle_mesh", env)
        self.assertIn("DOODLE_NAMESPACE=/mesh", env)
        self.assertNotIn(PASSWORD, env)
        result = self.run_launcher("config", COMPOSE_PROJECT_NAME="rig_owned")
        self.assertIn("COMPOSE_PROJECT_NAME=rig_owned", self.envlog.read_text())
        self.assertNotIn(" -p ", self.calls())

    def test_invalid_config_never_reaches_compose(self):
        for overrides in ({"tx_power": {"startup": "loud"}}, {"name": "mesh; touch /tmp/x"},
                          {"connection": {"ubus": {"host": "10.223.18.52"}}}):
            self.write_config(**overrides)
            self.argv.unlink(missing_ok=True)
            result = self.run_launcher("up", "-d")
            self.assertNotEqual(result.returncode, 0, overrides)
            self.assertIn("render_config:", result.stderr)
            self.assertEqual(set(self.calls().splitlines()), {"docker compose version"})

    def test_up_writes_the_secret_privately_and_down_removes_it(self):
        result = self.run_launcher("up", "-d")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.secret.read_text(), PASSWORD)
        self.assertEqual(self.secret.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(PASSWORD, self.envlog.read_text())
        self.assertNotIn(PASSWORD, result.stdout + result.stderr)
        self.write_config(password="Rotated-Password-2")
        self.assertEqual(self.run_launcher("up", "-d").returncode, 0)
        self.assertEqual(self.secret.read_text(), "Rotated-Password-2")
        self.assertEqual(self.run_launcher("down").returncode, 0)
        self.assertFalse(self.secret.exists())

    def test_unfilled_rig_var_renders_but_never_starts(self):
        self.write_config(password="{{doodle_radio_password}}")
        self.assertEqual(self.run_launcher("config").returncode, 0)  # bake/certify still render
        result = self.run_launcher("up", "-d")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sudo rig provision --var doodle_radio_password=", result.stderr)
        self.assertNotIn(" up", self.calls())
        self.assertFalse(self.secret.exists())

    def test_tx_power_never_creates_a_container(self):
        result = self.run_launcher("tx-power")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["service"], "down")
        result = self.run_launcher("tx-power", "low")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is down", result.stderr)
        self.assertNotIn(" up", self.calls())
        self.assertNotIn(" exec", self.calls())
        self.assertEqual(self.run_launcher("tx-power", "low", "now").returncode, 2)

    def test_tx_power_talks_to_the_running_supervisor(self):
        result = self.run_launcher("tx-power", "low", RUNNING="1")
        self.assertEqual(json.loads(result.stdout), {"set": "low"})
        self.assertIn("exec -T radio /usr/local/bin/radioctl.sh set low", self.calls())
        result = self.run_launcher("tx-power", RUNNING="1")
        self.assertTrue(json.loads(result.stdout)["converged"])

    def test_up_detached_waits_for_the_target(self):
        result = self.run_launcher("up", "-d")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("radioctl.sh ready", self.calls())
        result = self.run_launcher("up", "-d", FAKE_STATUS='{"reachable": true, "converged": false, "error": "x"}')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not reach its target", result.stderr)

    def test_down_checks_the_exit_status_before_removing_the_container(self):
        result = self.run_launcher("down", RUNNING="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertLess(calls.index(" stop radio"), calls.index("inspect --format"))
        self.assertLess(calls.index("inspect --format"), calls.index(" down"))
        result = self.run_launcher("down", "--timeout", "15", RUNNING="1", STOP_EXIT_CODE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("radio power unconfirmed", result.stderr)
        self.assertIn("shutdown log evidence", result.stderr)
        self.assertIn(" stop --timeout 15 radio", self.calls())


@unittest.skipUnless(shutil.which("docker") and subprocess.run(
    ["docker", "compose", "version"], capture_output=True).returncode == 0,
    "needs the docker compose plugin (renders client-side; no daemon)")
class ComposeRenderTests(unittest.TestCase):
    def test_rendered_compose(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp, "mesh.yaml")
            doc = yaml.safe_load((ROOT / "sensors/doodle.example.yaml").read_text())
            doc["connection"]["ubus"]["password"] = PASSWORD
            doc["tx_power"] = {"startup": "medium", "shutdown": False}
            config.write_text(yaml.safe_dump(doc))
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("DOODLE_", "RIG_", "COMPOSE_"))}
            env.update(RIG_IMAGE_REGISTRY="registry.example:5000", RIG_IMAGE_TAG="v1",
                       ROS_DOMAIN_ID="7", RMW_IMPLEMENTATION="rmw_zenoh_cpp")
            result = subprocess.run([str(ROOT / "doodle-up"), str(config), "config"], env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(PASSWORD, result.stdout)
            compose = yaml.safe_load(result.stdout)
            self.assertEqual(compose["name"], "doodle_mesh")
            radio = compose["services"]["radio"]
            self.assertEqual(radio["image"], "registry.example:5000/doodle_radio:v1")
            self.assertEqual(radio["environment"]["ROS_DOMAIN_ID"], "7")
            self.assertEqual(radio["environment"]["RMW_IMPLEMENTATION"], "rmw_zenoh_cpp")
            runtime = json.loads(radio["environment"]["DOODLE_CONFIG"])
            self.assertEqual(runtime["tx_power"]["shutdown"], "off")
            self.assertEqual(compose["secrets"]["radio_password"]["file"],
                             str(ROOT / "var/run/mesh.radio_password"))
            self.assertNotIn(PASSWORD, json.dumps(radio["environment"]))


if __name__ == "__main__":
    unittest.main()
