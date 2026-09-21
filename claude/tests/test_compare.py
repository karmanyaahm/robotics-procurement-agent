from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from procure import compare as cmp
from procure.config import Policy, Vendor, load_policy, load_vendors
from procure.models import Item, Offer, Request, Verification, load_offers, load_request, load_verifications

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "toolbox"
NOW = datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc)
T = "2026-09-21T15:00:00Z"


def load_example():
    return (
        load_request(EXAMPLE / "request.yaml"),
        load_offers(EXAMPLE / "offers.jsonl"),
        load_verifications(EXAMPLE / "verifications.jsonl"),
        load_vendors(EXAMPLE / "config"),
        load_policy(EXAMPLE / "config"),
    )


def by_item(results, item_id):
    return next(r for r in results if r.item.id == item_id)


def test_example_toolbox_ranking_and_option_delta():
    req, offers, vers, vendors, pol = load_example()
    r = by_item(cmp.compare(req, offers, vendors, pol, vers, NOW), "toolbox")
    assert [e.offer.id for e in r.ranked] == ["vendor-a:TC-2D", "vendor-a:TC-3D", "vendor-b:RC3"]
    assert r.winner.landed.total == Decimal("204.12")
    assert r.winner.verification == cmp.VERIFIED
    assert [(o.option, o.delta) for o in r.options] == [("2-drawer", Decimal("0")), ("3-drawer", Decimal("54.00"))]
    reasons = {e.offer.id: " ".join(e.reasons) for e in r.excluded}
    assert "fails requirement 'width'" in reasons["vendor-b:RC2"] and "28.5 in" in reasons["vendor-b:RC2"]
    assert "not approved" in reasons["vendor-c:X3"]


def test_example_standoffs_buy_up_and_vendor_shipping_rule():
    req, offers, vers, vendors, pol = load_example()
    r = by_item(cmp.compare(req, offers, vendors, pol, vers, NOW), "standoffs")
    w = r.winner
    assert w.offer.id == "vendor-a:SO-M3-10"
    assert (w.landed.purchase.buy_qty, w.landed.shipping, w.landed.total) == (100, Decimal("6.95"), Decimal("19.91"))
    assert any("instead of 90" in f for f in w.flags)
    assert any("vendor rule" in f for f in w.flags)
    assert r.ranked[1].landed.purchase.buy_qty == 90  # MOQ 10 / multiple 10 already satisfied


def _setup(offer_patch=None, item_patch=None, need_by=None):
    base = {
        "id": "v:1", "item": "x", "option": "o", "vendor": "v", "url": "https://example.com/1",
        "currency": "USD", "price_breaks": [{"qty": 1, "unit_price": 10}], "stock_qty": 100,
        "shipping": 5, "shipping_source": "cart", "spec_checks": {"r": {"pass": True, "value": "ok"}},
        "source": "t", "captured_at": T,
    }
    base.update(offer_patch or {})
    item = Item(id="x", qty=1, requirements={"r": "must be ok"})
    if item_patch:
        item = replace(item, **item_patch)
    req = Request(title="t", items=[item], need_by=need_by)
    vendors = {"v": Vendor(key="v", name="V", approved=True)}
    return req, [Offer.from_dict(base)], vendors, Policy(max_lead_time_days=14)


def ver(**kw):
    d = dict(offer_id="v:1", checked_at=datetime(2026, 9, 21, 15, 30, tzinfo=timezone.utc), qty_checked=1,
             observed_unit_price=Decimal("10"), specs_confirmed=True)
    d.update(kw)
    return Verification(**d)


def only(results):
    r = results[0]
    return (r.ranked + r.excluded)[0]


def test_verified_when_observations_match():
    req, offers, vendors, pol = _setup()
    assert only(cmp.compare(req, offers, vendors, pol, [ver()], NOW)).verification == cmp.VERIFIED


def test_price_mismatch_excludes_offer():
    req, offers, vendors, pol = _setup()
    e = only(cmp.compare(req, offers, vendors, pol, [ver(observed_unit_price=Decimal("12.00"))], NOW))
    assert not e.eligible and e.verification == cmp.MISMATCH and "recapture" in e.reasons[-1]


def test_unconfirmed_specs_is_a_mismatch():
    req, offers, vendors, pol = _setup()
    e = only(cmp.compare(req, offers, vendors, pol, [ver(specs_confirmed=False)], NOW))
    assert e.verification == cmp.MISMATCH


