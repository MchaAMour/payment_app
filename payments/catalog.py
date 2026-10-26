"""The demo store's catalog.

Prices live here, on the server. The browser only ever sends product ids and quantities, so it can
never decide how much a payment is for. Prices are demo values, in integer minor units (cents).
"""
from dataclasses import dataclass

STORE_NAME = "Baseline Tennis"
STORE_ACCOUNT = "baseline_tennis"  # the seller account that is credited in the wallet and ledger
CURRENCY = "usd"
MAX_QTY = 10


@dataclass(frozen=True)
class Product:
    id: str
    brand: str
    name: str
    tagline: str
    details: tuple
    price_minor: int
    art: str  # id of the SVG illustration in templates/payments/_art.html

    @property
    def title(self):
        return f"{self.brand} {self.name}"


PRODUCTS = (
    Product("babolat-pure-drive", "Babolat", "Pure Drive Racket",
            "Power and spin from the baseline.",
            ("100 sq in head", "16×19 string pattern", "Graphite frame"), 24900, "racket"),
    Product("wilson-rush-pro", "Wilson", "Rush Pro Tennis Shoes",
            "Stable, cushioned and built for hard courts.",
            ("Hard-court outsole", "Cushioned midsole", "Breathable upper"), 15000, "shoes"),
    Product("uniqlo-dry-ex", "Uniqlo", "Dry-EX Tennis Shirt",
            "Light, quick-drying and easy to move in.",
            ("Quick-dry fabric", "Breathable mesh", "Regular fit"), 2990, "shirt"),
)
BY_ID = {p.id: p for p in PRODUCTS}


class CartError(ValueError):
    """The submitted cart is not something we can price."""


def price_cart(items):
    """Validate a cart of [{"id": ..., "qty": ...}] and price it from the catalog.

    Returns (lines, total_minor, description). Anything the browser sends besides ids and
    quantities (prices, names) is ignored.
    """
    if not isinstance(items, list) or not items:
        raise CartError("cart is empty")
    lines, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise CartError("invalid cart line")
        pid, qty = item.get("id"), item.get("qty")
        if pid not in BY_ID:
            raise CartError(f"unknown product: {pid!r}")
        if pid in seen:
            raise CartError(f"duplicate product: {pid}")
        if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= MAX_QTY:
            raise CartError(f"quantity must be a whole number from 1 to {MAX_QTY}")
        seen.add(pid)
        product = BY_ID[pid]
        lines.append({"product": product, "qty": qty, "line_minor": product.price_minor * qty})
    total = sum(line["line_minor"] for line in lines)
    description = ", ".join(f"{line['product'].title} ×{line['qty']}" for line in lines)
    return lines, total, description
