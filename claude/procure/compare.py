"""Rank captured offers per item and explain every exclusion."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .config import Policy, Vendor
from .models import Item, Offer, Request, Verification, latest_offers, money_json
from .pricing import Landed, cents, landed_cost, unit_price_at

VERIFIED, UNVERIFIED, MISMATCH = "verified", "unverified", "mismatch"


@dataclass
class Evaluated:
    offer: Offer
    landed: Landed | None = None
    eligible: bool = True
    reasons: list[str] = field(default_factory=list)  # why excluded
    flags: list[str] = field(default_factory=list)  # caveats on eligible offers
    verification: str = UNVERIFIED
    verification_note: str = ""

    def exclude(self, reason: str) -> None:
        self.eligible = False
        self.reasons.append(reason)


@dataclass
class OptionSummary:
    option: str
    best: Evaluated
    delta: Decimal  # vs cheapest option's best landed total
    delta_pct: Decimal | None


@dataclass
class ItemResult:
    item: Item
    ranked: list[Evaluated]
    excluded: list[Evaluated]
    options: list[OptionSummary]

    @property
    def winner(self) -> Evaluated | None:
        return self.ranked[0] if self.ranked else None


# --------------------------------------------------------------------------- evaluation


def _latest_verification(offer: Offer, verifications: list[Verification]) -> Verification | None:
    relevant = [v for v in verifications if v.offer_id == offer.id and v.checked_at >= offer.captured_at]
    return max(relevant, key=lambda v: v.checked_at) if relevant else None


def _check_verification(ev: Evaluated, v: Verification | None, policy: Policy) -> None:
    offer = ev.offer
    if v is None:
        ev.verification = UNVERIFIED
        return
    problems = []
    qty = max(v.qty_checked, offer.price_breaks[0].qty)
    expected = unit_price_at(offer.price_breaks, qty)
    if abs(v.observed_unit_price - expected) > policy.verify_tolerance:
        problems.append(f"unit price at qty {v.qty_checked}: captured {expected}, observed {v.observed_unit_price}")
    if not v.specs_confirmed:
        problems.append("verifier could not confirm all spec requirements")
    if v.in_stock is False and offer.stock_qty != 0:
        problems.append("verifier saw the item out of stock")
    if (
        v.observed_shipping is not None
        and offer.shipping is not None
        and abs(v.observed_shipping - offer.shipping) > policy.verify_tolerance
    ):
        problems.append(f"shipping: captured {offer.shipping}, observed {v.observed_shipping}")
    if problems:
        ev.verification = MISMATCH
        ev.verification_note = "; ".join(problems)
    else:
        ev.verification = VERIFIED


def _max_lead_days(item: Item, request: Request, policy: Policy, today: date) -> int:
    limit = item.max_lead_time_days if item.max_lead_time_days is not None else policy.max_lead_time_days
    if request.need_by is not None:
        limit = min(limit, (request.need_by - today).days)
    return limit


def evaluate_offer(
    offer: Offer,
    item: Item,
    request: Request,
    vendors: dict[str, Vendor],
    policy: Policy,
    verifications: list[Verification],
    now: datetime,
) -> Evaluated:
    ev = Evaluated(offer=offer)
    vendor = vendors.get(offer.vendor)

    if vendor is None:
        ev.exclude(f"vendor '{offer.vendor}' is not in vendors.yaml")
    elif not vendor.approved:
        ev.exclude(f"vendor '{offer.vendor}' is not approved")

    if offer.currency != policy.currency:
        ev.exclude(f"currency {offer.currency} != policy currency {policy.currency}")

    for rid, statement in item.requirements.items():
        sc = offer.spec_checks.get(rid)
        if sc is None or sc.passed is None:
            ev.exclude(f"requirement '{rid}' not checked ({statement})")
        elif sc.passed is False:
            got = f" — found: {sc.value}" if sc.value else ""
            ev.exclude(f"fails requirement '{rid}' ({statement}){got}")
    extra = sorted(set(offer.spec_checks) - set(item.requirements))
    if extra:
        ev.flags.append(f"spec checks not in request: {', '.join(extra)}")

    try:
        ev.landed = landed_cost(item.qty, offer, vendor, policy)
    except ValueError as e:
        ev.exclude(f"pricing error: {e}")
        return ev

    p = ev.landed.purchase
    if p.moq_forced:
        ev.flags.append(f"MOQ/multiple forces buying {p.buy_qty} (need {p.needed_qty})")
    if p.buy_up:
        saved = p.base_extended - p.extended
        ev.flags.append(f"buying {p.buy_qty} instead of {p.base_qty} is ${cents(saved)} cheaper (price break)")

    max_lead = _max_lead_days(item, request, policy, now.date())
    if offer.stock_qty is None:
        ev.flags.append("stock unknown")
    elif offer.stock_qty < p.buy_qty:
        if offer.lead_time_days is None:
            ev.exclude(f"insufficient stock ({offer.stock_qty} < {p.buy_qty}) and lead time unknown")
        elif offer.lead_time_days > max_lead:
            ev.exclude(
                f"insufficient stock ({offer.stock_qty} < {p.buy_qty}); lead time {offer.lead_time_days}d > limit {max_lead}d"
            )
        else:
            ev.flags.append(f"backorder: stock {offer.stock_qty} < {p.buy_qty}, lead {offer.lead_time_days}d")

    age_h = (now - offer.captured_at).total_seconds() / 3600
    if age_h > policy.max_offer_age_hours:
        ev.flags.append(f"stale capture ({age_h:.0f}h old; recapture before buying)")

    if not ev.landed.shipping_known:
        ev.flags.append("shipping unknown — landed total is a lower bound")
    elif ev.landed.shipping_source and ev.landed.shipping_source.startswith("vendor rule"):
        ev.flags.append(f"shipping from {ev.landed.shipping_source}, not a cart quote")
    if policy.tax_rate == 0:
        ev.flags.append("tax not modelled (policy tax_rate = 0)")

    _check_verification(ev, _latest_verification(offer, verifications), policy)
    if ev.verification == MISMATCH:
        ev.exclude(f"verification mismatch: {ev.verification_note} — recapture this offer")

    return ev


def _sort_key(ev: Evaluated):
    stock_ok = ev.offer.stock_qty is not None and ev.landed is not None and ev.offer.stock_qty >= ev.landed.purchase.buy_qty
    return (
        ev.landed.total if ev.landed else Decimal("Infinity"),
        0 if ev.landed and ev.landed.shipping_known else 1,
        0 if stock_ok else 1,
        ev.offer.lead_time_days if ev.offer.lead_time_days is not None else 10**6,
        ev.offer.id,
    )


def compare(
    request: Request,
    offers: list[Offer],
    vendors: dict[str, Vendor],
    policy: Policy,
    verifications: list[Verification],
    now: datetime,
) -> list[ItemResult]:
    current = latest_offers(offers)
    results = []
    for item in request.items:
        evs = [
            evaluate_offer(o, item, request, vendors, policy, verifications, now)
            for o in current
            if o.item == item.id
        ]
        ranked = sorted([e for e in evs if e.eligible], key=_sort_key)
        excluded = [e for e in evs if not e.eligible]

        options: list[OptionSummary] = []
        if item.compare_options:
            best_by_option: dict[str, Evaluated] = {}
            for e in ranked:
                best_by_option.setdefault(e.offer.option, e)
            if best_by_option:
                floor = min(e.landed.total for e in best_by_option.values())
                for opt, e in sorted(best_by_option.items(), key=lambda kv: kv[1].landed.total):
                    delta = e.landed.total - floor
                    pct = (delta / floor * 100).quantize(Decimal("0.1")) if floor > 0 else None
                    options.append(OptionSummary(option=opt, best=e, delta=delta, delta_pct=pct))
        results.append(ItemResult(item=item, ranked=ranked, excluded=excluded, options=options))
    return results


def orphan_offers(request: Request, offers: list[Offer]) -> list[Offer]:
    ids = {i.id for i in request.items}
    return [o for o in latest_offers(offers) if o.item not in ids]


# --------------------------------------------------------------------------- verify list


def verify_list(results: list[ItemResult], vendors: dict[str, Vendor], top: int = 2) -> list[dict]:
    """Offers the verifier must re-check: top-N per item plus the best offer per option."""
    out, seen = [], set()
    for r in results:
        picks = list(r.ranked[:top]) + [o.best for o in r.options]
        for e in picks:
            if e.offer.id in seen or e.verification == VERIFIED:
                continue
            seen.add(e.offer.id)
            v = vendors.get(e.offer.vendor)
            p = e.landed.purchase
            out.append(
                {
                    "offer_id": e.offer.id,
                    "item": r.item.id,
                    "option": e.offer.option,
                    "vendor": e.offer.vendor,
                    "sku": e.offer.sku,
                    "mpn": e.offer.mpn,
                    "url": e.offer.url,
                    "qty_to_check": p.buy_qty,
                    "requirements": r.item.requirements,
                    "vendor_login": bool(v and v.login),
                    "vendor_cart_ok": bool(v and v.cart_ok),
                    "check_shipping": e.offer.shipping is not None and e.offer.shipping_source == "cart",
                }
            )
    return out


# --------------------------------------------------------------------------- rendering


def _m(d: Decimal | None) -> str:
    return "?" if d is None else f"${d:,.2f}"


def _unit(d: Decimal) -> str:
    """Unit prices keep sub-cent precision (cheap parts) but show at least 2 decimals."""
    s = f"{d:,.4f}"
    while s.endswith("0") and len(s.split(".")[1]) > 2:
        s = s[:-1]
    return f"${s}"


def _stock(ev: Evaluated) -> str:
    o = ev.offer
    s = "?" if o.stock_qty is None else f"{o.stock_qty:,}"
    if o.lead_time_days is not None:
        s += f" / {o.lead_time_days}d"
    return s


def _esc(s: str | None) -> str:
    return (s or "").replace("|", "\\|")


def render_markdown(request: Request, results: list[ItemResult], policy: Policy, now: datetime, orphans: list[Offer]) -> str:
    tax = f"{policy.tax_rate * 100:.3f}".rstrip("0").rstrip(".") + "%" if policy.tax_rate else "not modelled"
    lines = [
        f"# Comparison — {request.title}",
        "",
        f"Generated {now.strftime('%Y-%m-%d %H:%M UTC')} · currency {policy.currency} · tax {tax}"
        f" · offers older than {policy.max_offer_age_hours}h flagged stale",
        "",
    ]
    for r in results:
        it = r.item
        lines += [f"## {it.id} (qty {it.qty})", ""]
        if it.description:
            lines += [it.description, ""]
        w = r.winner
        if w is None:
            lines += ["**No eligible offer.** See exclusions below.", ""]
        else:
            status = "VERIFIED" if w.verification == VERIFIED else "UNVERIFIED — run verification before buying"
            lines += [
                f"**Winner:** {w.offer.vendor} · {_esc(w.offer.option)} · {_esc(w.offer.sku or w.offer.mpn or '')}"
                f" · buy {w.landed.purchase.buy_qty} · landed {_m(w.landed.total)}"
                f"{' (lower bound)' if not w.landed.shipping_known else ''} · **{status}**",
                "",
                f"Source: {w.offer.url}",
                "",
            ]
        if r.ranked:
            lines += [
                "| # | option | vendor | sku | buy | unit | extended | ship | fees | tax | landed | stock/lead | verified | flags |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
            ]
            for n, e in enumerate(r.ranked, 1):
                L = e.landed
                lines.append(
                    f"| {n} | {_esc(e.offer.option)} | {e.offer.vendor} | {_esc(e.offer.sku or e.offer.mpn)} "
                    f"| {L.purchase.buy_qty} | {_unit(L.purchase.unit_price)} | {_m(L.purchase.extended)} "
                    f"| {_m(L.shipping)} | {_m(L.fees)} | {_m(L.tax)} | **{_m(L.total)}** | {_stock(e)} "
                    f"| {e.verification} | {_esc('; '.join(e.flags))} |"
                )
            lines.append("")
        if r.options:
            lines += ["**Options (best landed offer per option)**", "", "| option | vendor | landed | Δ vs cheapest |", "|---|---|---|---|"]
            for o in r.options:
                d = "—" if o.delta == 0 else f"+{_m(o.delta)}" + (f" (+{o.delta_pct}%)" if o.delta_pct is not None else "")
                lines.append(f"| {_esc(o.option)} | {o.best.offer.vendor} | {_m(o.best.landed.total)} | {d} |")
            if any(not o.best.landed.shipping_known for o in r.options):
                lines.append("")
                lines.append("_At least one option has unknown shipping, so its delta is a lower bound._")
            lines.append("")
        if r.excluded:
            lines += ["**Excluded**", ""]
            for e in r.excluded:
                lines.append(f"- `{e.offer.id}` ({e.offer.vendor}, {_esc(e.offer.option)}): {'; '.join(e.reasons)}")
            lines.append("")
    if orphans:
        lines += ["## Offers for unknown items", ""]
        lines += [f"- `{o.id}` references item '{o.item}', which is not in request.yaml" for o in orphans]
        lines.append("")
    return "\n".join(lines)


def results_json(results: list[ItemResult]) -> list[dict]:
    def ev_json(e: Evaluated) -> dict:
        L = e.landed
        return {
            "offer_id": e.offer.id,
            "vendor": e.offer.vendor,
            "option": e.offer.option,
            "url": e.offer.url,
            "eligible": e.eligible,
            "reasons": e.reasons,
            "flags": e.flags,
            "verification": e.verification,
            "buy_qty": L.purchase.buy_qty if L else None,
            "unit_price": money_json(L.purchase.unit_price) if L else None,
            "extended": money_json(L.purchase.extended) if L else None,
            "shipping": money_json(L.shipping) if L else None,
            "fees": money_json(L.fees) if L else None,
            "tax": money_json(L.tax) if L else None,
            "landed": money_json(L.total) if L else None,
            "landed_is_lower_bound": bool(L and not L.shipping_known),
        }

    return [
        {
            "item": r.item.id,
            "qty": r.item.qty,
            "winner": ev_json(r.winner) if r.winner else None,
            "ranked": [ev_json(e) for e in r.ranked],
            "excluded": [ev_json(e) for e in r.excluded],
            "options": [
                {
                    "option": o.option,
                    "offer_id": o.best.offer.id,
                    "landed": money_json(o.best.landed.total),
                    "delta_vs_cheapest": money_json(o.delta),
                    "delta_pct": money_json(o.delta_pct),
                }
                for o in r.options
            ],
        }
        for r in results
    ]


def request_paths(request_dir: Path) -> dict[str, Path]:
    return {
        "request": request_dir / "request.yaml",
        "offers": request_dir / "offers.jsonl",
        "verifications": request_dir / "verifications.jsonl",
        "comparison": request_dir / "comparison.md",
    }
