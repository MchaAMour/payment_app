
import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from . import catalog, services
from .models import DeadLetter, LedgerEntry, Mismatch, PaymentOrder, StripeEvent, Wallet

User = get_user_model()

# ---- auth ----------------------------------------------------------------
# Tokens come from simplejwt's POST /auth/token (login) and /auth/token/refresh.

@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def register(request):
    if not settings.ALLOW_REGISTRATION:
        return Response({"error": "registration_closed"}, status=status.HTTP_403_FORBIDDEN)
    username = str(request.data.get("username", "")).strip()
    password = str(request.data.get("password", ""))
    if not username or len(password) < 8:
        return Response({"error": "username required, password must be 8+ chars"},
                        status=status.HTTP_400_BAD_REQUEST)
    if User.objects.filter(username=username).exists():
        return Response({"error": "username_taken"}, status=status.HTTP_409_CONFLICT)
    User.objects.create_user(username=username, password=password)
    return Response({"username": username}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
def me(request):
    return Response({"username": request.user.username, "is_staff": request.user.is_staff})


# ---- payments API ---------------------------------------------------------

@api_view(["POST"])
def create_payment(request):
    """POST /v1/payments. The buyer is the authenticated user, never the request body."""
    try:
        body = request.data
        checkout_id = str(body["checkout_id"])
        orders = body["payment_orders"]
        for o in orders:
            o["payment_order_id"], o["seller_account"], int(o["amount_minor"]), o["currency"]
    except (KeyError, TypeError, ValueError):
        return Response({"error": "invalid_request"}, status=400)
    return _create(request.user.username, checkout_id, orders)


@api_view(["POST"])
def demo_start(request):
    """Checkout for the demo store. The browser sends product ids and quantities only; the server
    prices the cart from the catalog and builds the single payment order."""
    checkout_id = request.data.get("checkout_id")
    if not checkout_id:
        return Response({"error": "invalid_request"}, status=400)
    try:
        _, total, description = catalog.price_cart(request.data.get("items"))
    except catalog.CartError as e:
        return Response({"error": "invalid_cart", "detail": str(e)}, status=400)
    orders = [{
        "payment_order_id": f"{checkout_id}-0",
        "seller_account": catalog.STORE_ACCOUNT,
        "amount_minor": total,
        "currency": catalog.CURRENCY,
        "description": description,
    }]
    return _create(request.user.username, str(checkout_id), orders)


def _create(buyer, checkout_id, orders):
    try:
        results = services.create_payment(checkout_id=checkout_id, buyer_id=buyer, orders=orders)
    except services.ConflictError:
        return Response({"error": "checkout_id_reused_with_different_orders"}, status=409)
    except services.ForbiddenError:
        return Response({"error": "not_found"}, status=404)  # another buyer's checkout: do not reveal it
    except stripe.StripeError as e:
        return Response({"error": "psp_error", "detail": str(e)}, status=502)
    return Response({"checkout_id": checkout_id, "payment_orders": results})


@api_view(["GET"])
def get_payment(request, payment_order_id):
    po = PaymentOrder.objects.filter(pk=payment_order_id, buyer_account=request.user.username).first()
    if not po:
        return Response({"error": "not_found"}, status=404)
    return Response({"payment_order_id": po.payment_order_id, "status": po.status})


@api_view(["GET"])
def dashboard_data(request):
    """Everything the dashboard shows, scoped to the logged-in buyer."""
    u = request.user.username
    orders = PaymentOrder.objects.filter(buyer_account=u).order_by("-created_at")[:20]
    sellers = {o.seller_account for o in PaymentOrder.objects.filter(buyer_account=u)}
    totals = LedgerEntry.objects.aggregate(d=Sum("debit_minor"), c=Sum("credit_minor"))
    d, c = totals["d"] or 0, totals["c"] or 0
    data = {
        "user": u,
        "orders": [{"id": o.payment_order_id, "seller": o.seller_account, "amount_minor": o.amount_minor,
                    "status": o.status, "description": o.description, "wallet_updated": o.wallet_updated,
                    "ledger_updated": o.ledger_updated} for o in orders],
        "wallets": [{"seller": w.seller_account, "balance_minor": w.balance_minor}
                    for w in Wallet.objects.filter(seller_account__in=sellers)],
        "ledger": [{"id": e.id, "account": e.account, "debit_minor": e.debit_minor,
                    "credit_minor": e.credit_minor, "order": e.order_id}
                   for e in LedgerEntry.objects.filter(order__buyer_account=u).order_by("-id")[:20]],
        "debits": d, "credits": c, "balanced": d == c,
    }
    if request.user.is_staff:  # reconciliation output is an ops concern
        data["mismatches"] = [{"bucket": m.bucket, "order": m.payment_order_id, "detail": m.detail}
                              for m in Mismatch.objects.order_by("-id")[:10]]
        data["dead_letters"] = [{"order": d.payment_order_id, "reason": d.reason, "attempts": d.attempts,
                                 "error": d.error[:160]}
                                for d in DeadLetter.objects.filter(resolved=False).order_by("-id")[:10]]
    return Response(data)


# ---- Stripe webhook (authenticated by Stripe's signature, not a JWT) -------

@csrf_exempt
@require_POST
def stripe_webhook(request):
    try:
        event = stripe.Webhook.construct_event(
            request.body,  # raw bytes: the signature covers the exact payload
            request.META.get("HTTP_STRIPE_SIGNATURE", ""),
            settings.STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError):
        return HttpResponse(status=400)

    event = event.to_dict()  # stripe v16 objects are not dicts; use plain dicts from here on

    # Dedup and handler share a transaction: a crash rolls back both, so Stripe's retry reprocesses.
    try:
        with transaction.atomic():
            StripeEvent.objects.create(event_id=event["id"], type=event["type"])
            obj = event["data"]["object"]
            order_id = (obj.get("metadata") or {}).get("payment_order_id")
            if order_id and PaymentOrder.objects.filter(pk=order_id).exists():
                if event["type"] == "payment_intent.succeeded":
                    services.mark_success(order_id)
                elif event["type"] == "payment_intent.payment_failed":
                    services.mark_failed(order_id)
    except IntegrityError:
        pass  # already processed
    return HttpResponse(status=200)


# ---- pages (shells: data and auth come from the JWT API via JS) ---------------

def login_page(request):
    return render(request, "payments/login.html", {
        "store_name": catalog.STORE_NAME, "allow_registration": settings.ALLOW_REGISTRATION})


def _products():
    return [{"id": p.id, "brand": p.brand, "name": p.name, "title": p.title, "tagline": p.tagline,
             "details": list(p.details), "price_minor": p.price_minor, "art": p.art}
            for p in catalog.PRODUCTS]


def store_page(request):
    return render(request, "payments/store.html", {
        "store_name": catalog.STORE_NAME, "products": _products(), "max_qty": catalog.MAX_QTY})


def checkout_page(request):
    return render(request, "payments/checkout.html", {
        "store_name": catalog.STORE_NAME, "publishable_key": settings.STRIPE_PUBLISHABLE_KEY,
        "products": _products(), "max_qty": catalog.MAX_QTY})


def dashboard(request):
    return render(request, "payments/dashboard.html", {"store_name": catalog.STORE_NAME})
