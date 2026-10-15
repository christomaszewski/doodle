#!/usr/bin/env python3
"""Validate a doodle rig config and print the launcher's environment lines.

  render_config.py <config.yaml>

Prints shell-quoted KEY=value lines for `doodle-up` to export: instance identity, the host path of
the radio password file, the runtime config the container validates again (DOODLE_CONFIG), and the
rendered Zenoh override pairs. Exits 2 with one message on stderr for any invalid setting, before
the launcher touches a container. Needs PyYAML on the host (apt: python3-yaml).
"""
import os
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "docker" / "entrypoints"))
import doodle_config  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("render_config: PyYAML required on the host (apt install python3-yaml)\n")
    sys.exit(2)


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write("usage: render_config.py <config.yaml>\n")
        return 2
    path = Path(sys.argv[1])
    try:
        with open(path) as handle:
            raw = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as error:
        sys.stderr.write(f"render_config: cannot read {path}: {error}\n")
        return 2
    try:
        config = doodle_config.load(raw)
    except doodle_config.ConfigError as error:
        sys.stderr.write(f"render_config: {error}\n")
        return 2
    for warning in config["warnings"]:
        sys.stderr.write(f"render_config: {warning}\n")
    # A relative password file is relative to the config file, like any other path in it.
    password_file = Path(os.path.expanduser(config["connection"]["password_file"]))
    if not password_file.is_absolute():
        password_file = path.resolve().parent / password_file
    lines = [f"SERVICE={config['service']}",
             f"NAME={config['name']}",
             f"NAMESPACE=/{config['namespace']}",
             "PASSWORD_FILE=" + shlex.quote(str(password_file)),
             "DOODLE_CONFIG=" + shlex.quote(doodle_config.runtime_json(config)),
             "ZENOH_OVERRIDE=" + shlex.quote(config["zenoh_override"])]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
