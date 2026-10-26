from unittest import mock

from django.test import Client, TestCase, override_settings

from payments import catalog, services
from payments.models import PaymentEvent, PaymentOrder

from .helpers import ORDERS, authed_client, fake_pi, post_json

RACKET, SHOES, SHIRT = "babolat-pure-drive", "wilson-rush-pro", "uniqlo-dry-ex"


class CatalogTests(TestCase):
    def test_the_store_sells_three_items_priced_in_cents(self):
        self.assertEqual([p.id for p in catalog.PRODUCTS], [RACKET, SHOES, SHIRT])
        self.assertEqual([p.brand for p in catalog.PRODUCTS], ["Babolat", "Wilson", "Uniqlo"])
        self.assertTrue(all(isinstance(p.price_minor, int) and p.price_minor > 0 for p in catalog.PRODUCTS))

    def test_prices_a_cart_and_describes_it(self):
        lines, total, description = catalog.price_cart([{"id": RACKET, "qty": 2}, {"id": SHIRT, "qty": 1}])
        self.assertEqual(total, 2 * 24900 + 2990)
        self.assertEqual([line["qty"] for line in lines], [2, 1])
        self.assertEqual(description, "Babolat Pure Drive Racket ×2, Uniqlo Dry-EX Tennis Shirt ×1")

    def test_rejects_carts_it_cannot_price(self):
        bad = [None, [], "x", {}, [{"id": "nope", "qty": 1}], [{"id": RACKET, "qty": 0}],
               [{"id": RACKET, "qty": catalog.MAX_QTY + 1}], [{"id": RACKET, "qty": "2"}],
               [{"id": RACKET, "qty": 1.5}], [{"id": RACKET, "qty": True}], [{"id": RACKET}],
               [{"id": RACKET, "qty": 1}, {"id": RACKET, "qty": 1}], ["racket"]]
        for cart in bad:
            with self.assertRaises(catalog.CartError, msg=repr(cart)):
                catalog.price_cart(cart)


@override_settings(STRIPE_SECRET_KEY="sk_test_x")
class DemoCheckoutTests(TestCase):
    def setUp(self):
        patcher = mock.patch("payments.services.stripe.PaymentIntent")
        self.PI = patcher.start()
        self.addCleanup(patcher.stop)
        self.PI.create.return_value = fake_pi()
        self.PI.retrieve.return_value = fake_pi()
        self.client = authed_client("shopper")

    def start(self, checkout_id, items, **extra):
        return post_json(self.client, "/demo/start", {"checkout_id": checkout_id, "items": items, **extra})

    def test_requires_login(self):
        r = post_json(Client(), "/demo/start", {"checkout_id": "c1", "items": [{"id": RACKET, "qty": 1}]})
        self.assertEqual(r.status_code, 401)

    def test_server_prices_the_cart_and_ignores_client_prices(self):
        r = self.start("c1", [{"id": RACKET, "qty": 2, "price_minor": 1, "amount_minor": 1},
                              {"id": SHIRT, "qty": 1}], amount_minor=1, total=1)
        self.assertEqual(r.status_code, 200)
        kwargs = self.PI.create.call_args.kwargs
        self.assertEqual(kwargs["amount"], 52790)  # 2 x $249.00 + $29.90, whatever the browser claimed
        self.assertEqual(kwargs["description"], "Babolat Pure Drive Racket ×2, Uniqlo Dry-EX Tennis Shirt ×1")
        order = PaymentOrder.objects.get()
        self.assertEqual((order.amount_minor, order.seller_account), (52790, catalog.STORE_ACCOUNT))
        self.assertEqual(order.description, kwargs["description"])
        self.assertEqual(r.json()["payment_orders"][0]["description"], order.description)

    def test_invalid_carts_are_rejected_before_anything_is_created(self):
        for items in (None, [], [{"id": "nope", "qty": 1}], [{"id": RACKET, "qty": 99}]):
            r = self.start("c1", items)
            self.assertEqual(r.status_code, 400, items)
            self.assertEqual(r.json()["error"], "invalid_cart")
        self.assertEqual(PaymentOrder.objects.count(), 0)
        self.PI.create.assert_not_called()

    def test_checkout_id_is_required(self):
        self.assertEqual(post_json(self.client, "/demo/start", {"items": [{"id": RACKET, "qty": 1}]}).status_code, 400)

    def test_same_checkout_and_cart_is_idempotent(self):
        cart = [{"id": RACKET, "qty": 1}, {"id": SHOES, "qty": 1}]
        self.assertEqual(self.start("c1", cart).status_code, 200)
        self.assertEqual(self.start("c1", cart).status_code, 200)
        self.assertEqual(self.PI.create.call_count, 1)
        self.assertEqual(PaymentOrder.objects.count(), 1)

    def test_same_checkout_with_a_different_cart_is_refused(self):
        self.assertEqual(self.start("c1", [{"id": RACKET, "qty": 1}]).status_code, 200)
        r = self.start("c1", [{"id": RACKET, "qty": 2}])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(PaymentOrder.objects.get().amount_minor, 24900)  # the original order is untouched


class IdempotencyPayloadTests(TestCase):
    """Reusing a checkout_id with a different amount must fail, not return the old order."""

    def setUp(self):
        patcher = mock.patch("payments.services.stripe.PaymentIntent")
        self.PI = patcher.start()
        self.addCleanup(patcher.stop)
        self.PI.create.return_value = fake_pi()

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_conflict_on_changed_amount_seller_or_currency(self):
        services.create_payment(checkout_id="c1", buyer_id="b", orders=ORDERS[:1])
        for change in ({"amount_minor": 1}, {"seller_account": "mallory"}, {"currency": "EUR"},
                       {"description": "something else"}):
            with self.assertRaises(services.ConflictError, msg=change):
                services.create_payment(checkout_id="c1", buyer_id="b", orders=[{**ORDERS[0], **change}])
        self.assertEqual(PaymentEvent.objects.count(), 1)

    @override_settings(STRIPE_SECRET_KEY="sk_test_x")
    def test_identical_payload_differing_only_in_currency_case_is_a_duplicate(self):
        services.create_payment(checkout_id="c1", buyer_id="b", orders=ORDERS[:1])  # "USD"
        services.create_payment(checkout_id="c1", buyer_id="b", orders=[{**ORDERS[0], "currency": "usd"}])
        self.assertEqual(self.PI.create.call_count, 1)
