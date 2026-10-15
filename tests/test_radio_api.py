"""The ubus JSON-RPC client: sessions, errors, permissions, secrets, HTTP and TLS."""
import hashlib
import logging
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker/entrypoints"))
sys.path.insert(0, str(ROOT / "tests"))
from radio_api import AuthError, RadioAPI, RadioError, UbusError  # noqa: E402
from fake_radio import FakeRadio, MESH_KEY, Server  # noqa: E402


def api(radio, password="fixture-password", **kwargs):
    return RadioAPI("fake", "user", password, transport=radio, **kwargs)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.radio = FakeRadio()
        self.api = api(self.radio)

    def test_reads_extract_only_needed_fields(self):
        self.assertEqual(self.api.txpower_list(), list(range(19, 33)))
        wireless = self.api.wireless()
        self.assertEqual((wireless["txpower"], wireless["frequency"]), (32, 2450))
        self.assertNotIn("ssid", wireless)
        self.assertEqual(self.api.board()["firmware"], "firmware-2026-03.1")
        self.assertEqual(self.api.power_control(),
                         {"automatic": True, "aggressive": False, "chanbw": "20"})
        self.assertTrue(self.api.rf_up())
        self.assertEqual(self.api.read_json("/tmp/linkstate_current.json")["sta_count"], 0)

    def test_mesh_key_and_password_never_leave_the_client(self):
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("probe").debug("start")
            result = self.api.rf_up()
            self.api.set_txpower(25)
        self.assertIs(result, True)
        self.assertNotIn(MESH_KEY, repr(vars(self.api)) + "".join(logs.output))
        self.assertNotIn("fixture-password", repr(self.api))
        with self.assertRaises(AuthError) as caught:
            api(self.radio, password="Bad-Secret-123").rf_up()
        self.assertNotIn("Bad-Secret-123", str(caught.exception))
        self.assertIn("no 'user' login", str(caught.exception))

    def test_expired_session_logs_in_again_once(self):
        self.api.rf_up()
        self.radio.expire_sessions()
        self.assertTrue(self.api.rf_up())
        self.assertEqual(len(self.radio.sessions), 1)

    def test_ubus_status_errors_are_named(self):
        self.radio.fail["iwinfo.info"] = 6
        with self.assertRaises(UbusError) as caught:
            self.api.wireless()
        self.assertEqual(str(caught.exception), "iwinfo.info: permission denied")
        self.assertEqual(caught.exception.status, 6)
        with self.assertRaises(UbusError):
            self.api.read_json("/tmp/missing.json")

    def test_set_txpower_sends_iw_in_hundredths_and_reports_failures(self):
        self.api.set_txpower(25)
        self.assertEqual(self.radio.calls[-1][2], {"command": "/usr/sbin/iw", "params": [
            "dev", "wlan0", "set", "txpower", "fixed", "2500"]})
        self.assertEqual(self.radio.txpower, 25)
        self.radio.rf_up = False
        with self.assertRaisesRegex(RadioError, r"exit 237\): command failed"):
            self.api.set_txpower(25)
        with self.assertRaises(ValueError):
            self.api.set_txpower(True)

    def test_permissions_come_from_the_login_acls(self):
        self.assertEqual(self.api.missing_permissions("observe", "set_power", "rf_toggle"), [])
        # The restricted 2023-09 role: iwinfo info only, no network.wireless.
        self.radio.acls = {"ubus": {"iwinfo": ["info"], "system": ["info"], "uci": ["*"],
                                    "file": ["exec", "read"]},
                           "file": {"/usr/sbin/iw *": ["exec"]}}
        restricted = api(self.radio)
        missing = restricted.missing_permissions("observe", "rf_toggle")
        self.assertIn("ubus iwinfo txpowerlist", missing)
        self.assertIn("ubus network.wireless down", missing)
        with self.assertRaisesRegex(RadioError, "lacks permission"):
            restricted.require("rf_toggle")

    def test_malformed_replies(self):
        for reply in ("nope", {"result": "x"}, {"result": []}, {"result": [0, ["list"]]}):
            radio = FakeRadio()
            client = api(radio)
            client.rf_up()  # log in first
            client._transport = lambda payload, timeout, reply=reply: reply
            with self.assertRaises(RadioError, msg=reply):
                client.system_info()


class TransportTests(unittest.TestCase):
    def test_http_keep_alive_and_reconnect(self):
        radio = FakeRadio()
        with Server(radio) as server:
            client = RadioAPI("127.0.0.1", "user", "fixture-password", scheme="http",
                              port=server.server_address[1])
            for _ in range(3):
                client.system_info()
            self.assertEqual(len(server.connections), 1)
            server.close_after_reply = True
            client.system_info()
            client.system_info()  # the radio closed the connection: reconnect transparently
            client.close()

    def test_unreachable_radio_is_a_bounded_error(self):
        client = RadioAPI("127.0.0.1", "user", "pw", scheme="http", port=1, timeout=1)
        with self.assertRaisesRegex(RadioError, "unreachable"):
            client.system_info()

    @unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a test certificate")
    def test_tls_with_and_without_a_fingerprint_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            key, cert = Path(tmp, "key.pem"), Path(tmp, "cert.pem")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", key, "-out", cert, "-days", "1", "-subj", "/CN=radio"],
                           check=True, capture_output=True)
            der = ssl.PEM_cert_to_DER_cert(cert.read_text())
            radio = FakeRadio()
            with Server(radio) as server:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(cert, key)
                server.socket = context.wrap_socket(server.socket, server_side=True)
                port = server.server_address[1]
                unpinned = RadioAPI("127.0.0.1", "user", "fixture-password", port=port)
                self.assertTrue(unpinned.rf_up())
                pinned = RadioAPI("127.0.0.1", "user", "fixture-password", port=port,
                                  fingerprint=hashlib.sha256(der).hexdigest())
                self.assertTrue(pinned.rf_up())
                wrong = RadioAPI("127.0.0.1", "user", "fixture-password", port=port,
                                 fingerprint="0" * 64)
                with self.assertRaisesRegex(RadioError, "does not match"):
                    wrong.rf_up()
                for client in (unpinned, pinned, wrong):
                    client.close()


if __name__ == "__main__":
    unittest.main()
