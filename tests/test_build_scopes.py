"""Integration with Rig's planner. CI supplies RIG_SOURCE; service runtime does not depend on Rig."""
import dataclasses
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if os.environ.get("RIG_SOURCE"):
    sys.path.insert(0, os.environ["RIG_SOURCE"])
try:
    from rig_cli import RigError, matrix_build
    from rig_cli.descriptor import load_descriptor
    from rig_cli.manifest import Manifest, RosSettings, Sensor
except ModuleNotFoundError as exc:
    if os.environ.get("RIG_SOURCE") or exc.name != "rig_cli":
        raise
    matrix_build = None


@unittest.skipIf(matrix_build is None, "set RIG_SOURCE to a Rig v0.2.62+ checkout")
class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # A matrix service makes runtime/CPU inference and generic-image deduplication realistic.
        camera = self.root / "camera"
        camera.mkdir()
        (camera / "rigging.yaml").write_text('''service: camera
launcher: up
build:
  command: build.sh
  images: [camera]
  architectures: [arm64, amd64]
  capabilities: {platforms: true, no_cache: true}
  platforms: [jp6m, dev]
  variants:
    jp6m: {architectures: [arm64]}
replay: {platform: dev}
''')
        (camera / "build.sh").write_text("#!/bin/sh\nexit 0\n")
        self.descs = {"doodle": load_descriptor("doodle", ROOT),
                      "camera": load_descriptor("camera", camera)}
        self.manifest = Manifest(
            vehicle="test", ros=RosSettings(0, "rmw_zenoh_cpp", "lyrical"),
            sensors=[Sensor("radio", "doodle", ROOT / "sensors/doodle.example.yaml", True, 10),
                     Sensor("camera", "camera", camera / "config.yaml", True, 20)],
            platform="jp6m", arch="arm64", image_registry="registry.test:5000", image_tag="scope-test")

    def radio_tasks(self, scope=None, **kwargs):
        tasks = matrix_build.plan(self.manifest, self.descs, registry=self.manifest.image_registry,
                                  tag=None, scopes=scope, **kwargs)
        radio = [task for task in tasks if task.service == "doodle"]
        for task in radio:
            self.assertIsNone(task.variant)
            self.assertEqual(task.images, ["doodle_radio"])
            self.assertEqual(task.tag, "scope-test")
        return radio

    def test_full_matrix_keeps_both_cpus_even_for_disabled_radio(self):
        self.manifest.sensors[0] = dataclasses.replace(self.manifest.sensors[0], enabled=False)
        task, = self.radio_tasks()
        self.assertEqual(set(task.architectures), {"arm64", "amd64"})
        self.assertEqual(self.radio_tasks("deployment"), [])

    def test_deployment_selects_vehicle_cpu_with_optional_inference(self):
        for arch in ("arm64", None):
            self.manifest.arch = arch
            task, = self.radio_tasks("deployment")
            self.assertEqual(task.architectures, ["arm64"])

    def test_host_uses_provisioned_runtime_and_docker_cpu(self):
        machine = self.root / "machine.yaml"
        machine.write_text("platform: dev\n")
        with patch("rig_cli.manifest.machine_file", return_value=machine), \
                patch("rig_cli.image_catalog.docker_arch", return_value="amd64"):
            task, = self.radio_tasks("host")
        self.assertEqual(task.architectures, ["amd64"])

    def test_deployment_host_union_publishes_one_generic_index(self):
        machine = self.root / "machine.yaml"
        machine.write_text("platform: dev\n")
        for host_arch in ("arm64", "amd64"):
            with self.subTest(host_arch=host_arch), \
                    patch("rig_cli.manifest.machine_file", return_value=machine), \
                    patch("rig_cli.image_catalog.docker_arch", return_value=host_arch):
                task, = self.radio_tasks("deployment,host")
                self.assertEqual(set(task.architectures), {"arm64", host_arch})

    def test_replay_omits_live_radio_until_policy_opts_in(self):
        self.assertEqual(self.radio_tasks("replay"), [])
        task, = self.radio_tasks("deployment,replay")
        self.assertEqual(task.architectures, ["arm64"])
        for kind, name in (("services", "doodle"), ("instances", "radio")):
            self.manifest.replay_policy = {kind: {name: {"start": True}}}
            task, = self.radio_tasks("replay")
            self.assertEqual(set(task.architectures), {"arm64", "amd64"})

    def test_conflicting_host_provision_is_rejected(self):
        machine = self.root / "machine.yaml"
        machine.write_text("platform: dev\narch: arm64\n")
        with patch("rig_cli.manifest.machine_file", return_value=machine), \
                patch("rig_cli.image_catalog.docker_arch", return_value="amd64"):
            with self.assertRaisesRegex(RigError, "does not match Docker engine"):
                self.radio_tasks("host")


if __name__ == "__main__":
    unittest.main()
