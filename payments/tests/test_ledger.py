from django.db import DatabaseError, connection, transaction
from django.test import TestCase

from payments.models import LedgerEntry, PaymentEvent, PaymentOrder


class LedgerAppendOnlyTests(TestCase):
    def setUp(self):
        event = PaymentEvent.objects.create(checkout_id="c1", buyer_id="b")
        order = PaymentOrder.objects.create(payment_order_id="o1", event=event, buyer_account="b",
                                            seller_account="alice", amount_minor=100, currency="usd")
        self.entry = LedgerEntry.objects.create(order=order, account="b", debit_minor=100)

    def test_instance_update_and_delete_are_refused_in_python(self):
        self.entry.debit_minor = 1
        with self.assertRaises(RuntimeError):
            self.entry.save()
        with self.assertRaises(RuntimeError):
            self.entry.delete()

    def test_queryset_update_is_refused_by_the_database(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            LedgerEntry.objects.filter(pk=self.entry.pk).update(debit_minor=1)

    def test_queryset_delete_is_refused_by_the_database(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            LedgerEntry.objects.filter(pk=self.entry.pk).delete()

    def test_raw_sql_is_refused_by_the_database(self):
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as c:
            c.execute("DELETE FROM payments_ledgerentry")
        self.assertEqual(LedgerEntry.objects.count(), 1)

    def test_inserting_is_still_allowed(self):
        LedgerEntry.objects.create(order_id="o1", account="alice", credit_minor=100)
        self.assertEqual(LedgerEntry.objects.count(), 2)
