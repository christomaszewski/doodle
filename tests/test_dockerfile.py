"""The runtime Dockerfile's build-cache and base-sharing properties, checked without building.

What the build itself proves is in CI (the image jobs). These are the properties a later edit can
quietly undo: the frontend line, the position of the layers shared with the other rig services,
the packages step that must not reach the network on the fleet base, and a build context that
holds only what is copied.
"""
from itertools import takewhile
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker/Dockerfile.runtime"
# The two instructions every rig ROS service runs first on the shared parent, byte for byte (the
# ouster service's docker/Dockerfile.runtime). On one builder they are then built once per parent.
HOLD = ("RUN dpkg-query -W -f='${Package} ${db:Status-Status}\\n' 'ros-*' \\\n"
        "      | awk '$2 == \"installed\" {print $1}' | xargs -r apt-mark hold")
POLICY = ("RUN printf 'APT::Install-Recommends \"0\";\\nAPT::Install-Suggests \"0\";\\n"
          "APT::Get::Upgrade \"false\";\\n' \\\n"
          "      > /etc/apt/apt.conf.d/99-rig-apt-policy")


def instructions(text):
    """[(keyword, full text)] with comments dropped and continuation lines joined."""
    found, current = [], []
    for line in text.splitlines():
        if not current and (not line.strip() or line.lstrip().startswith("#")):
            continue
        current.append(line)
        if not line.rstrip().endswith("\\"):
            found.append((current[0].split()[0].upper(), "\n".join(current)))
            current = []
    return found


class DockerfileTests(unittest.TestCase):
    def setUp(self):
        self.text = DOCKERFILE.read_text()
        self.steps = instructions(self.text)
        self.keywords = [keyword for keyword, _ in self.steps]

    def test_frontend_is_the_builders_own(self):
        # Parser directives only count in the leading comment block. A `# syntax=` there makes
        # every build, cached or not, resolve docker/dockerfile on Docker Hub first: a rate limit
        # or a disconnected builder (`rig build --offline`) then fails before any step runs.
        leading = takewhile(lambda line: line.startswith("#"), self.text.splitlines())
        self.assertFalse([line for line in leading if re.match(r"#\s*syntax\s*=", line)])

    def test_one_stage_on_the_parent_rig_passes(self):
        froms = [text for keyword, text in self.steps if keyword == "FROM"]
        self.assertEqual(froms, ["FROM ${BASE_IMAGE}"])
        self.assertIn("ARG BASE_IMAGE=ros:${ROS_DISTRO}-ros-core", self.text)  # standalone parent

    def test_the_layers_shared_with_other_services_keep_their_cache_key(self):
        """A RUN's cache key covers the instruction text and every ARG and ENV in scope before
        it. So the order after FROM is part of the contract with the other services."""
        after_from = self.steps[self.keywords.index("FROM") + 1:]
        self.assertEqual([text for _, text in after_from[:5]], [
            'SHELL ["/bin/bash", "-o", "pipefail", "-c"]',
            HOLD,                                   # nothing but SHELL before it
            "ARG ROS_DISTRO",
            "ENV DEBIAN_FRONTEND=noninteractive",
            POLICY,
        ])

    def test_packages_install_only_when_the_parent_lacks_them(self):
        runs = [text for keyword, text in self.steps if keyword == "RUN"]
        installs = [text for text in runs if "apt-get" in text]
        self.assertEqual(len(installs), 1)
        step = installs[0]
        # Behind a check of what is installed, so the fleet base needs no network at all.
        self.assertLess(step.index("dpkg-query"), step.index("apt-get update"))
        self.assertRegex(step, r'if \[ -n "\$\{missing\}" \]; then')
        self.assertIn("--no-install-recommends --no-upgrade", step)  # contract M5
        self.assertIn("rm -rf /var/lib/apt/lists/*", step)
        for package in ("rmw-fastrtps-cpp", "rmw-zenoh-cpp", "diagnostic-msgs", "std-msgs"):
            self.assertIn("ros-${ROS_DISTRO}-" + package, step)
        self.assertNotIn("--mount=type=cache", self.text)  # builder caches stay out of the image

    def test_the_services_own_files_come_last(self):
        last_install = max(i for i, (_, text) in enumerate(self.steps) if "apt-get" in text)
        first_copy = self.keywords.index("COPY")
        self.assertGreater(first_copy, last_install)  # a code change rebuilds nothing above it
        self.assertNotIn("chmod +x", self.text)       # COPY --chmod: no extra layer of copies

    def test_every_entrypoint_file_is_copied_and_nothing_else(self):
        copied = set()
        for keyword, text in self.steps:
            if keyword != "COPY":
                continue
            words = [w for w in text.replace("\\\n", " ").split()[1:] if not w.startswith("--")]
            sources, destination = words[:-1], words[-1]
            self.assertEqual(destination, "/usr/local/bin/")
            copied.update(sources)
        present = {str(path.relative_to(ROOT)) for path in (ROOT / "docker/entrypoints").iterdir()
                   if path.is_file()}
        self.assertEqual(copied, present)  # a new module must be added to the Dockerfile
        executable = {name for name in present if name.endswith(".sh")}
        chmod_copy = next(text for keyword, text in self.steps
                          if keyword == "COPY" and "--chmod=0755" in text)
        self.assertEqual({w for w in chmod_copy.replace("\\\n", " ").split() if w.endswith(".sh")},
                         executable)
        # Every module the supervisor can import is checked at build time.
        check = next(text for keyword, text in self.steps if keyword == "RUN" and "import " in text)
        for module in ("supervisor", "health_ros", "characterize"):
            self.assertIn(module, check)

    def test_the_build_context_is_the_entrypoints_directory_only(self):
        # Everything is excluded, then the one directory the image is built from is let back in
        # (minus bytecode). Tests, docs, tools, radio configs and var/ (password files) never
        # reach the builder, so none of them can invalidate a layer. That the COPY sources
        # survive this file is proven by every image build.
        lines = [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
                 if line.strip() and not line.startswith("#")]
        self.assertEqual(lines, ["*", "!docker/entrypoints/", "**/__pycache__/", "**/*.pyc"])

if __name__ == "__main__":
    unittest.main()
