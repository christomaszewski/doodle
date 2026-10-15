#!/usr/bin/env python3
"""Own one radio's transmit power within one running container.

Only the supervisor changes the radio. Requests arrive over a local Unix socket (radioctl.sh,
`doodle-up tx-power`) and as the ROS parameter `tx_power` (health_ros.py). A reconcile pass every
few seconds keeps the radio at its target across radio reboots, wifi restarts and RF toggles;
SIGTERM applies tx_power.shutdown and confirms it. Every change is live (nothing is written to the
radio's flash) and is read back. Diagnostics run beside it (health.py) and only read.

  supervisor.py serve | status | set <value> | health
"""
import contextlib
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import socket
import socketserver
import sys
import threading
import time

import doodle_config
import power
from radio_api import RadioAPI, RadioError

LOG = logging.getLogger("doodle")
SOCKET = os.environ.get("DOODLE_CONTROL_SOCKET", "/run/doodle/control.sock")
TARGET_FILE = os.environ.get("DOODLE_TARGET_FILE", "/var/lib/doodle/target.json")
SECRET_FILE = os.environ.get("DOODLE_SECRET_FILE", "/run/secrets/radio_password")
RECONCILE_INTERVAL = 5
LOCK_WAIT = 20          # a request queued behind another change fails after this long
RF_TIMEOUT = 90         # RF on (observed: < 40 s) or off
APPLY_TIMEOUT = 10      # power read-back after `iw set txpower`
SHUTDOWN_BUDGET = 45    # inside compose's 60 s stop grace period
HEALTH_TIMEOUT = 2.0    # per diagnostics request


