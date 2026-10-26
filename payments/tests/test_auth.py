from unittest import mock

from django.test import Client, TestCase, override_settings

from payments import services
from payments.models import PaymentEvent

from .helpers import ORDERS, SECRET, authed_client, fake_pi, pi_event, post_json, signed_post


@override_settings(STRIPE_WEBHOOK_SECRET=SECRET, STRIPE_SECRET_KEY="sk_test_x")
class JwtAuthTests(TestCase):
    def setUp(self):
        patcher = mock.patch("payments.services.stripe.PaymentIntent")
        self.PI = patcher.start()
        self.addCleanup(patcher.stop)
        self.PI.create.side_effect = [fake_pi(id="pi_1"), fake_pi(id="pi_2")]
        self.PI.retrieve.return_value = fake_pi()
        self.client = Client()

    def test_register_and_login_issue_jwt(self):
        r = post_json(self.client, "/auth/register", {"username": "zed", "password": "longenough1"})
        self.assertEqual(r.status_code, 201)
        r = post_json(self.client, "/auth/token", {"username": "zed", "password": "longenough1"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("access", r.json()); self.assertIn("refresh", r.json())
        self.assertEqual(post_json(self.client, "/auth/token", {"username": "zed", "password": "bad"}).status_code, 401)
        self.assertEqual(post_json(self.client, "/auth/register", {"username": "zed", "password": "longenough1"}).status_code, 409)
        self.assertEqual(post_json(self.client, "/auth/register", {"username": "x", "password": "short"}).status_code, 400)

    def test_payment_api_requires_jwt(self):
        r = post_json(self.client, "/v1/payments", {"checkout_id": "c9", "payment_orders": ORDERS})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.client.get("/v1/payments/o1").status_code, 401)
        bad = Client(headers={"Authorization": "Bearer not.a.token"})
        self.assertEqual(bad.get("/v1/me").status_code, 401)

    def test_buyer_comes_from_token_not_body(self):
        c = authed_client("alice")
        r = post_json(c, "/v1/payments", {"checkout_id": "cj", "buyer_info": {"id": "mallory"},
                                              "payment_orders": ORDERS[:1]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(PaymentEvent.objects.get(pk="cj").buyer_id, "alice")

    def test_api_duplicate_request_is_idempotent(self):
        c = authed_client("alice")
        body = {"checkout_id": "cj", "payment_orders": ORDERS[:1]}
        r1 = post_json(c, "/v1/payments", body).json()
        r2 = post_json(c, "/v1/payments", body).json()
        self.assertEqual(self.PI.create.call_count, 1)
        self.assertEqual(r1["payment_orders"][0]["payment_order_id"], r2["payment_orders"][0]["payment_order_id"])

    def test_invalid_body_is_400(self):
        c = authed_client("alice")
        self.assertEqual(post_json(c, "/v1/payments", {"checkout_id": "c9"}).status_code, 400)

    def test_buyers_cannot_see_or_hijack_each_others_payments(self):
        alice, bob = authed_client("alice"), authed_client("bob")
        post_json(alice, "/v1/payments", {"checkout_id": "ca", "payment_orders": ORDERS[:1]})
        self.assertEqual(bob.get("/v1/payments/o1").status_code, 404)
        r = post_json(bob, "/v1/payments", {"checkout_id": "ca", "payment_orders": ORDERS[:1]})
        self.assertEqual(r.status_code, 404)  # same checkout_id, different buyer: not revealed
        d = bob.get("/v1/dashboard").json()
        self.assertEqual(d["orders"], [])
        self.assertNotIn("mismatches", d)  # staff only

    def test_webhook_needs_signature_not_jwt(self):
        services.create_payment(checkout_id="c1", buyer_id="b", orders=ORDERS)
        r = signed_post(self.client, pi_event("evt_1", "payment_intent.succeeded", "o1"))
        self.assertEqual(r.status_code, 200)  # no Authorization header at all

    @override_settings(ALLOW_REGISTRATION=False)
    def test_registration_can_be_closed(self):
        r = post_json(self.client, "/auth/register", {"username": "late", "password": "longenough1"})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn(b'id="reg"', self.client.get("/login/").content)
        self.assertEqual(post_json(self.client, "/auth/token", {"username": "late", "password": "x"}).status_code, 401)

    def test_registration_button_is_shown_when_open(self):
        self.assertIn(b'id="reg"', self.client.get("/login/").content)
