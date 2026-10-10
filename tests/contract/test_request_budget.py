import signal
import socket
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from troubleshooter_deployment.request_budget import BudgetError, operation_budget


class BudgetTests(unittest.TestCase):
    def test_interrupts_blocked_read_and_restores_signal_state(self):
        original = signal.getsignal(signal.SIGALRM)
        left, right = socket.socketpair()
        try:
            with self.assertRaises(BudgetError), operation_budget(0.03):
                left.recv(1)
        finally:
            left.close()
            right.close()
        self.assertEqual(signal.getsignal(signal.SIGALRM), original)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_late_completion_is_rejected_without_signal_delivery(self):
        with patch("troubleshooter_deployment.request_budget.monotonic", side_effect=[0, 21]):
            with self.assertRaises(BudgetError), operation_budget():
                pass

    def test_refuses_thread_nested_alarm_and_invalid_budget_before_work(self):
        def enter():
            with operation_budget():
                self.fail("Unsupported context reached work")

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(BudgetError):
                pool.submit(enter).result()
        with operation_budget():
            with self.assertRaises(BudgetError):
                enter()
        for value in [0, -1, True, float("inf"), float("nan")]:
            with self.assertRaises(BudgetError), operation_budget(value):
                self.fail("Invalid budget reached work")

    def test_exception_restores_alarm_and_unsupported_platform_is_rejected(self):
        original = signal.getsignal(signal.SIGALRM)
        with self.assertRaises(ValueError), operation_budget():
            raise ValueError("synthetic")
        self.assertEqual(signal.getsignal(signal.SIGALRM), original)
        with patch("troubleshooter_deployment.request_budget.SUPPORTED", False):
            with self.assertRaises(BudgetError), operation_budget():
                self.fail("Unsupported platform reached work")
