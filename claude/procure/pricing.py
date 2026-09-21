"""Deterministic pricing math. The agent never does this arithmetic itself."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from .config import Policy, Vendor
from .models import Offer, PriceBreak

CENT = Decimal("0.01")


def cents(d: Decimal) -> Decimal:
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def round_up_to_multiple(q: int, mult: int) -> int:
    return q if mult <= 1 else -(-q // mult) * mult


def unit_price_at(breaks: list[PriceBreak], qty: int) -> Decimal:
    """Unit price for an order of `qty` (breaks sorted ascending by qty)."""
    applicable = [b for b in breaks if b.qty <= qty]
    if not applicable:
        raise ValueError(f"qty {qty} is below the lowest price break ({breaks[0].qty})")
    return applicable[-1].unit_price


def base_order_qty(qty: int, offer: Offer) -> int:
    """Smallest orderable quantity >= qty given MOQ, lowest break and order multiple."""
    q = max(qty, offer.moq, offer.price_breaks[0].qty)
    return round_up_to_multiple(q, offer.order_multiple)


@dataclass
class Purchase:
    needed_qty: int
    buy_qty: int
    unit_price: Decimal
    extended: Decimal  # buy_qty * unit_price, in cents
    moq_forced: bool  # had to buy more than needed because of MOQ/multiple/lowest break
    buy_up: bool  # chose a larger quantity because a higher break makes it cheaper overall
    base_qty: int  # smallest orderable quantity
    base_extended: Decimal  # extended cost at base_qty


def best_purchase(qty: int, offer: Offer, allow_buy_up: bool = True) -> Purchase:
    base = base_order_qty(qty, offer)
    base_unit = unit_price_at(offer.price_breaks, base)
    base_ext = cents(base_unit * base)
    best_q, best_unit, best_ext = base, base_unit, base_ext
    if allow_buy_up:
        for b in offer.price_breaks:
            if b.qty <= base:
                continue
            q = round_up_to_multiple(max(b.qty, offer.moq), offer.order_multiple)
            unit = unit_price_at(offer.price_breaks, q)
            ext = cents(unit * q)
            if ext < best_ext:
                best_q, best_unit, best_ext = q, unit, ext
    return Purchase(
        needed_qty=qty,
        buy_qty=best_q,
        unit_price=best_unit,
        extended=best_ext,
        moq_forced=base > qty,
        buy_up=best_q > base,
        base_qty=base,
        base_extended=base_ext,
    )


@dataclass
class Landed:
    purchase: Purchase
    shipping: Decimal | None
    shipping_source: str | None
    fees: Decimal
    tax: Decimal
    total: Decimal  # lower bound when shipping is None

    @property
    def shipping_known(self) -> bool:
        return self.shipping is not None


def resolve_shipping(offer: Offer, vendor: Vendor | None, extended: Decimal) -> tuple[Decimal | None, str | None]:
    """Captured shipping wins; otherwise the vendor's configured rule; otherwise unknown."""
    if offer.shipping is not None:
        return offer.shipping, offer.shipping_source or "captured"
    if vendor is not None and vendor.shipping_flat is not None:
        if vendor.shipping_free_over is not None and extended >= vendor.shipping_free_over:
            return Decimal("0"), "vendor rule (free over threshold)"
        return vendor.shipping_flat, "vendor rule (flat)"
    return None, None


def landed_cost(qty: int, offer: Offer, vendor: Vendor | None, policy: Policy) -> Landed:
    p = best_purchase(qty, offer, policy.allow_buy_up)
    shipping, ship_src = resolve_shipping(offer, vendor, p.extended)
    fees = cents(offer.fees)
    taxable = p.extended + fees
    if policy.tax_shipping and shipping is not None:
        taxable += shipping
    charges_tax = vendor.charges_tax if vendor is not None else True
    tax = cents(taxable * policy.tax_rate) if charges_tax else Decimal("0.00")
    total = cents(p.extended + fees + (shipping or Decimal("0")) + tax)
    return Landed(purchase=p, shipping=shipping, shipping_source=ship_src, fees=fees, tax=tax, total=total)
