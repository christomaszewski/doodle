"""Resolve a power target against what the radio supports right now.

Targets are normalized by doodle_config.power_value: a level name, 'off', 'unchanged', or whole dBm.
The supported list comes from the radio (`iwinfo txpowerlist`) and can change with channel and
regulatory domain, so levels are resolved on every check, never cached from configuration time.
"""


class PowerError(ValueError):
    pass


def resolve(target, supported, *, levels=None, max_dbm=None):
    """-> None (leave power alone), 'off', or the whole dBm to apply."""
    if target == "unchanged":
        return None
    if target == "off":
        return "off"
    if not supported:
        raise PowerError("the radio reported no supported transmit power levels")
    allowed = sorted({v for v in supported if max_dbm is None or v <= max_dbm})
    if not allowed:
        raise PowerError(f"max_dbm {max_dbm} is below the radio's lowest power, {min(supported)} dBm")
    low, high = allowed[0], allowed[-1]
    span = f"{low}..{high} dBm" + (" (capped by max_dbm)" if max_dbm is not None
                                   and max_dbm < max(supported) else "")
    if isinstance(target, int):
        if target not in allowed:
            raise PowerError(f"{target} dBm is not supported by the radio right now ({span})")
        return target
    if target == "standby":
        return low
    if target in ("high", "max"):
        return high
    if target in ("low", "medium"):
        override = (levels or {}).get(target)
        if override is not None:
            if override not in allowed:
                raise PowerError(f"tx_power.levels.{target} = {override} dBm is not supported "
                                 f"by the radio right now ({span})")
            return override
        want = low + (high - low) * (1 if target == "low" else 2) // 3
        return max(v for v in allowed if v <= want)
    raise PowerError(f"unknown power target {target!r}")


def describe(target):
    """Human form of a normalized target, for logs and status."""
    return f"{target} dBm" if isinstance(target, int) else str(target)
