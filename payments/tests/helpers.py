import hashlib
import hmac
import json
import time
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import Client

SECRET = "whsec_test"
PASSWORD = "pw-12345678"

ORDERS = [
    {"payment_order_id": "o1", "seller_account": "alice", "amount_minor": 2500, "currency": "USD"},
    {"payment_order_id": "o2", "seller_account": "bob", "amount_minor": 4800, "currency": "USD"},
]


def fake_pi(**kw):
    return SimpleNamespace(id=kw.get("id", "pi_1"), client_secret="pi_1_secret_x")


def signed_post(client, payload: dict):
    """POST a webhook the way Stripe does: HMAC-SHA256 over '<timestamp>.<body>'."""
    body = json.dumps(payload)
    ts = int(time.time())
    sig = hmac.new(SECRET.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return client.post("/webhooks/stripe/", data=body, content_type="application/json",
                       HTTP_STRIPE_SIGNATURE=f"t={ts},v1={sig}")


def pi_event(event_id, typ, order_id):
    return {"id": event_id, "object": "event", "type": typ,
            "data": {"object": {"id": "pi_1", "object": "payment_intent",
                                "metadata": {"payment_order_id": order_id}}}}


def post_json(client, path, body):
    return client.post(path, data=json.dumps(body), content_type="application/json")


def authed_client(username, **user_kwargs):
    """A Client carrying a real JWT for `username` (user is created on first use)."""
    User = get_user_model()
    if not User.objects.filter(username=username).exists():
        User.objects.create_user(username=username, password=PASSWORD, **user_kwargs)
    tok = post_json(Client(), "/auth/token", {"username": username, "password": PASSWORD}).json()["access"]
    return Client(headers={"Authorization": f"Bearer {tok}"}, enforce_csrf_checks=True)
