from datetime import timedelta

import stripe
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Sum
from django.utils import timezone

from payments import services
from payments.models import LedgerEntry, Mismatch, PaymentOrder


class Command(BaseCommand):
    help = "Compare our DB with Stripe and the ledger with itself; classify mismatches."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=1)

    def handle(self, *args, **opts):
        stripe.api_key = settings.STRIPE_SECRET_KEY
        since = timezone.now() - timedelta(days=opts["days"])

        stripe_paid = {}
        for pi in stripe.PaymentIntent.list(created={"gte": int(since.timestamp())},
                                            limit=100).auto_paging_iter():
            oid = (pi.to_dict().get("metadata") or {}).get("payment_order_id")
            if oid and pi.status == "succeeded":
                stripe_paid[oid] = pi.amount_received

        ours = {o.payment_order_id: o for o in PaymentOrder.objects.filter(created_at__gte=since)}
        n = 0

        for oid, amount in stripe_paid.items():
            o = ours.get(oid)
            if o is None:
                n += self.flag("UNCLASSIFIED", oid, "Succeeded at Stripe, unknown to our DB")
            elif o.status != "SUCCESS":
                services.mark_success(oid)  # class 1: cause known, cheap to automate
                n += self.flag("AUTO_FIXED", oid, f"Stripe succeeded but we had {o.status}; applied mark_success")
            elif o.amount_minor != amount:
                n += self.flag("MANUAL", oid, f"Amount differs: ours {o.amount_minor}, Stripe {amount}")
        for oid, o in ours.items():
            if o.status == "SUCCESS" and oid not in stripe_paid:
                n += self.flag("MANUAL", oid, "We say SUCCESS, Stripe has no succeeded payment in window")

        t = LedgerEntry.objects.aggregate(d=Sum("debit_minor"), c=Sum("credit_minor"))
        if (t["d"] or 0) != (t["c"] or 0):
            n += self.flag("UNCLASSIFIED", "", f"Ledger imbalance: debits {t['d']} vs credits {t['c']}")

        self.stdout.write(f"reconciled {len(ours)} local / {len(stripe_paid)} Stripe orders, {n} mismatches")

    def flag(self, bucket, oid, detail):
        Mismatch.objects.create(bucket=bucket, payment_order_id=oid, detail=detail)
        self.stderr.write(f"[{bucket}] {oid}: {detail}")
        return 1