class Controller:
    def __init__(self, radio, settings, target_file, *, clock=time.monotonic, stop_event=None,
                 rf_timeout=RF_TIMEOUT, apply_timeout=APPLY_TIMEOUT, poll=1.0, sleep=time.sleep):
        self.radio = radio
        self.settings = settings  # validated tx_power block
        self.target_file = Path(target_file)
        self.clock, self.sleep = clock, sleep
        self.stop_event = stop_event or threading.Event()
        self.rf_timeout, self.apply_timeout, self.poll = rf_timeout, apply_timeout, poll
        self.lock = threading.Lock()
        self.uptime = None
        self.changing = ""      # what a write in progress is doing, for diagnostics
        self.observed_at = None  # monotonic time of the latest snapshot
        self.generation = 0      # increments with every snapshot (diagnostics sample id)
        self.diagnostics = "off"  # set by the ROS health monitor
        self._permissions_checked = False
        self._supported = (None, [])  # (frequency, txpowerlist) — re-read when the channel moves
        self.target = self._load_target()
        self.snapshot = self._snapshot(self.target, None, "", "waiting for first observation")

    # --- target ---------------------------------------------------------------------------------

    def _load_target(self):
        if self.target_file.exists():
            try:
                return doodle_config.power_value(
                    json.loads(self.target_file.read_text())["target"], "saved target")
            except (OSError, ValueError, KeyError, TypeError) as error:
                LOG.warning("ignoring unreadable saved target (%s); using tx_power.startup", error)
        return self.settings["startup"]

    def _save_target(self, target):
        """Persist a runtime request (survives Docker restarts); None returns to tx_power.startup."""
        self.target_file.parent.mkdir(parents=True, exist_ok=True)
        if target is None:
            self.target_file.unlink(missing_ok=True)
            return
        temporary = self.target_file.with_suffix(".tmp")
        temporary.write_text(json.dumps({"target": target}) + "\n")
        temporary.replace(self.target_file)

    def resolve(self, target, supported):
        return power.resolve(target, supported, levels=self.settings["levels"],
                             max_dbm=self.settings["max_dbm"])

    # --- observation (read-only) ----------------------------------------------------------------

    def observe(self):
        if not self._permissions_checked:
            self.radio.require("observe")  # one clear message instead of per-call denials
            self._permissions_checked = True
        uptime = self.radio.system_info().get("uptime")
        if isinstance(uptime, int):
            if self.uptime is not None and uptime < self.uptime:
                LOG.warning("radio rebooted (uptime %ss -> %ss); its saved settings are back",
                            self.uptime, uptime)
                self._supported = (None, [])
            self.uptime = uptime
        observation = {"rf": "off", "tx_power_dbm": None, "supported": []}
        if self.radio.rf_up():
            wireless = self.radio.wireless()
            frequency = wireless.get("frequency")
            if self._supported[0] != frequency or not self._supported[1]:
                self._supported = (frequency, self.radio.txpower_list())
            observation.update(rf="on", tx_power_dbm=wireless.get("txpower"),
                               supported=self._supported[1])
        else:
            self._supported = (None, [])
        return observation

    def evaluate(self, target, observation):
        """-> (dBm or 'off' or None, converged, detail). Raises PowerError for impossible targets."""
        if target == "unchanged":
            return None, True, "power not managed (target unchanged)"
        if target == "off":
            done = observation["rf"] == "off"
            return "off", done, "RF off" if done else "RF still on"
        if observation["rf"] != "on":
            return None, False, "RF is off; the target needs it on"
        wanted = self.resolve(target, observation["supported"])
        done = observation["tx_power_dbm"] == wanted
        return wanted, done, (f"at {wanted} dBm" if done else
                              f"radio at {observation['tx_power_dbm']} dBm, target {wanted} dBm")

    def _snapshot(self, target, observation, error, detail=""):
        snapshot = {"target": target, "target_dbm": None, "rf": "unknown", "tx_power_dbm": None,
                    "range_dbm": None, "converged": False, "detail": detail, "error": error,
                    "startup": self.settings["startup"], "shutdown": self.settings["shutdown"]}
        if observation is not None:
            supported = observation["supported"]
            snapshot.update(rf=observation["rf"], tx_power_dbm=observation["tx_power_dbm"],
                            range_dbm=[supported[0], supported[-1]] if supported else None)
            try:
                wanted, done, snapshot["detail"] = self.evaluate(target, observation)
                snapshot["target_dbm"] = wanted if isinstance(wanted, int) else None
                snapshot["converged"] = done and not error
            except power.PowerError as invalid:
                snapshot.update(error=error or str(invalid), detail="target not applicable")
        return snapshot

    def _record(self, observation, error=""):
        self.snapshot = self._snapshot(self.target, observation, error)
        self.observed_at = self.clock()
        self.generation += 1
        return self.snapshot

    def health_state(self):
        """For the diagnostics heartbeat: plain attribute reads, never the lock or the radio."""
        return {"snapshot": self.snapshot, "at": self.observed_at, "sample_id": self.generation,
                "changing": self.changing}

    def status(self):
        """Fresh read-only status; while a change runs, the last snapshot marked busy."""
        if not self.lock.acquire(blocking=False):
            return dict(self.snapshot, busy=True, diagnostics=self.diagnostics)
        try:
            try:
                self._record(self.observe())
            except RadioError as error:
                self._record(None, str(error))
            return dict(self.snapshot, busy=False, diagnostics=self.diagnostics)
        finally:
            self.lock.release()

    # --- changes --------------------------------------------------------------------------------

    def _pause(self, seconds, stop_aware):
        if stop_aware:
            if self.stop_event.wait(seconds):
                raise RadioError("supervisor stopping")
        else:
            self.sleep(seconds)

    def _wait_for(self, done, deadline, what, stop_aware):
        while True:
            try:
                observation = self.observe()
                if done(observation):
                    return observation
                last = f"RF {observation['rf']}, {observation['tx_power_dbm']} dBm"
            except RadioError as error:  # e.g. the interface is still coming up
                last = str(error)
            if self.clock() >= deadline:
                raise RadioError(f"radio did not reach {what} (last seen: {last})")
            self._pause(self.poll, stop_aware)

    def apply(self, target, *, deadline, stop_aware=True):
        """Bring the radio to `target`; read everything back. Writes only what differs."""
        try:
            return self._apply(target, deadline, stop_aware)
        finally:
            self.changing = ""

    def _apply(self, target, deadline, stop_aware):
        observation = self.observe()
        if target == "unchanged":
            return observation
        if target == "off":
            if observation["rf"] == "on":
                self.radio.require("rf_toggle")
                LOG.info("turning RF off")
                self.changing = "turning RF off"
                self.radio.set_rf(False)
                observation = self._wait_for(lambda o: o["rf"] == "off", deadline, "RF off",
                                             stop_aware)
            return observation
        if observation["rf"] == "off":
            self.radio.require("rf_toggle")
            LOG.info("turning RF on")
            self.changing = "turning RF on"
            self.radio.set_rf(True)
            observation = self._wait_for(
                lambda o: o["rf"] == "on" and o["supported"] and o["tx_power_dbm"] is not None,
                deadline, "RF on", stop_aware)
        wanted = self.resolve(target, observation["supported"])
        if observation["tx_power_dbm"] != wanted:
            self.radio.require("set_power")
            LOG.info("setting transmit power to %s dBm (radio at %s dBm)", wanted,
                     observation["tx_power_dbm"])
            self.changing = f"setting {wanted} dBm"
            self.radio.set_txpower(wanted)
            observation = self._wait_for(lambda o: o["tx_power_dbm"] == wanted,
                                         min(deadline, self.clock() + self.apply_timeout),
                                         f"{wanted} dBm", stop_aware)
        return observation

    def reconcile(self):
        """One control-loop pass. Never queues behind a request; errors land in the status."""
        if not self.lock.acquire(blocking=False):
            return self.snapshot
        try:
            if self.stop_event.is_set():
                return self.snapshot
            try:
                return self._record(self.apply(self.target,
                                               deadline=self.clock() + self.rf_timeout))
            except (RadioError, power.PowerError) as error:
                try:
                    observation = self.observe()
                except RadioError:
                    observation = None
                return self._record(observation, str(error))
        finally:
            self.lock.release()

    def request(self, value):
        """A runtime change: validate, persist, apply. `startup` returns to the configured value."""
        target = self.settings["startup"] if value == "startup" else \
            doodle_config.power_value(value, "tx-power")
        if not self.lock.acquire(timeout=LOCK_WAIT):
            raise RuntimeError("another change is in progress; retry when it settles")
        try:
            if self.stop_event.is_set():
                raise RuntimeError("supervisor stopping")
            previous = self.target
            saved = self.target_file.read_text() if self.target_file.exists() else None
            self.target = target
            self._save_target(None if value == "startup" else target)
            LOG.info("request: %s", power.describe(target))
            try:
                observation = self.apply(target, deadline=self.clock() + self.rf_timeout)
            except power.PowerError as error:
                # The radio cannot honor it: reject instead of persisting an impossible target.
                self.target = previous
                if saved is None:
                    self.target_file.unlink(missing_ok=True)
                else:
                    self.target_file.write_text(saved)
                self._record(None, "")
                raise RuntimeError(str(error)) from None
            except RadioError as error:
                # Accepted but not (yet) applied: the target stays; reconcile keeps trying.
                self._record(None, str(error))
                raise
            return self._record(observation)
        finally:
            self.lock.release()

    def shutdown(self):
        """Apply tx_power.shutdown and confirm it. Does not change the saved runtime request."""
        policy = self.settings["shutdown"]
        self.stop_event.set()
        started = self.clock()
        if not self.lock.acquire(timeout=LOCK_WAIT):
            raise RuntimeError("shutdown could not take control of the radio; power unconfirmed")
        try:
            if policy == "unchanged":
                LOG.info("shutdown: leaving radio power unchanged")
                return
            LOG.info("shutdown: applying %s", power.describe(policy))
            # Runs despite stop_event: this is the one change that must finish.
            observation = self.apply(policy, deadline=self.clock() + SHUTDOWN_BUDGET,
                                     stop_aware=False)
            LOG.info("shutdown complete: RF %s, %s dBm (restart target stays %s)",
                     observation["rf"], observation["tx_power_dbm"], power.describe(self.target))
        except Exception:
            LOG.error("shutdown failed; radio power is NOT confirmed")
            raise
        finally:
            self.lock.release()
            LOG.info("shutdown sequence finished in %.2fs", self.clock() - started)


