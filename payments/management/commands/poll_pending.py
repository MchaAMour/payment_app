from datetime import timedelta

import stripe
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from payments import services
from payments.models import PaymentOrder


class Command(BaseCommand):
    help = ("Recover stuck orders: poll Stripe for EXECUTING ones (lost webhook, 3DS, manual review) "
            "and re-queue NOT_STARTED ones whose retry task was never queued (broker was down).")

    def add_arguments(self, parser):
        parser.add_argument("--older-than-minutes", type=int, default=30)

    def handle(self, *args, **opts):
        stripe.api_key = settings.STRIPE_SECRET_KEY
        cutoff = timezone.now() - timedelta(minutes=opts["older_than_minutes"])
        stuck = PaymentOrder.objects.filter(status="EXECUTING", updated_at__lt=cutoff)
        for po in stuck:
            pi = stripe.PaymentIntent.retrieve(po.stripe_payment_intent_id)
            if pi.status == "succeeded":
                services.mark_success(po.payment_order_id)
                self.stdout.write(f"fixed {po.payment_order_id}: Stripe succeeded, webhook was missed")
            elif pi.status == "canceled":
                services.mark_failed(po.payment_order_id)
            else:
                self.stderr.write(f"ALERT {po.payment_order_id} still '{pi.status}' at Stripe")
        self.stdout.write(f"checked {stuck.count()} in-flight orders")

        orphans = PaymentOrder.objects.filter(status="NOT_STARTED", updated_at__lt=cutoff)
        for po in orphans:
            services.enqueue_start(po.payment_order_id)  # the task is a no-op if already started
            self.stdout.write(f"re-queued {po.payment_order_id}: never reached the PSP")
        self.stdout.write(f"re-queued {orphans.count()} not-started orders")
