#!/usr/bin/env python3
"""Bound launcher readiness independently of Docker, socket and radio request timeouts.

Three budgets, each starting when the one before it is met:

  DOODLE_START_TIMEOUT    the supervisor answers at all. This is the container's own start (the
                          entrypoint sourcing ROS, Python's imports), which takes seconds on a
                          cold boot and says nothing about the radio.
  DOODLE_CONNECT_TIMEOUT  the supervisor has reached the radio.
  DOODLE_SETTLE_TIMEOUT   the radio is at its target power.
"""
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


def wait(command, start_s, connect_s, settle_s, *, clock=time.monotonic, sleep=time.sleep,
         run=subprocess.run):
    deadline = clock() + start_s
    phase = 'start'  # -> 'connect' once the supervisor answers -> 'settle' once the radio does
    status = {}
    while clock() < deadline:
        answered = False
        try:
            result = run(command, capture_output=True, text=True,
                         timeout=max(0.001, min(2.0, deadline - clock())))
            reply = json.loads(result.stdout) if result.returncode == 0 else None
            if isinstance(reply, dict):
                status, answered = reply, True
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass
        if not answered:
            status = {}
        if status.get('error_kind') in ('auth', 'config'):
            return False, f"{status['error_kind']}: {status.get('error', 'radio rejected configuration')}"
        if status.get('converged') and not status.get('error'):
            return True, ''
        if phase == 'start' and answered:
            phase, deadline = 'connect', clock() + connect_s
        if phase != 'settle' and status.get('reachable'):
            phase, deadline = 'settle', clock() + settle_s
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(0.5, remaining))
    if phase == 'settle':
        return False, f"radio did not reach its target within {settle_s:g}s (last status: {status})"
    if phase == 'connect':
        return False, (f"radio unreachable within {connect_s:g}s "
                       f"(last status: {status or 'no supervisor reply'})")
    return False, (f"the supervisor did not answer within {start_s:g}s: the container is still "
                   "starting or has stopped (nothing is known about the radio yet)")


def main():
    try:
        ok, message = wait(sys.argv[1:], budget('DOODLE_START_TIMEOUT', '30'),
                           budget('DOODLE_CONNECT_TIMEOUT', '10'),
                           budget('DOODLE_SETTLE_TIMEOUT', '120'))
    except ValueError as error:
        ok, message = False, str(error)
    if not ok:
        print(f"doodle-up: {message} — left up; background retries continue; check 'tx-power' / 'logs'",
              file=sys.stderr)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
