from django.contrib.auth import get_user_model
from django.test import TestCase

from payments.models import DeadLetter, LedgerEntry, PaymentEvent, PaymentOrder


class AdminTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_superuser("root", password="pw-12345678")
        self.client.login(username="root", password="pw-12345678")
        event = PaymentEvent.objects.create(checkout_id="c1", buyer_id="b")
        self.order = PaymentOrder.objects.create(payment_order_id="o1", event=event, buyer_account="b",
                                                 seller_account="alice", amount_minor=100, currency="usd")

    def test_money_records_are_visible_but_not_editable(self):
        for model in ("paymentorder", "ledgerentry", "wallet", "paymentevent", "stripeevent", "mismatch"):
            self.assertEqual(self.client.get(f"/admin/payments/{model}/").status_code, 200, model)
        self.assertEqual(self.client.get("/admin/payments/paymentorder/add/").status_code, 403)
        self.assertEqual(self.client.post("/admin/payments/paymentorder/o1/delete/", {"post": "yes"}).status_code, 403)

    def test_ledger_entries_cannot_be_deleted_even_through_admin_actions(self):
        entry = LedgerEntry.objects.create(order=self.order, account="b", debit_minor=100)
        self.client.post("/admin/payments/ledgerentry/", {"action": "delete_selected", "_selected_action": [entry.pk],
                                                          "post": "yes"})
        self.assertTrue(LedgerEntry.objects.filter(pk=entry.pk).exists())

    def test_dead_letter_can_be_marked_resolved(self):
        dl = DeadLetter.objects.create(payment_order=self.order, reason="NON_RETRYABLE", error="x")
        r = self.client.post(f"/admin/payments/deadletter/{dl.pk}/change/", {"resolved": "on", "_save": "Save"})
        self.assertEqual(r.status_code, 302)
        dl.refresh_from_db()
        self.assertTrue(dl.resolved)
        self.assertEqual(dl.reason, "NON_RETRYABLE")  # everything else stays read-only
