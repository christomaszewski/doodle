#!/usr/bin/env python3
"""Validate a doodle rig config for the launcher.

  render_config.py <config.yaml>                       env lines for doodle-up to export
  render_config.py --write-secret <path> <config.yaml> write the radio password to <path> (0600)

The env lines carry instance identity, the runtime config the container validates again
(DOODLE_CONFIG) and the rendered Zenoh override pairs — never the password. The password reaches
the container only through the secret file, which compose mounts at /run/secrets/radio_password,
so neither `config` output nor a rig bake artifact contains it. Invalid settings exit 2 with one
message on stderr, before the launcher touches a container. Needs PyYAML (apt: python3-yaml).
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


def write_secret(path: Path, password: str) -> None:
    """Atomically replace `path` with the password, readable by its owner only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(password)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    args = sys.argv[1:]
    secret = None
    if len(args) == 3 and args[0] == "--write-secret":
        secret, args = Path(args[1]), args[2:]
    if len(args) != 1:
        sys.stderr.write("usage: render_config.py [--write-secret <path>] <config.yaml>\n")
        return 2
    path = Path(args[0])
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

    if secret is not None:
        var = doodle_config.unresolved_password_var(config)
        if var:
            sys.stderr.write(
                f"render_config: connection.ubus.password is the rig var {{{{{var}}}}}, which was "
                f"not filled in. Under rig, provision it once per vehicle: sudo rig provision "
                f"--var {var}=<password>. Standalone, put the password itself in the config.\n")
            return 2
        write_secret(secret, config["connection"]["password"])
        return 0

    for warning in config["warnings"]:
        sys.stderr.write(f"render_config: {warning}\n")
    lines = [f"SERVICE={config['service']}",
             f"NAME={config['name']}",
             f"NAMESPACE=/{config['namespace']}",
             "DOODLE_CONFIG=" + shlex.quote(doodle_config.runtime_json(config)),
             "ZENOH_OVERRIDE=" + shlex.quote(config["zenoh_override"])]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
