import random

import stripe
from celery import shared_task
from django.conf import settings

from . import services
from .models import DeadLetter, PaymentOrder


def backoff_seconds(retries):
    """Exponential backoff with jitter: base * 2^n (2s, 4s, 8s, ...) capped at 5 min."""
    delay = min(settings.PAYMENT_RETRY_BASE_SECONDS * 2 ** retries, 300)
    return delay * random.uniform(0.75, 1.25)


@shared_task(bind=True, max_retries=None)  # the limit is enforced below from settings
def start_order_task(self, payment_order_id):
    """Retry queue consumer: register the payment with the PSP after a transient failure."""
    po = PaymentOrder.objects.get(pk=payment_order_id)
    if po.status != PaymentOrder.Status.NOT_STARTED:
        return "skipped"  # someone else (client replay, earlier attempt) already started it

    attempt = self.request.retries + 1
    try:
        services.start_order(po)
    except services.RETRYABLE as e:
        if self.request.retries >= settings.PAYMENT_MAX_RETRIES:
            services.dead_letter(po, DeadLetter.Reason.RETRIES_EXHAUSTED, e, attempts=attempt)
            return "dead_lettered"
        raise self.retry(exc=e, countdown=backoff_seconds(self.request.retries))
    except stripe.StripeError as e:
        services.dead_letter(po, DeadLetter.Reason.NON_RETRYABLE, e, attempts=attempt)
        return "dead_lettered"
    return "started"
