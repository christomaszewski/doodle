"""The host readiness deadline bounds hung Docker exec calls and radio settling separately."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from wait_ready import wait


class ReadinessTests(unittest.TestCase):
    def test_hung_exec_is_bounded_by_initial_connection_budget(self):
        now = [0.0]
        def run(command, **kw):
            now[0] += kw['timeout']
            raise subprocess.TimeoutExpired(command, kw['timeout'])
        def sleep(t): now[0] += t
        ok, reason = wait(['docker'], 3, 120, clock=lambda: now[0], sleep=sleep, run=run)
        self.assertFalse(ok)
        self.assertEqual(now[0], 3)
        self.assertIn('unreachable within 3s', reason)

    def test_reachable_radio_gets_separate_settle_budget(self):
        now = [0.0]
        def sleep(t): now[0] += t
        def run(command, **kw):
            return SimpleNamespace(returncode=0, stdout=json.dumps({'reachable': now[0] >= 1}))
        ok, reason = wait(['docker'], 3, 8, clock=lambda: now[0], sleep=sleep, run=run)
        self.assertFalse(ok)
        self.assertEqual(now[0], 9)
        self.assertIn('did not reach its target within 8s', reason)

    def test_terminal_errors_fail_immediately_and_convergence_succeeds(self):
        for body, success in (({'error_kind': 'auth', 'error': 'denied'}, False),
                              ({'error_kind': 'config', 'error': 'invalid power'}, False),
                              ({'reachable': True, 'converged': True}, True)):
            ok, _ = wait(['docker'], 10, 120, run=lambda *a, **k:
                         SimpleNamespace(returncode=0, stdout=json.dumps(body)))
            self.assertEqual(ok, success)


if __name__ == '__main__':
    unittest.main()
