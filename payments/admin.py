from django.contrib import admin

from .models import (DeadLetter, LedgerEntry, Mismatch, PaymentEvent, PaymentOrder, StripeEvent,
                     Wallet)


class ReadOnlyAdmin(admin.ModelAdmin):
    """Money records are written by the payment flow only, never edited by hand."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PaymentEvent)
class PaymentEventAdmin(ReadOnlyAdmin):
    list_display = ("checkout_id", "buyer_id", "is_payment_done", "created_at")
    search_fields = ("checkout_id", "buyer_id")


@admin.register(PaymentOrder)
class PaymentOrderAdmin(ReadOnlyAdmin):
    list_display = ("payment_order_id", "buyer_account", "seller_account", "amount_minor",
                    "currency", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("payment_order_id", "stripe_payment_intent_id", "buyer_account")


@admin.register(Wallet)
class WalletAdmin(ReadOnlyAdmin):
    list_display = ("seller_account", "balance_minor")


@admin.register(LedgerEntry)
class LedgerEntryAdmin(ReadOnlyAdmin):
    list_display = ("id", "account", "debit_minor", "credit_minor", "order", "created_at")
    list_filter = ("account",)


@admin.register(StripeEvent)
class StripeEventAdmin(ReadOnlyAdmin):
    list_display = ("event_id", "type", "received_at")


@admin.register(Mismatch)
class MismatchAdmin(ReadOnlyAdmin):
    list_display = ("bucket", "payment_order_id", "detail", "created_at")
    list_filter = ("bucket",)


@admin.register(DeadLetter)
class DeadLetterAdmin(ReadOnlyAdmin):
    """Read-only except for one thing: a human marks a dead letter as resolved."""
    list_display = ("payment_order", "reason", "attempts", "resolved", "created_at")
    list_filter = ("reason", "resolved")
    fields = ("payment_order", "reason", "error", "attempts", "created_at", "resolved")
    readonly_fields = ("payment_order", "reason", "error", "attempts", "created_at")

    def has_change_permission(self, request, obj=None):
        return True
