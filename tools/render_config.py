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
    """Make `path` hold the password, readable by its owner only.

    A new file is created atomically. An existing one is rewritten in place and never replaced:
    a running container has that very file mounted (a bind mount holds the file, not its path),
    and the supervisor reads it again at every login. So a rotated password reaches a running
    service with the next `up`, without recreating the container, which would run the shutdown
    and startup power changes just to change a password. A read that lands inside the rewrite
    is one failed login, retried on the next pass."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir() and not path.is_symlink():
        path.rmdir()  # Docker creates a directory for a bind source that did not exist
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        temporary = path.with_name(f".{path.name}.{os.getpid()}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(password)
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return
    with os.fdopen(descriptor, "r+") as handle:
        os.fchmod(handle.fileno(), 0o600)
        if handle.read() != password:
            handle.seek(0)
            handle.truncate()
            handle.write(password)
            handle.flush()
            os.fsync(handle.fileno())


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
    except OSError as error:
        sys.stderr.write(f"render_config: cannot read {path}: {error.strerror or error}\n")
        return 2
    except yaml.YAMLError as error:
        # Never the parser's own message: for an unquoted password starting with ! or * it
        # quotes the "tag" or "alias" it could not resolve, which is the password.
        mark = getattr(error, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        sys.stderr.write(f"render_config: {path} is not valid YAML{where} (the text is not "
                         "shown: it may be the password). Put text values in quotes, single "
                         "quotes if they contain \\ or \".\n")
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
             # Empty = not set in the config; the launcher then follows RIG_SIM_TIME.
             "USE_SIM_TIME=" + {None: "", True: "true", False: "false"}[config["use_sim_time"]],
             "DOODLE_CONFIG=" + shlex.quote(doodle_config.runtime_json(config)),
             "ZENOH_OVERRIDE=" + shlex.quote(config["zenoh_override"])]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
