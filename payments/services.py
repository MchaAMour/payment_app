import logging

import stripe
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F

from .models import DeadLetter, LedgerEntry, PaymentEvent, PaymentOrder, Wallet

log = logging.getLogger(__name__)
S = PaymentOrder.Status

# Transient PSP failures: worth retrying (the Stripe idempotency key makes retries safe).
# Anything else from Stripe (invalid request, bad key, permissions) will not fix itself.
RETRYABLE = (stripe.APIConnectionError, stripe.APIError, stripe.RateLimitError, stripe.IdempotencyError)


class ConflictError(Exception):
    """Same checkout_id reused with different payment orders."""


class ForbiddenError(Exception):
    """checkout_id already belongs to another buyer."""


def _stripe():
    stripe.api_key = settings.STRIPE_SECRET_KEY
    if settings.STRIPE_API_BASE:
        stripe.api_base = settings.STRIPE_API_BASE
    return stripe


def create_payment(*, checkout_id, buyer_id, orders):
    """POST /v1/payments. Idempotent on checkout_id (the client's idempotency key)."""
    try:
        with transaction.atomic():
            event = PaymentEvent.objects.create(checkout_id=checkout_id, buyer_id=buyer_id)
            for o in orders:
                PaymentOrder.objects.create(
                    payment_order_id=o["payment_order_id"], event=event,
                    buyer_account=buyer_id, seller_account=o["seller_account"],
                    amount_minor=int(o["amount_minor"]), currency=o["currency"].lower(),
                    description=o.get("description", ""),
                )
    except IntegrityError:
        # Duplicate request: do not create anything, return the existing state.
        event = PaymentEvent.objects.get(checkout_id=checkout_id)
        if event.buyer_id != buyer_id:
            raise ForbiddenError(checkout_id)
        # Same key, different payload: refuse, as Stripe does, rather than silently return the old order.
        requested = {(o["payment_order_id"], o["seller_account"], int(o["amount_minor"]),
                      o["currency"].lower(), o.get("description", "")) for o in orders}
        existing = {(po.payment_order_id, po.seller_account, po.amount_minor, po.currency, po.description)
                    for po in event.orders.all()}
        if requested != existing:
            raise ConflictError(checkout_id)

    return [start_or_defer(po) for po in event.orders.order_by("created_at", "payment_order_id")]


def _order_view(po, client_secret=None):
    return {"payment_order_id": po.payment_order_id, "status": po.status,
            "amount_minor": po.amount_minor, "currency": po.currency,
            "seller_account": po.seller_account, "description": po.description,
            "client_secret": client_secret}


def start_or_defer(po):
    """Try the PSP now. Transient failure -> retry queue; permanent failure -> FAILED + dead letter."""
    try:
        return start_order(po)
    except RETRYABLE as e:
        if po.status != S.NOT_STARTED:
            raise
        log.warning("PSP call for %s failed (%s); sending to retry queue", po.payment_order_id, e)
        enqueue_start(po.payment_order_id)
        return _order_view(po)  # still NOT_STARTED; the client polls the status
    except stripe.StripeError as e:
        if po.status != S.NOT_STARTED:
            raise
        dead_letter(po, DeadLetter.Reason.NON_RETRYABLE, e, attempts=1)
        po.refresh_from_db()
        return _order_view(po)


def enqueue_start(payment_order_id):
    from .tasks import start_order_task
    try:
        start_order_task.delay(payment_order_id)
    except Exception:  # broker down: the order stays NOT_STARTED and a client replay retries it
        log.exception("could not enqueue retry for %s", payment_order_id)


def dead_letter(po, reason, error, attempts):
    """Fail the order (only if nothing else moved it) and park it for a human."""
    with transaction.atomic():
        PaymentOrder.objects.filter(pk=po.pk, status=S.NOT_STARTED).update(status=S.FAILED)
        DeadLetter.objects.create(payment_order=po, reason=reason, error=str(error)[:2000], attempts=attempts)
    log.error("dead letter %s for %s after %s attempt(s): %s", reason, po.payment_order_id, attempts, error)


def start_order(po):
    """NOT_STARTED -> EXECUTING. The Stripe call is outside any DB transaction."""
    st = _stripe()
    if po.status == S.NOT_STARTED:
        extra = {"description": po.description} if po.description else {}
        pi = st.PaymentIntent.create(
            amount=po.amount_minor,
            currency=po.currency,
            automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
            metadata={"payment_order_id": po.payment_order_id,
                      "checkout_id": po.event_id,
                      "seller_account": po.seller_account},
            idempotency_key=po.payment_order_id,  # same order -> same PaymentIntent, always
            **extra,
        )
        PaymentOrder.objects.filter(pk=po.pk, status=S.NOT_STARTED).update(
            status=S.EXECUTING, stripe_payment_intent_id=pi.id)
        po.refresh_from_db()
        client_secret = pi.client_secret
    else:
        client_secret = None
        if po.status == S.EXECUTING and po.stripe_payment_intent_id:
            client_secret = st.PaymentIntent.retrieve(po.stripe_payment_intent_id).client_secret
    return _order_view(po, client_secret)


@transaction.atomic
def mark_success(payment_order_id):
    """Idempotent. Safe to call from webhook, poller and reconciliation."""
    po = PaymentOrder.objects.select_for_update().get(pk=payment_order_id)
    if po.status == S.SUCCESS and po.wallet_updated and po.ledger_updated:
        return
    po.status = S.SUCCESS

    if not po.wallet_updated:
        Wallet.objects.get_or_create(seller_account=po.seller_account)
        Wallet.objects.filter(pk=po.seller_account).update(
            balance_minor=F("balance_minor") + po.amount_minor)
        po.wallet_updated = True

    if not po.ledger_updated:  # double entry: debit buyer, credit seller, same amount
        LedgerEntry.objects.create(order=po, account=po.buyer_account, debit_minor=po.amount_minor)
        LedgerEntry.objects.create(order=po, account=po.seller_account, credit_minor=po.amount_minor)
        po.ledger_updated = True

    po.save()
    if not po.event.orders.exclude(status=S.SUCCESS).exists():
        PaymentEvent.objects.filter(pk=po.event_id).update(is_payment_done=True)


@transaction.atomic
def mark_failed(payment_order_id):
    po = PaymentOrder.objects.select_for_update().get(pk=payment_order_id)
    if po.status != S.SUCCESS:  # never downgrade a success
        po.status = S.FAILED
        po.save(update_fields=["status", "updated_at"])
