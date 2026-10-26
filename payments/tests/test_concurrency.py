"""Real concurrency against Postgres: threads, separate connections, no mocks of the database."""
import threading
from unittest import mock

from django.db import connection
from django.db.models import Sum
from django.test import TransactionTestCase, override_settings

from payments import services
from payments.models import LedgerEntry, PaymentEvent, PaymentOrder, Wallet

from .helpers import ORDERS, fake_pi


def run_parallel(fn, n):
    """Run fn() in n threads released at the same moment; return the exceptions (if any)."""
    barrier, errors = threading.Barrier(n), []

    def worker():
        try:
            barrier.wait()
            fn()
        except Exception as e:  # noqa: BLE001 - collected and asserted by the test
            errors.append(e)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return errors


@override_settings(STRIPE_SECRET_KEY="sk_test_x")
class ConcurrencyTests(TransactionTestCase):
    def setUp(self):
        patcher = mock.patch("payments.services.stripe.PaymentIntent")
        self.PI = patcher.start()
        self.addCleanup(patcher.stop)
        self.PI.create.side_effect = lambda **kw: fake_pi(id="pi_1")  # Stripe dedups on the idempotency key

    def test_simultaneous_duplicate_checkouts_create_one_order(self):
        errors = run_parallel(
            lambda: services.create_payment(checkout_id="c1", buyer_id="b", orders=ORDERS[:1]), 8)
        self.assertEqual(errors, [])
        self.assertEqual(PaymentEvent.objects.count(), 1)
        self.assertEqual(PaymentOrder.objects.count(), 1)
        # however many requests reached Stripe, they all carried the same idempotency key
        self.assertEqual({c.kwargs["idempotency_key"] for c in self.PI.create.call_args_list}, {"o1"})
        self.assertEqual(PaymentOrder.objects.get().status, "EXECUTING")

    def test_simultaneous_success_signals_credit_the_wallet_once(self):
        """The webhook, the poller and reconciliation can all report the same success at once."""
        services.create_payment(checkout_id="c1", buyer_id="b", orders=ORDERS[:1])
        errors = run_parallel(lambda: services.mark_success("o1"), 8)
        self.assertEqual(errors, [])
        self.assertEqual(Wallet.objects.get(pk="alice").balance_minor, 2500)
        self.assertEqual(LedgerEntry.objects.count(), 2)
        totals = LedgerEntry.objects.aggregate(d=Sum("debit_minor"), c=Sum("credit_minor"))
        self.assertEqual(totals["d"], totals["c"])
        self.assertTrue(PaymentEvent.objects.get(pk="c1").is_payment_done)

    def test_success_racing_with_failure_always_ends_in_success(self):
        services.create_payment(checkout_id="c1", buyer_id="b", orders=ORDERS[:1])
        flip = iter([services.mark_success, services.mark_failed] * 4)
        lock = threading.Lock()

        def call():
            with lock:
                fn = next(flip)
            fn("o1")
        errors = run_parallel(call, 8)
        self.assertEqual(errors, [])
        self.assertEqual(PaymentOrder.objects.get(pk="o1").status, "SUCCESS")
        self.assertEqual(Wallet.objects.get(pk="alice").balance_minor, 2500)
