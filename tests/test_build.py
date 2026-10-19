"""Exercise the real build entrypoint with a Docker stub; no image builds or pushes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "a" * 64
IMAGE = "registry.test:5000/doodle_radio:scope-test"
DOCKER = '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["DOCKER_ARGV_LOG"], "a") as out:
    out.write(json.dumps(args) + "\\n")
if os.environ.get("FAIL_BUILD"):
    sys.exit(17)
if "--metadata-file" in args:
    metadata = {} if os.environ.get("NO_DIGEST") else {"containerimage.digest": "sha256:" + "a" * 64}
    Path(args[args.index("--metadata-file") + 1]).write_text(json.dumps(metadata))
'''


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="doodle build ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("tools/build_image.sh", "tools/rig-build.sh"):
            target = self.root / name
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(ROOT / name, target)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "docker").write_text(f"#!{sys.executable}\n" + DOCKER)
        (bin_dir / "docker").chmod(0o755)
        # The helper invokes python3 to write the result report.
        (bin_dir / "python3").symlink_to(sys.executable)
        self.log = self.root / "argv.jsonl"
        self.report = self.root / "result.jsonl"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("RIG_", "DOODLE_", "DOCKER_"))
                    and k not in {"ROS_DISTRO", "PUSH", "FAIL_BUILD", "NO_DIGEST"}}
        self.env.update(PATH=f"{bin_dir}:{self.env['PATH']}", DOCKER_ARGV_LOG=str(self.log),
                        TMPDIR=str(self.root))

    def build(self, **env):
        return subprocess.run([str(self.root / "tools/build_image.sh"),
                               "registry.test:5000", "scope-test"],
                              cwd=self.root, env=dict(self.env, **env), capture_output=True,
                              text=True, timeout=15)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_standalone_native_and_parent_defaults(self):
        result = self.build()
        self.assertEqual(result.returncode, 0, result.stderr)
        build, push = self.calls()
        self.assertEqual(build[:3], ["build", "-t", IMAGE])
        self.assertIn("ROS_DISTRO=lyrical", build)
        self.assertIn("BASE_IMAGE=ros:lyrical-ros-core", build)
        self.assertEqual(build[-1], str(self.root))
        self.assertFalse(set(build) & {"--platform", "--no-cache", "--pull"})
        self.assertEqual(push, ["push", IMAGE])

    def test_standalone_cpu_override_and_no_push(self):
        result = self.build(DOODLE_BUILD_PLATFORM="linux/amd64", PUSH="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        build, = self.calls()
        self.assertEqual(build[build.index("--platform") + 1], "linux/amd64")
        self.assertNotIn("pushed", result.stderr)

    def test_scoped_cpus_override_standalone_and_report_published_identity(self):
        for platforms in ("linux/arm64", "linux/amd64", "linux/arm64,linux/amd64"):
            with self.subTest(platforms=platforms):
                self.log.unlink(missing_ok=True)
                self.report.unlink(missing_ok=True)
                result = self.build(RIG_BUILD_PLATFORMS=platforms,
                                    DOODLE_BUILD_PLATFORM="linux/unsupported",
                                    RIG_BUILD_RESULT=str(self.report),
                                    RIG_BASE_IMAGE=f"registry.test:5000/base@{DIGEST}",
                                    ROS_DISTRO="lyrical")
                self.assertEqual(result.returncode, 0, result.stderr)
                build, = self.calls()
                self.assertEqual(build[:2], ["buildx", "build"])
                self.assertEqual(build.count("--platform"), 1)
                self.assertEqual(build[build.index("--platform") + 1], platforms)
                self.assertIn("--push", build)
                self.assertIn(f"BASE_IMAGE=registry.test:5000/base@{DIGEST}", build)
                self.assertEqual(json.loads(self.report.read_text()), {"ref": IMAGE, "digest": DIGEST})
                self.assertFalse(Path(build[build.index("--metadata-file") + 1]).exists())

    def test_no_cache_and_parent_pull_policy(self):
        for platforms in ("", "linux/arm64,linux/amd64"):
            for pull in ("0", "1"):
                with self.subTest(platforms=platforms, pull=pull):
                    self.log.unlink(missing_ok=True)
                    result = self.build(RIG_BUILD_PLATFORMS=platforms, RIG_BUILD_NO_CACHE="1",
                                        RIG_BUILD_PULL=pull, ROS_DISTRO="testdistro")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    build = self.calls()[0]
                    self.assertIn("--no-cache", build)
                    self.assertEqual("--pull" in build, pull == "1")
                    self.assertIn("ROS_DISTRO=testdistro", build)
                    self.assertIn("BASE_IMAGE=ros:testdistro-ros-core", build)

    def test_load_requires_one_cpu(self):
        result = self.build(RIG_BUILD_PLATFORMS="linux/arm64,linux/amd64", PUSH="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("multiarch builds require PUSH=1", result.stderr)
        self.assertEqual(self.calls(), [])
        result = self.build(RIG_BUILD_PLATFORMS="linux/arm64", PUSH="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        build, = self.calls()
        self.assertIn("--load", build)
        self.assertNotIn("--push", build)

    def test_failed_build_or_missing_digest_cannot_report_success(self):
        for failure in ({"FAIL_BUILD": "1"}, {"NO_DIGEST": "1"}):
            with self.subTest(failure=failure):
                result = self.build(RIG_BUILD_PLATFORMS="linux/arm64",
                                    RIG_BUILD_RESULT=str(self.report), **failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.report.exists())
                self.assertFalse(list(self.root.glob("rig-build.*")))

    def test_result_report_appends(self):
        prior = {"ref": "registry.test:5000/base:scope-test", "digest": DIGEST}
        self.report.write_text(json.dumps(prior) + "\n")
        result = self.build(RIG_BUILD_PLATFORMS="linux/amd64", RIG_BUILD_RESULT=str(self.report))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([json.loads(line) for line in self.report.read_text().splitlines()],
                         [prior, {"ref": IMAGE, "digest": DIGEST}])


if __name__ == "__main__":
    unittest.main()