def test_verification_older_than_capture_is_ignored():
    req, offers, vendors, pol = _setup()
    old = ver(checked_at=datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc))
    assert only(cmp.compare(req, offers, vendors, pol, [old], NOW)).verification == cmp.UNVERIFIED


def test_latest_capture_wins():
    req, offers, vendors, pol = _setup()
    newer = Offer.from_dict({**offers[0].to_dict(), "captured_at": "2026-09-21T15:45:00Z",
                             "price_breaks": [{"qty": 1, "unit_price": 8}]})
    r = cmp.compare(req, offers + [newer], vendors, pol, [ver()], NOW)[0]
    assert len(r.ranked) == 1 and r.ranked[0].landed.purchase.unit_price == Decimal("8")
    # the verification predates the re-capture, so it no longer counts
    assert r.ranked[0].verification == cmp.UNVERIFIED


@pytest.mark.parametrize("checks, fragment", [({}, "not checked"), ({"r": {"pass": None}}, "not checked"),
                                              ({"r": {"pass": False, "value": "nope"}}, "fails requirement")])
def test_spec_checks_are_strict(checks, fragment):
    req, offers, vendors, pol = _setup({"spec_checks": checks})
    e = only(cmp.compare(req, offers, vendors, pol, [], NOW))
    assert not e.eligible and fragment in " ".join(e.reasons)


def test_backorder_within_lead_limit_is_flagged_not_excluded():
    req, offers, vendors, pol = _setup({"stock_qty": 0, "lead_time_days": 10})
    e = only(cmp.compare(req, offers, vendors, pol, [], NOW))
    assert e.eligible and any("backorder" in f for f in e.flags)


def test_backorder_beyond_lead_limit_or_unknown_is_excluded():
    for patch in ({"stock_qty": 0, "lead_time_days": 30}, {"stock_qty": 0}):
        req, offers, vendors, pol = _setup(patch)
        assert not only(cmp.compare(req, offers, vendors, pol, [], NOW)).eligible


def test_need_by_tightens_lead_limit():
    req, offers, vendors, pol = _setup({"stock_qty": 0, "lead_time_days": 10}, need_by=date(2026, 9, 26))
    assert not only(cmp.compare(req, offers, vendors, pol, [], NOW)).eligible


def test_stale_and_unknown_shipping_flags():
    req, offers, vendors, pol = _setup({"shipping": None, "shipping_source": None})
    later = NOW + timedelta(hours=pol.max_offer_age_hours + 5)
    e = only(cmp.compare(req, offers, vendors, pol, [], later))
    assert any("stale" in f for f in e.flags) and any("lower bound" in f for f in e.flags)


def test_verify_list_skips_verified_and_hides_captured_prices():
    req, offers, vers, vendors, pol = load_example()
    todo = cmp.verify_list(cmp.compare(req, offers, vendors, pol, vers, NOW), vendors)
    ids = [t["offer_id"] for t in todo]
    assert "vendor-a:TC-2D" not in ids and "vendor-a:TC-3D" in ids
    assert all("unit_price" not in k for t in todo for k in t)  # verifier reads prices blind


def test_markdown_renders():
    req, offers, vers, vendors, pol = load_example()
    res = cmp.compare(req, offers, vendors, pol, vers, NOW)
    md = cmp.render_markdown(req, res, pol, NOW, [])
    assert "**Winner:** vendor-a · 2-drawer" in md and "+$54.00 (+26.5%)" in md


def test_conflicting_same_timestamp_captures_are_rejected(tmp_path):
    import json

    from procure.models import ValidationError

    rec = {"id": "v:1", "item": "x", "option": "o", "vendor": "v", "url": "https://example.com",
           "currency": "USD", "price_breaks": [{"qty": 1, "unit_price": 10}], "source": "t", "captured_at": T}
    f = tmp_path / "offers.jsonl"
    f.write_text(json.dumps(rec) + "\n" + json.dumps(rec) + "\n")
    assert len(load_offers(f)) == 1  # identical duplicates collapse
    rec2 = {**rec, "price_breaks": [{"qty": 1, "unit_price": 11}]}
    f.write_text(json.dumps(rec) + "\n" + json.dumps(rec2) + "\n")
    with pytest.raises(ValidationError, match="conflicting"):
        load_offers(f)
