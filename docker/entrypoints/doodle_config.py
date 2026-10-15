"""Validate a doodle rig service config.

One module for both sides of the container boundary: tools/render_config.py validates the YAML
before a container is created or replaced, and the supervisor validates the JSON it receives
again with the same rules. Pure Python (no PyYAML) so the runtime image needs nothing extra.
"""
import json
import math
import re

IDENT = re.compile(r"^[a-z][a-z0-9_]*$")
HOST = re.compile(r"^[A-Za-z0-9.:\[\]-]+$")
DEVICE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
USERNAME = re.compile(r"^[A-Za-z0-9_.-]+$")
FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")

LEVELS = ("standby", "low", "medium", "high", "max")
KEYWORDS = ("unchanged", "off")
# Sanity bounds only; the radio's own supported list decides at runtime.
DBM_MIN, DBM_MAX = 0, 40

UBUS_KEYS = {"host", "port", "scheme", "username", "password_file", "tls_fingerprint_sha256",
             "device", "uci_radio"}
TX_POWER_KEYS = {"startup", "shutdown", "levels", "max_dbm"}
HEALTH_KEYS = {"interval_s", "poll_interval_s", "stale_after_s", "limits"}
LIMIT_BOUNDS = ("warn_above", "warn_below", "error_above", "error_below")
ZENOH_KEYS = {"shared_memory", "shm_pool_mb", "overrides"}


class ConfigError(ValueError):
    pass


def _mapping(value, where):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a mapping")
    return value


def _no_unknown(block, allowed, where):
    unknown = sorted(set(block) - allowed)
    if unknown:
        raise ConfigError(f"unknown {where} key(s): {', '.join(map(str, unknown))} "
                          f"(supported: {', '.join(sorted(allowed))})")


def _int(value, where):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where} must be a whole number")
    return value


def ident(what, value):
    value = str(value)
    if not IDENT.match(value):
        raise ConfigError(f"{what} {value!r} must match [a-z][a-z0-9_]* "
                          "(it becomes a compose project / ROS namespace)")
    return value


def power_value(value, where="tx_power"):
    """Normalize one power setting: a level name, 'off', 'unchanged', or whole dBm."""
    if value is False:  # YAML 1.1 reads a bare `off` as false
        return "off"
    if isinstance(value, bool):
        raise ConfigError(f"{where}: {value!r} is not a power setting (did you mean off?)")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().lower() in LEVELS + KEYWORDS:
        return value.strip().lower()
    elif isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        number = int(value.strip())
    else:
        raise ConfigError(f"{where}: {value!r} must be one of {', '.join(LEVELS + KEYWORDS)} "
                          "or whole dBm")
    if not DBM_MIN <= number <= DBM_MAX:
        raise ConfigError(f"{where}: {number} dBm is outside {DBM_MIN}..{DBM_MAX}")
    return number


def connection(raw, *, runtime=False):
    """`runtime` is the container's view: the host-side password_file path is not passed in."""
    raw = _mapping(raw, "connection")
    kind = str(raw.get("type") or "ubus")
    if kind != "ubus":
        raise ConfigError(f"unsupported connection.type {kind!r} (supported: ubus)")
    block = _mapping(raw.get("ubus"), "connection.ubus")
    _no_unknown(block, UBUS_KEYS, "connection.ubus")
    host = str(block.get("host") or "")
    if not HOST.match(host):
        raise ConfigError("connection.ubus.host is required (radio IP or hostname)")
    scheme = str(block.get("scheme") or "https")
    if scheme not in ("https", "http"):
        raise ConfigError("connection.ubus.scheme must be https or http")
    port = block.get("port")
    if port is not None and not 1 <= _int(port, "connection.ubus.port") <= 65535:
        raise ConfigError("connection.ubus.port must be 1..65535")
    username = str(block.get("username") or "user")
    if not USERNAME.match(username):
        raise ConfigError("connection.ubus.username has invalid characters")
    password_file = block.get("password_file")
    if runtime:
        if password_file is not None:
            raise ConfigError("password_file is host-side only")
    elif not isinstance(password_file, str) or not password_file.strip():
        raise ConfigError("connection.ubus.password_file is required (a file holding the radio's "
                          "API password; see docs/RADIO_SETUP.md)")
    elif any(c in password_file for c in "$\n"):
        raise ConfigError("connection.ubus.password_file must not contain '$' or newlines")
    fingerprint = str(block.get("tls_fingerprint_sha256") or "").replace(":", "").lower()
    if fingerprint and not FINGERPRINT.match(fingerprint):
        raise ConfigError("connection.ubus.tls_fingerprint_sha256 must be 64 hex digits")
    if fingerprint and scheme != "https":
        raise ConfigError("connection.ubus.tls_fingerprint_sha256 requires scheme https")
    device = str(block.get("device") or "wlan0")
    if not DEVICE.match(device):
        raise ConfigError("connection.ubus.device must be an interface name like wlan0")
    result = {"host": host, "port": port, "scheme": scheme, "username": username,
              "tls_fingerprint_sha256": fingerprint,
              "device": device,
              "uci_radio": ident("connection.ubus.uci_radio", block.get("uci_radio") or "radio0")}
    if not runtime:
        result["password_file"] = password_file
    return result