# --- control socket ------------------------------------------------------------------------------

class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            message = json.loads(self.rfile.readline(1024))
            controller = self.server.controller
            if message.get("command") == "status":
                result = {"ok": True, **controller.status()}
            elif message.get("command") == "set":
                result = {"ok": True, **controller.request(message.get("value"))}
            else:
                raise ValueError(f"unknown command: {message.get('command')}")
        except Exception as error:
            result = {"ok": False, "error": str(error)}
        try:
            self.wfile.write((json.dumps(result) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass


def read_password(path):
    try:
        password = Path(path).read_text().rstrip("\r\n")
    except OSError as error:
        raise RuntimeError(f"cannot read the radio password at {path}: {error.strerror}") from None
    if not password:
        raise RuntimeError(f"the radio password file {path} is empty")
    return password


def health_monitor(controller, config, stop_event, health_radio):
    """The ROS diagnostics publisher, unless DOODLE_DIAGNOSTICS=off (tests without ROS)."""
    if os.environ.get("DOODLE_DIAGNOSTICS", "on") == "off":
        return contextlib.nullcontext()
    from health_ros import ros_health  # rclpy is imported only here
    return ros_health(controller, config, stop_event, health_radio)


def serve(*, monitor=health_monitor):
    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    config = doodle_config.runtime(json.loads(os.environ.get("DOODLE_CONFIG") or "{}"))
    connection = config["connection"]
    password = read_password(SECRET_FILE)
    radio = RadioAPI.from_config(connection, password)
    # Diagnostics read with their own session and short timeouts, so they never wait on a change.
    health_radio = RadioAPI.from_config(connection, password, timeout=HEALTH_TIMEOUT)
    controller = Controller(radio, config["tx_power"], TARGET_FILE, stop_event=stop_event)
    LOG.info("radio %s://%s as %s; target %s, shutdown %s", connection["scheme"],
             connection["host"], connection["username"], power.describe(controller.target),
             power.describe(config["tx_power"]["shutdown"]))
    path = Path(SOCKET)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("w") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)  # one supervisor per radio
        path.unlink(missing_ok=True)
        with Server(str(path), Handler) as server:
            os.chmod(path, 0o600)
            server.controller = controller
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            finished = False

            def finish():
                """Stop requests, then apply the shutdown power — before ROS is torn down, so a
                slow ROS shutdown can never cost the radio its shutdown setting."""
                nonlocal finished
                if finished:
                    return
                finished = True
                stop_event.set()
                server.shutdown()
                thread.join()
                controller.shutdown()

            try:
                with monitor(controller, config, stop_event, health_radio):
                    try:
                        last_error, logged_at = None, 0.0
                        while not stop_event.is_set():
                            try:
                                error = controller.reconcile().get("error")
                            except Exception as unexpected:  # a bug must not end power control
                                LOG.exception("reconcile failed")
                                error = f"internal error: {unexpected}"
                            if error and (error != last_error
                                          or time.monotonic() - logged_at > 60):
                                LOG.warning("%s", error)
                                logged_at = time.monotonic()
                            elif not error and last_error:
                                LOG.info("recovered: %s", controller.snapshot["detail"])
                            last_error = error
                            stop_event.wait(RECONCILE_INTERVAL)
                    finally:
                        finish()
            finally:
                try:
                    finish()  # also when the monitor itself failed to start or stop
                finally:
                    radio.close()
                    health_radio.close()
                    path.unlink(missing_ok=True)


def client(command, value=None, *, timeout=15):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(SOCKET)
        message = {"command": command} if value is None else {"command": command, "value": value}
        connection.sendall((json.dumps(message) + "\n").encode())
        with connection.makefile("rb") as stream:
            result = json.loads(stream.readline(65536))
    if not result.pop("ok"):
        raise RuntimeError(result["error"])
    return result


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    args = sys.argv[1:] if argv is None else argv
    try:
        if args == ["serve"]:
            serve()
        elif args == ["status"]:
            print(json.dumps(client("status", timeout=20)))
        elif len(args) == 2 and args[0] == "set":
            print(json.dumps(client("set", args[1], timeout=LOCK_WAIT + RF_TIMEOUT + 30)))
        elif args == ["health"]:
            status = client("status", timeout=8)
            healthy = status.get("converged") and not status.get("error")
            return 0 if healthy and not str(status.get("diagnostics")).startswith("failed") else 1
        else:
            sys.stderr.write("usage: supervisor.py serve | status | set <value> | health\n")
            return 2
        return 0
    except Exception as error:
        LOG.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
