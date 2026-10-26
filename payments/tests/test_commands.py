from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from payments.models import LedgerEntry, Mismatch, PaymentEvent, PaymentOrder, Wallet


def make_order(oid, status="EXECUTING", amount=2500, seller="alice"):
    event, _ = PaymentEvent.objects.get_or_create(checkout_id=f"ck-{oid}", defaults={"buyer_id": "b"})
    return PaymentOrder.objects.create(
        payment_order_id=oid, event=event, buyer_account="b", seller_account=seller,
        amount_minor=amount, currency="usd", status=status, stripe_payment_intent_id=f"pi_{oid}")


def run(name, *args):
    out, err = StringIO(), StringIO()
    call_command(name, *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


class PollPendingTests(TestCase):
    def setUp(self):
        patcher = mock.patch("payments.management.commands.poll_pending.stripe.PaymentIntent")
        self.PI = patcher.start()
        self.addCleanup(patcher.stop)

    def test_executing_order_whose_intent_succeeded_is_recovered(self):
        make_order("o1")
        self.PI.retrieve.return_value = SimpleNamespace(status="succeeded")
        run("poll_pending", "--older-than-minutes", "0")
        self.assertEqual(PaymentOrder.objects.get(pk="o1").status, "SUCCESS")
        self.assertEqual(Wallet.objects.get(pk="alice").balance_minor, 2500)

    def test_still_processing_raises_an_alert_and_changes_nothing(self):
        make_order("o1")
        self.PI.retrieve.return_value = SimpleNamespace(status="requires_action")
        _, err = run("poll_pending", "--older-than-minutes", "0")
        self.assertIn("ALERT", err)
        self.assertEqual(PaymentOrder.objects.get(pk="o1").status, "EXECUTING")

    def test_canceled_intent_fails_the_order(self):
        make_order("o1")
        self.PI.retrieve.return_value = SimpleNamespace(status="canceled")
        run("poll_pending", "--older-than-minutes", "0")
        self.assertEqual(PaymentOrder.objects.get(pk="o1").status, "FAILED")

    def test_recent_orders_are_left_alone(self):
        make_order("o1")
        run("poll_pending")  # default threshold: 30 minutes
        self.PI.retrieve.assert_not_called()

    @mock.patch("payments.management.commands.poll_pending.services.enqueue_start")
    def test_not_started_orders_are_requeued(self, enqueue):
        make_order("o1", status="NOT_STARTED")
        run("poll_pending", "--older-than-minutes", "0")
        enqueue.assert_called_once_with("o1")


class ReconcileTests(TestCase):
    def setUp(self):
        patcher = mock.patch("payments.management.commands.reconcile.stripe.PaymentIntent")
        self.PI = patcher.start()
        self.addCleanup(patcher.stop)
        self.stripe_side = []
        self.PI.list.return_value.auto_paging_iter.side_effect = lambda: iter(self.stripe_side)

    def stripe_paid(self, oid, amount, status="succeeded"):
        self.stripe_side.append(SimpleNamespace(
            status=status, amount_received=amount, to_dict=lambda: {"metadata": {"payment_order_id": oid}}))

    def buckets(self):
        return sorted(Mismatch.objects.values_list("bucket", flat=True))

    def test_clean_run_reports_nothing(self):
        make_order("o1", status="SUCCESS")
        self.stripe_paid("o1", 2500)
        out, _ = run("reconcile")
        self.assertIn("0 mismatches", out)
        self.assertEqual(self.buckets(), [])

    def test_stripe_succeeded_but_we_missed_it_is_auto_fixed(self):
        make_order("o1", status="EXECUTING")
        self.stripe_paid("o1", 2500)
        run("reconcile")
        self.assertEqual(self.buckets(), ["AUTO_FIXED"])
        self.assertEqual(PaymentOrder.objects.get(pk="o1").status, "SUCCESS")
        self.assertEqual(LedgerEntry.objects.count(), 2)

    def test_success_we_cannot_confirm_at_stripe_needs_finance(self):
        make_order("o1", status="SUCCESS")
        run("reconcile")
        self.assertEqual(self.buckets(), ["MANUAL"])

    def test_amount_difference_needs_finance(self):
        make_order("o1", status="SUCCESS", amount=2500)
        self.stripe_paid("o1", 2400)
        run("reconcile")
        self.assertEqual(self.buckets(), ["MANUAL"])

    def test_payment_unknown_to_us_is_unclassified(self):
        self.stripe_paid("ghost", 100)
        run("reconcile")
        self.assertEqual(self.buckets(), ["UNCLASSIFIED"])

    def test_failed_intents_are_ignored(self):
        make_order("o1", status="FAILED")
        self.stripe_paid("o1", 2500, status="requires_payment_method")
        run("reconcile")
        self.assertEqual(self.buckets(), [])

    def test_ledger_imbalance_is_unclassified(self):
        po = make_order("o1", status="SUCCESS")
        self.stripe_paid("o1", 2500)
        LedgerEntry.objects.create(order=po, account="b", debit_minor=2500)  # no matching credit
        run("reconcile")
        self.assertEqual(self.buckets(), ["UNCLASSIFIED"])


class SeedDemoTests(TestCase):
    def test_creates_buyer_and_staff(self):
        out, _ = run("seed_demo", "--password", "demo-pass-123")
        U = get_user_model()
        self.assertFalse(U.objects.get(username="demo").is_staff)
        staff = U.objects.get(username="staff")
        self.assertTrue(staff.is_staff and staff.is_superuser)
        self.assertTrue(U.objects.get(username="demo").check_password("demo-pass-123"))
        self.assertNotIn("demo-pass-123", out)  # a password you supply is never echoed back

    def test_generated_password_is_printed_once_and_works(self):
        out, _ = run("seed_demo")
        password = out.splitlines()[0].split("password: ")[1]
        self.assertGreaterEqual(len(password), 12)
        self.assertTrue(get_user_model().objects.get(username="demo").check_password(password))

    def test_is_idempotent_and_does_not_touch_existing_passwords(self):
        run("seed_demo", "--password", "first-pass-123")
        out, _ = run("seed_demo", "--password", "second-pass-123")
        self.assertIn("already exists", out)
        self.assertTrue(get_user_model().objects.get(username="demo").check_password("first-pass-123"))

    def test_reset_changes_the_password(self):
        run("seed_demo", "--password", "first-pass-123")
        run("seed_demo", "--password", "second-pass-123", "--reset")
        self.assertTrue(get_user_model().objects.get(username="demo").check_password("second-pass-123"))