def tx_power(raw):
    raw = _mapping(raw, "tx_power")
    _no_unknown(raw, TX_POWER_KEYS, "tx_power")
    result = {"startup": power_value(raw.get("startup", "unchanged"), "tx_power.startup"),
              "shutdown": power_value(raw.get("shutdown", "unchanged"), "tx_power.shutdown")}
    levels = _mapping(raw.get("levels"), "tx_power.levels")
    _no_unknown(levels, {"low", "medium"}, "tx_power.levels")
    result["levels"] = {}
    for name, value in levels.items():
        number = power_value(value, f"tx_power.levels.{name}")
        if not isinstance(number, int):
            raise ConfigError(f"tx_power.levels.{name} must be whole dBm")
        result["levels"][name] = number
    if "low" in result["levels"] and "medium" in result["levels"] \
            and result["levels"]["low"] > result["levels"]["medium"]:
        raise ConfigError("tx_power.levels.low must not be above tx_power.levels.medium")
    max_dbm = raw.get("max_dbm")
    if max_dbm is not None:
        max_dbm = power_value(max_dbm, "tx_power.max_dbm")
        if not isinstance(max_dbm, int):
            raise ConfigError("tx_power.max_dbm must be whole dBm")
        for name, value in result["levels"].items():
            if value > max_dbm:
                raise ConfigError(f"tx_power.levels.{name} ({value}) is above tx_power.max_dbm")
    result["max_dbm"] = max_dbm
    return result


def _number(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigError(f"{where} must be a finite number")
    return value


def health(raw):
    raw = _mapping(raw, "health")
    _no_unknown(raw, HEALTH_KEYS, "health")
    result = {"interval_s": 1, "poll_interval_s": 5, "stale_after_s": 15, "limits": {}}
    result.update(raw)
    for key in ("interval_s", "poll_interval_s", "stale_after_s"):
        if _number(result[key], f"health.{key}") <= 0:
            raise ConfigError(f"health.{key} must be positive")
    limits = _mapping(result["limits"], "health.limits")
    for metric, bounds in limits.items():
        bounds = _mapping(bounds, f"health.limits.{metric}")
        for bound, value in bounds.items():
            if bound not in LIMIT_BOUNDS:
                raise ConfigError(f"invalid health.limits.{metric}.{bound}")
            _number(value, f"health.limits.{metric}.{bound}")
    result["limits"] = limits
    return result


def zenoh(raw):
    """The fleet-uniform `zenoh:` block -> (ZENOH_CONFIG_OVERRIDE pairs, warnings).

    Same shape and flattening as the ouster service and the rig-infra bag logger: SHM sugar first,
    then `overrides` flattened to leaf path=json pairs (later pairs win in rmw_zenoh).
    """
    raw = _mapping(raw, "zenoh")
    _no_unknown(raw, ZENOH_KEYS, "zenoh")
    enable = raw.get("shared_memory", False)
    if not isinstance(enable, bool):
        raise ConfigError("zenoh.shared_memory must be a bool")
    pool_mb = raw.get("shm_pool_mb")
    if pool_mb is not None and (isinstance(pool_mb, bool) or not isinstance(pool_mb, int)
                                or pool_mb <= 0):
        raise ConfigError("zenoh.shm_pool_mb must be a positive integer (MB)")
    warnings = []
    if enable:
        warnings.append("zenoh.shared_memory has no effect for this service (small messages, no "
                        "host IPC); the session falls back to TCP")
    if pool_mb is not None and not enable:
        warnings.append("zenoh.shm_pool_mb is ignored without zenoh.shared_memory: true")
        pool_mb = None
    pairs = []
    if enable:
        pairs.append(("transport/shared_memory/enabled", "true"))
        if pool_mb:
            pairs.append(("transport/shared_memory/transport_optimization/pool_size",
                          str(pool_mb * 1024 * 1024)))
    overrides = raw.get("overrides")
    if overrides is not None and not isinstance(overrides, dict):
        raise ConfigError("zenoh.overrides must be a mapping (nested zenoh session config)")

    def leaves(prefix, value):
        if isinstance(value, dict) and value:
            for key in sorted(value):
                leaves(f"{prefix}/{key}" if prefix else str(key), value[key])
        else:
            pairs.append((prefix, json.dumps(value, sort_keys=True, separators=(",", ":"))))

    if overrides:
        leaves("", overrides)
    for path, value in pairs:
        if ";" in path or ";" in value:
            raise ConfigError(f"zenoh override at {path!r} contains ';' (the override separator)")
    return ";".join(f"{p}={v}" for p, v in pairs), warnings


def load(cfg):
    """Validate a whole instance config (parsed YAML) -> normalized dict."""
    cfg = _mapping(cfg, "config")
    name = ident("name", cfg.get("name") or "doodle")
    ros = _mapping(cfg.get("ros"), "ros")
    zenoh_pairs, warnings = zenoh(cfg.get("zenoh"))
    return {
        "service": ident("service", cfg.get("service") or "doodle"),
        "name": name,
        "namespace": ident("ros.namespace", ros.get("namespace") or name),
        "connection": connection(cfg.get("connection")),
        "tx_power": tx_power(cfg.get("tx_power")),
        "health": health(cfg.get("health")),
        "zenoh_override": zenoh_pairs,
        "warnings": warnings,
    }


def runtime(raw):
    """The subset the container receives as DOODLE_CONFIG, validated again on arrival."""
    raw = _mapping(raw, "DOODLE_CONFIG")
    _no_unknown(raw, {"connection", "tx_power", "health"}, "DOODLE_CONFIG")
    return {"connection": connection({"type": "ubus", "ubus": raw.get("connection")}, runtime=True),
            "tx_power": tx_power(raw.get("tx_power")),
            "health": health(raw.get("health"))}


def runtime_json(config):
    """Deterministic DOODLE_CONFIG for a validated config (password_file stays host-side)."""
    conn = {k: v for k, v in config["connection"].items() if k != "password_file"}
    return json.dumps({"connection": conn, "tx_power": config["tx_power"],
                       "health": config["health"]}, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)
