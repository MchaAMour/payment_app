from unittest import mock

from django.db.models import Sum
from django.test import Client, TestCase, override_settings

from payments import services
from payments.models import LedgerEntry, PaymentEvent, PaymentOrder, StripeEvent, Wallet

from .helpers import ORDERS, SECRET, authed_client, fake_pi, pi_event, signed_post


@override_settings(STRIPE_WEBHOOK_SECRET=SECRET, STRIPE_SECRET_KEY="sk_test_x")
class PaymentFlowTests(TestCase):
    def setUp(self):
        patcher = mock.patch("payments.services.stripe.PaymentIntent")
        self.PI = patcher.start()
        self.addCleanup(patcher.stop)
        self.PI.create.side_effect = [fake_pi(id="pi_1"), fake_pi(id="pi_2")]
        self.PI.retrieve.return_value = fake_pi()
        self.client = Client()

    def pay(self, order_id, event_id="evt_1"):
        return signed_post(self.client, pi_event(event_id, "payment_intent.succeeded", order_id))

    def test_duplicate_request_creates_one_payment_intent_per_order(self):
        services.create_payment(checkout_id="c1", buyer_id="b", orders=ORDERS)
        services.create_payment(checkout_id="c1", buyer_id="b", orders=ORDERS)  # retry / double click
        self.assertEqual(self.PI.create.call_count, 2)  # 2 orders, not 4
        self.assertEqual(PaymentOrder.objects.count(), 2)
        keys = {c.kwargs["idempotency_key"] for c in self.PI.create.call_args_list}
        self.assertEqual(keys, {"o1", "o2"})  # PSP-side idempotency key = payment_order_id

    def test_same_checkout_different_orders_is_conflict(self):
        services.create_payment(checkout_id="c1", buyer_id="b", orders=ORDERS)
        with self.assertRaises(services.ConflictError):
            services.create_payment(checkout_id="c1", buyer_id="b", orders=ORDERS[:1])

    def test_success_updates_wallet_and_balanced_ledger(self):
        services.create_payment(checkout_id="c1", buyer_id="b", orders=ORDERS)
        self.assertEqual(self.pay("o1").status_code, 200)
        self.assertEqual(Wallet.objects.get(pk="alice").balance_minor, 2500)
        t = LedgerEntry.objects.aggregate(d=Sum("debit_minor"), c=Sum("credit_minor"))
        self.assertEqual(t["d"], t["c"])
        self.assertFalse(PaymentEvent.objects.get(pk="c1").is_payment_done)  # o2 still open
        self.pay("o2", "evt_2")
        self.assertTrue(PaymentEvent.objects.get(pk="c1").is_payment_done)

    def test_webhook_replay_applies_once(self):
        services.create_payment(checkout_id="c1", buyer_id="b", orders=ORDERS)
        self.pay("o1"); self.pay("o1")  # Stripe re-delivers the same event id
        self.assertEqual(StripeEvent.objects.count(), 1)
        self.assertEqual(Wallet.objects.get(pk="alice").balance_minor, 2500)
        self.assertEqual(LedgerEntry.objects.count(), 2)

    def test_second_event_for_same_order_is_idempotent(self):
        services.create_payment(checkout_id="c1", buyer_id="b", orders=ORDERS)
        self.pay("o1", "evt_a"); self.pay("o1", "evt_b")  # different event ids, same business effect
        self.assertEqual(Wallet.objects.get(pk="alice").balance_minor, 2500)
        self.assertEqual(LedgerEntry.objects.count(), 2)

    def test_late_failure_never_downgrades_success(self):
        services.create_payment(checkout_id="c1", buyer_id="b", orders=ORDERS)
        self.pay("o1")
        signed_post(self.client, pi_event("evt_f", "payment_intent.payment_failed", "o1"))
        self.assertEqual(PaymentOrder.objects.get(pk="o1").status, "SUCCESS")

    def test_failure_then_retry_success(self):
        services.create_payment(checkout_id="c1", buyer_id="b", orders=ORDERS)
        signed_post(self.client, pi_event("evt_f", "payment_intent.payment_failed", "o1"))
        self.assertEqual(PaymentOrder.objects.get(pk="o1").status, "FAILED")
        self.pay("o1", "evt_s")  # customer retried with another card
        self.assertEqual(PaymentOrder.objects.get(pk="o1").status, "SUCCESS")

    def test_bad_signature_rejected(self):
        r = self.client.post("/webhooks/stripe/", data="{}", content_type="application/json",
                             HTTP_STRIPE_SIGNATURE="t=1,v1=bad")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(StripeEvent.objects.count(), 0)

    def test_get_payment_status(self):
        services.create_payment(checkout_id="c1", buyer_id="b", orders=ORDERS)
        c = authed_client("b")
        self.assertEqual(c.get("/v1/payments/o1").json()["status"], "EXECUTING")
        self.assertEqual(c.get("/v1/payments/nope").status_code, 404)
