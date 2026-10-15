"""Config validation shared by the launcher and the container, and the power-level logic."""
import json
from pathlib import Path
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker/entrypoints"))
import doodle_config as dc  # noqa: E402
from doodle_config import ConfigError  # noqa: E402
import power  # noqa: E402


def base(**tx_power):
    return {"name": "mesh", "connection": {"ubus": {"host": "10.223.18.52",
                                                    "password": "Config-Test-Pw"}},
            "tx_power": tx_power}


class ConfigTests(unittest.TestCase):
    def test_example_config_is_valid_and_leaves_power_alone(self):
        config = dc.load(yaml.safe_load((ROOT / "sensors/doodle.example.yaml").read_text()))
        self.assertEqual((config["service"], config["name"], config["namespace"]),
                         ("doodle", "mesh", "mesh"))
        self.assertEqual(config["tx_power"]["startup"], "unchanged")
        self.assertEqual(config["tx_power"]["shutdown"], "unchanged")
        self.assertEqual(config["connection"]["username"], "user")
        self.assertEqual(config["connection"]["scheme"], "https")

    def test_power_values(self):
        for raw, want in (("standby", "standby"), ("High", "high"), (" off ", "off"),
                          (False, "off"), (25, 25), ("25", 25), ("unchanged", "unchanged")):
            self.assertEqual(dc.power_value(raw), want, raw)
        for raw in (True, None, 25.5, "loud", 41, -1, "1e3"):
            with self.assertRaises(ConfigError, msg=raw):
                dc.power_value(raw)

    def test_bare_yaml_off_means_off(self):
        doc = yaml.safe_load("tx_power: {startup: low, shutdown: off}")
        config = dc.load({**base(), **doc})
        self.assertEqual(config["tx_power"]["shutdown"], "off")

    def test_levels_and_cap(self):
        config = dc.load(base(levels={"low": 22, "medium": 26}, max_dbm=30))
        self.assertEqual(config["tx_power"]["levels"], {"low": 22, "medium": 26})
        self.assertEqual(config["tx_power"]["max_dbm"], 30)
        for bad in ({"levels": {"low": 27, "medium": 22}}, {"levels": {"high": 30}},
                    {"levels": {"low": "standby"}}, {"levels": {"low": 31}, "max_dbm": 30},
                    {"max_dbm": "off"}, {"loudness": 1}):
            with self.assertRaises(ConfigError, msg=bad):
                dc.load(base(**bad))

    def test_connection_rules(self):
        config = dc.load(base())
        self.assertEqual(config["connection"]["password"], "Config-Test-Pw")
        pin = ":".join(["AB"] * 32)
        config = dc.load({**base(), "connection": {"ubus": {
            "host": "radio.local", "password": "p", "tls_fingerprint_sha256": pin}}})
        self.assertEqual(config["connection"]["tls_fingerprint_sha256"], "ab" * 32)
        bad_blocks = [{"password": "p"},                                       # no host
                      {"host": "h"},                                           # no password
                      {"host": "h", "password": ""},
                      {"host": "h", "password": 12345},                        # unquoted YAML
                      {"host": "h", "password": "a\nb"},
                      {"host": "h; rm -rf /", "password": "p"},
                      {"host": "h", "password": "p", "scheme": "ftp"},
                      {"host": "h", "password": "p", "port": 70000},
                      {"host": "h", "password": "p", "device": "wlan0 up"},
                      {"host": "h", "password": "p", "tls_fingerprint_sha256": "abc"},
                      {"host": "h", "password": "p", "scheme": "http",
                       "tls_fingerprint_sha256": "ab" * 32},
                      {"host": "h", "password": "p", "password_file": "/etc/p"}]
        for block in bad_blocks:
            with self.assertRaises(ConfigError, msg=block):
                dc.load({"connection": {"ubus": block}})
        with self.assertRaises(ConfigError):
            dc.load({**base(), "connection": {"type": "ssh", "ubus": {"host": "h"}}})

    def test_identity_must_be_safe_tokens(self):
        for bad in ({"name": "Mesh"}, {"name": "mesh;x"}, {"service": "1doodle"},
                    {"ros": {"namespace": "a/b"}}):
            with self.assertRaises(ConfigError, msg=bad):
                dc.load({**base(), **bad})

    def test_runtime_json_round_trips_without_host_paths(self):
        config = dc.load(base(startup="medium", shutdown=False))
        text = dc.runtime_json(config)
        self.assertNotIn("password", text)
        self.assertNotIn("Config-Test-Pw", text)
        self.assertEqual(text, dc.runtime_json(dc.load(base(startup="medium", shutdown=False))))
        runtime = dc.runtime(json.loads(text))
        self.assertEqual(runtime["tx_power"]["shutdown"], "off")
        self.assertNotIn("password", runtime["connection"])
        with self.assertRaises(ConfigError):
            dc.runtime({**json.loads(text), "extra": 1})
        leaked = json.loads(text)
        leaked["connection"]["password"] = "x"
        with self.assertRaises(ConfigError):
            dc.runtime(leaked)

    def test_rig_var_markers_validate_and_are_detected(self):
        config = dc.load({**base(), "connection": {"ubus": {
            "host": "h", "password": "{{doodle_radio_password}}"}}})
        self.assertEqual(dc.unresolved_password_var(config), "doodle_radio_password")
        self.assertIsNone(dc.unresolved_password_var(dc.load(base())))
        with self.assertRaises(ConfigError) as caught:
            dc.load({**base(), "connection": {"ubus": {"host": "h", "password": 8675309}}})
        self.assertNotIn("8675309", str(caught.exception))

    def test_health_and_zenoh_blocks(self):
        config = dc.load({**base(), "health": {"poll_interval_s": 2},
                          "zenoh": {"overrides": {"connect": {"endpoints": ["tcp/h:7447"]}}}})
        self.assertEqual(config["health"]["poll_interval_s"], 2)
        self.assertEqual(config["zenoh_override"], 'connect/endpoints=["tcp/h:7447"]')
        for bad in ({"health": {"interval_s": 0}}, {"health": {"limits": {"m": {"over": 1}}}},
                    {"zenoh": {"shared_memory": "yes"}}, {"zenoh": {"x": 1}}):
            with self.assertRaises(ConfigError, msg=bad):
                dc.load({**base(), **bad})


