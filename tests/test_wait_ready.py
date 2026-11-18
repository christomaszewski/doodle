"""The host readiness deadline: the container's start, radio reachability and settling are bounded
separately, and a hung Docker exec cannot outlast any of them."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from wait_ready import wait


def reply(body):
    return SimpleNamespace(returncode=0, stdout=json.dumps(body))


NO_SUPERVISOR = SimpleNamespace(returncode=1, stdout='')  # exec failed: nothing answers yet


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        self.clock = lambda: self.now[0]

    def sleep(self, seconds):
        self.now[0] += seconds

    def test_hung_exec_is_bounded_by_the_start_budget(self):
        def run(command, **kw):
            self.now[0] += kw['timeout']
            raise subprocess.TimeoutExpired(command, kw['timeout'])
        ok, reason = wait(['docker'], 5, 3, 120, clock=self.clock, sleep=self.sleep, run=run)
        self.assertFalse(ok)
        self.assertEqual(self.now[0], 5)
        self.assertIn('supervisor did not answer within 5s', reason)
        self.assertNotIn('unreachable', reason)  # nothing is known about the radio yet

    def test_a_cold_start_does_not_spend_the_radio_budget(self):
        """Measured through `rig up` on a cold start: 8.4 s before the supervisor's first log
        line and 11.9 s to its first radio write, with a radio that answers at once. The radio
        budget (10 s) starts when the supervisor answers, not when compose returns."""
        def run(command, **kw):
            if self.now[0] < 9:
                return NO_SUPERVISOR
            return reply({'reachable': self.now[0] >= 12, 'converged': self.now[0] >= 12})
        ok, reason = wait(['docker'], 30, 10, 120, clock=self.clock, sleep=self.sleep, run=run)
        self.assertTrue(ok, reason)
        self.assertLess(self.now[0], 13)

    def test_an_unreachable_radio_fails_on_its_own_budget_after_the_supervisor_answers(self):
        def run(command, **kw):
            return NO_SUPERVISOR if self.now[0] < 4 else reply({'reachable': False})
        ok, reason = wait(['docker'], 30, 3, 120, clock=self.clock, sleep=self.sleep, run=run)
        self.assertFalse(ok)
        self.assertEqual(self.now[0], 7)  # answered at 4 s, then 3 s for the radio
        self.assertIn('radio unreachable within 3s', reason)

    def test_reachable_radio_gets_separate_settle_budget(self):
        def run(command, **kw):
            return reply({'reachable': self.now[0] >= 1})
        ok, reason = wait(['docker'], 5, 3, 8, clock=self.clock, sleep=self.sleep, run=run)
        self.assertFalse(ok)
        self.assertEqual(self.now[0], 9)
        self.assertIn('did not reach its target within 8s', reason)

    def test_terminal_errors_fail_immediately_and_convergence_succeeds(self):
        for body, success in (({'error_kind': 'auth', 'error': 'denied'}, False),
                              ({'error_kind': 'config', 'error': 'invalid power'}, False),
                              ({'reachable': True, 'converged': True}, True)):
            ok, _ = wait(['docker'], 30, 10, 120, run=lambda *a, **k: reply(body))
            self.assertEqual(ok, success)


if __name__ == '__main__':
    unittest.main()
