#!/usr/bin/env python3
"""Bound launcher readiness independently of Docker, socket and radio request timeouts."""
import json
import math
import os
import subprocess
import sys
import time


def budget(name, default):
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return value


def wait(command, connect_s, settle_s, *, clock=time.monotonic, sleep=time.sleep,
         run=subprocess.run):
    deadline = clock() + connect_s
    connected = False
    status = {}
    while clock() < deadline:
        try:
            result = run(command, capture_output=True, text=True,
                         timeout=max(0.001, min(2.0, deadline - clock())))
            status = json.loads(result.stdout) if result.returncode == 0 else {}
            if not isinstance(status, dict):
                status = {}
        except (subprocess.TimeoutExpired, ValueError, OSError):
            status = {}
        if status.get('error_kind') in ('auth', 'config'):
            return False, f"{status['error_kind']}: {status.get('error', 'radio rejected configuration')}"
        if status.get('converged') and not status.get('error'):
            return True, ''
        if not connected and status.get('reachable'):
            connected = True
            deadline = clock() + settle_s
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(0.5, remaining))
    if connected:
        return False, f"radio did not reach its target within {settle_s:g}s (last status: {status})"
    return False, f"radio unreachable within {connect_s:g}s (last status: {status or 'no supervisor reply'})"


def main():
    try:
        ok, message = wait(sys.argv[1:], budget('DOODLE_CONNECT_TIMEOUT', '10'),
                           budget('DOODLE_SETTLE_TIMEOUT', '120'))
    except ValueError as error:
        ok, message = False, str(error)
    if not ok:
        print(f"doodle-up: {message} — left up; background retries continue; check 'tx-power' / 'logs'",
              file=sys.stderr)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