class PowerTests(unittest.TestCase):
    RANGE = list(range(19, 33))

    def test_levels_on_the_tested_radio(self):
        got = {t: power.resolve(t, self.RANGE)
               for t in ("standby", "low", "medium", "high", "max")}
        self.assertEqual(got, {"standby": 19, "low": 23, "medium": 27, "high": 32, "max": 32})
        self.assertIsNone(power.resolve("unchanged", []))
        self.assertEqual(power.resolve("off", []), "off")

    def test_cap_overrides_and_sparse_lists(self):
        self.assertEqual(power.resolve("high", self.RANGE, max_dbm=30), 30)
        self.assertEqual(power.resolve("medium", self.RANGE, max_dbm=30), 26)
        self.assertEqual(power.resolve("low", self.RANGE, levels={"low": 21}), 21)
        self.assertEqual(power.resolve("low", [10, 14, 20, 26]), 14)
        self.assertEqual(power.resolve(25, self.RANGE), 25)

    def test_impossible_targets_are_rejected_not_clamped(self):
        for target, kwargs in ((15, {}), (33, {}), (31, {"max_dbm": 30}),
                               ("standby", {"max_dbm": 10}), ("low", {"levels": {"low": 5}})):
            with self.assertRaises(power.PowerError, msg=(target, kwargs)):
                power.resolve(target, self.RANGE, **kwargs)
        with self.assertRaises(power.PowerError):
            power.resolve("high", [])


if __name__ == "__main__":
    unittest.main()
