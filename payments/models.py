from django.db import models


class PaymentEvent(models.Model):
    """One checkout. Holds one or more payment orders."""
    checkout_id = models.CharField(max_length=64, primary_key=True)
    buyer_id = models.CharField(max_length=64)
    is_payment_done = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)


class PaymentOrder(models.Model):
    class Status(models.TextChoices):
        NOT_STARTED = "NOT_STARTED"
        EXECUTING = "EXECUTING"
        SUCCESS = "SUCCESS"
        FAILED = "FAILED"

    payment_order_id = models.CharField(max_length=64, primary_key=True)
    event = models.ForeignKey(PaymentEvent, on_delete=models.PROTECT, related_name="orders")
    buyer_account = models.CharField(max_length=64)
    seller_account = models.CharField(max_length=64)
    amount_minor = models.BigIntegerField()  # 1999 = $19.99
    description = models.CharField(max_length=500, blank=True, default="")  # what was bought
    currency = models.CharField(max_length=3)  # ISO 4217, lowercase (Stripe style)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.NOT_STARTED)
    stripe_payment_intent_id = models.CharField(max_length=64, null=True, unique=True)
    wallet_updated = models.BooleanField(default=False)
    ledger_updated = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Wallet(models.Model):
    seller_account = models.CharField(max_length=64, primary_key=True)
    balance_minor = models.BigIntegerField(default=0)


class LedgerEntry(models.Model):
    """Double-entry, append-only. Never update or delete rows."""
    order = models.ForeignKey(PaymentOrder, on_delete=models.PROTECT)
    account = models.CharField(max_length=64)
    debit_minor = models.BigIntegerField(default=0)
    credit_minor = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.pk:
            raise RuntimeError("LedgerEntry is append-only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("LedgerEntry is append-only")


class StripeEvent(models.Model):
    """Webhook dedup: Stripe delivers at-least-once."""
    event_id = models.CharField(max_length=64, primary_key=True)
    type = models.CharField(max_length=64)
    received_at = models.DateTimeField(auto_now_add=True)


class Mismatch(models.Model):
    """Reconciliation output, one row per discrepancy."""
    class Bucket(models.TextChoices):
        AUTO_FIXED = "AUTO_FIXED"          # class 1: classifiable + automated
        MANUAL = "MANUAL"                  # class 2: classifiable, finance fixes
        UNCLASSIFIED = "UNCLASSIFIED"      # class 3: needs investigation

    bucket = models.CharField(max_length=16, choices=Bucket.choices)
    payment_order_id = models.CharField(max_length=64, blank=True)
    detail = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)


class DeadLetter(models.Model):
    """Orders whose PSP call could not be completed. Needs a human."""
    class Reason(models.TextChoices):
        NON_RETRYABLE = "NON_RETRYABLE"          # e.g. invalid request, bad API key
        RETRIES_EXHAUSTED = "RETRIES_EXHAUSTED"  # transient errors that never cleared

    payment_order = models.ForeignKey(PaymentOrder, on_delete=models.PROTECT)
    reason = models.CharField(max_length=20, choices=Reason.choices)
    error = models.TextField()
    attempts = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved = models.BooleanField(default=False)
