from unittest import mock

import stripe
from django.test import TestCase, override_settings

from payments import services, tasks
from payments.models import DeadLetter, PaymentEvent, PaymentOrder

from .helpers import ORDERS, authed_client, fake_pi


@override_settings(STRIPE_SECRET_KEY="sk_test_x", PAYMENT_MAX_RETRIES=3, PAYMENT_RETRY_BASE_SECONDS=1)
class RetryQueueTests(TestCase):
    """Failure -> retryable? -> retry queue -> ... -> dead letter queue."""

    def setUp(self):
        patcher = mock.patch("payments.services.stripe.PaymentIntent")
        self.PI = patcher.start()
        self.addCleanup(patcher.stop)
        delay = mock.patch("payments.tasks.start_order_task.delay")
        self.delay = delay.start()
        self.addCleanup(delay.stop)

    def create(self):
        return services.create_payment(checkout_id="c1", buyer_id="b", orders=ORDERS[:1])

    def test_transient_error_goes_to_retry_queue_and_order_stays_not_started(self):
        self.PI.create.side_effect = stripe.APIConnectionError("network down")
        res = self.create()
        self.assertEqual(res[0]["status"], "NOT_STARTED")
        self.assertIsNone(res[0]["client_secret"])
        self.delay.assert_called_once_with("o1")
        self.assertEqual(DeadLetter.objects.count(), 0)

    def test_all_retryable_error_types_are_queued(self):
        for i, exc in enumerate([stripe.APIConnectionError("x"), stripe.RateLimitError("x"),
                                 stripe.APIError("x"), stripe.IdempotencyError("x")]):
            self.PI.create.side_effect = exc
            services.create_payment(checkout_id=f"c{i}", buyer_id="b", orders=[{**ORDERS[0], "payment_order_id": f"q{i}"}])
        self.assertEqual(self.delay.call_count, 4)

    def test_permanent_error_is_not_retried_and_is_stored(self):
        self.PI.create.side_effect = stripe.InvalidRequestError("amount too small", param="amount")
        res = self.create()
        self.assertEqual(res[0]["status"], "FAILED")
        self.delay.assert_not_called()
        dl = DeadLetter.objects.get()
        self.assertEqual(dl.reason, "NON_RETRYABLE")

    def test_worker_retries_with_same_idempotency_key_then_succeeds(self):
        self.PI.create.side_effect = [stripe.APIConnectionError("1"), stripe.APIConnectionError("2"), fake_pi()]
        PaymentEvent.objects.create(checkout_id="c1", buyer_id="b")
        PaymentOrder.objects.create(payment_order_id="o1", event_id="c1", buyer_account="b",
                                    seller_account="alice", amount_minor=2500, currency="usd")
        result = tasks.start_order_task.apply(args=["o1"]).get()
        self.assertEqual(result, "started")
        self.assertEqual(PaymentOrder.objects.get(pk="o1").status, "EXECUTING")
        self.assertEqual(self.PI.create.call_count, 3)
        self.assertEqual({c.kwargs["idempotency_key"] for c in self.PI.create.call_args_list}, {"o1"})
        self.assertEqual(DeadLetter.objects.count(), 0)

    def test_retries_exhausted_lands_in_dead_letter_queue(self):
        self.PI.create.side_effect = stripe.APIConnectionError("still down")
        PaymentEvent.objects.create(checkout_id="c1", buyer_id="b")
        PaymentOrder.objects.create(payment_order_id="o1", event_id="c1", buyer_account="b",
                                    seller_account="alice", amount_minor=2500, currency="usd")
        result = tasks.start_order_task.apply(args=["o1"]).get()
        self.assertEqual(result, "dead_lettered")
        self.assertEqual(self.PI.create.call_count, 4)  # 1 try + PAYMENT_MAX_RETRIES(3)
        self.assertEqual(PaymentOrder.objects.get(pk="o1").status, "FAILED")
        dl = DeadLetter.objects.get()
        self.assertEqual((dl.reason, dl.attempts), ("RETRIES_EXHAUSTED", 4))

    def test_task_is_noop_if_order_already_started(self):
        PaymentEvent.objects.create(checkout_id="c1", buyer_id="b")
        PaymentOrder.objects.create(payment_order_id="o1", event_id="c1", buyer_account="b", seller_account="alice",
                                    amount_minor=2500, currency="usd", status="EXECUTING")
        self.assertEqual(tasks.start_order_task.apply(args=["o1"]).get(), "skipped")
        self.PI.create.assert_not_called()

    def test_task_does_not_clobber_order_that_succeeded_meanwhile(self):
        self.PI.create.side_effect = stripe.APIConnectionError("down")
        PaymentEvent.objects.create(checkout_id="c1", buyer_id="b")
        po = PaymentOrder.objects.create(payment_order_id="o1", event_id="c1", buyer_account="b",
                                         seller_account="alice", amount_minor=2500, currency="usd")
        PaymentOrder.objects.filter(pk="o1").update(status="SUCCESS")  # webhook beat the retry
        services.dead_letter(po, "RETRIES_EXHAUSTED", "x", attempts=4)
        self.assertEqual(PaymentOrder.objects.get(pk="o1").status, "SUCCESS")

    def test_broker_down_does_not_break_the_request(self):
        self.PI.create.side_effect = stripe.APIConnectionError("down")
        self.delay.side_effect = ConnectionError("redis unreachable")
        res = self.create()
        self.assertEqual(res[0]["status"], "NOT_STARTED")  # client replay will retry

    def test_backoff_is_exponential_and_capped(self):
        lows = [tasks.backoff_seconds(n) for n in (0, 3, 20)]
        self.assertTrue(0.75 <= lows[0] <= 1.25)        # base 1s
        self.assertTrue(6 <= lows[1] <= 10)             # 1 * 2^3 = 8s +-25%
        self.assertTrue(lows[2] <= 300 * 1.25)          # capped at 5 min

    def test_dead_letters_visible_to_staff_only(self):
        self.PI.create.side_effect = stripe.InvalidRequestError("bad", param="x")
        services.create_payment(checkout_id="c1", buyer_id="staffer", orders=ORDERS[:1])
        staff, regular = authed_client("staffer", is_staff=True), authed_client("regular")
        self.assertEqual(len(staff.get("/v1/dashboard").json()["dead_letters"]), 1)
        self.assertNotIn("dead_letters", regular.get("/v1/dashboard").json())
