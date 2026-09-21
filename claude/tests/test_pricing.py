from decimal import Decimal

import pytest

from procure.config import Policy, Vendor
from procure.models import Offer, ValidationError
from procure.pricing import best_purchase, landed_cost, unit_price_at

T = "2026-09-21T15:00:00Z"


def offer(breaks, **kw):
    d = {
        "id": "v:1", "item": "x", "option": "o", "vendor": "v", "url": "https://example.com",
        "currency": "USD", "price_breaks": [{"qty": q, "unit_price": p} for q, p in breaks],
        "source": "test", "captured_at": T,
    }
    d.update(kw)
    return Offer.from_dict(d)


def vendor(**kw):
    return Vendor(key="v", name="V", approved=True, **kw)


def test_break_selection():
    o = offer([(1, 1.03), (10, 0.79), (100, 0.56)])
    assert unit_price_at(o.price_breaks, 9) == Decimal("1.03")
    assert unit_price_at(o.price_breaks, 10) == Decimal("0.79")
    assert unit_price_at(o.price_breaks, 5000) == Decimal("0.56")


def test_breaks_are_sorted_on_load():
    o = offer([(100, 0.56), (1, 1.03)])
    assert [b.qty for b in o.price_breaks] == [1, 100]


def test_moq_and_multiple_force_quantity():
    o = offer([(10, 0.18)], moq=10, order_multiple=10)
    p = best_purchase(91, o)
    assert p.buy_qty == 100 and p.moq_forced and not p.buy_up
    assert p.extended == Decimal("18.00")


def test_below_lowest_break_rounds_up():
    p = best_purchase(3, offer([(5, 2.00)]))
    assert p.buy_qty == 5 and p.moq_forced


def test_buy_up_when_next_break_is_cheaper_overall():
    o = offer([(1, 0.25), (100, 0.12)])
    p = best_purchase(90, o)
    assert (p.buy_qty, p.extended, p.buy_up, p.base_qty) == (100, Decimal("12.00"), True, 90)
    assert p.base_extended == Decimal("22.50")


def test_no_buy_up_when_disabled_or_not_cheaper():
    o = offer([(1, 0.25), (100, 0.12)])
    assert best_purchase(90, o, allow_buy_up=False).buy_qty == 90
    o2 = offer([(1, 0.20), (100, 0.19)])
    assert best_purchase(90, o2).buy_qty == 90  # 90*0.20=18.00 < 100*0.19=19.00


def test_landed_with_vendor_shipping_rule_and_tax():
    o = offer([(1, 40.00)])
    pol = Policy(tax_rate=Decimal("0.0825"))
    v = vendor(shipping_flat=Decimal("6.95"), shipping_free_over=Decimal("50"))
    L = landed_cost(1, o, v, pol)
    assert L.shipping == Decimal("6.95") and L.tax == Decimal("3.30") and L.total == Decimal("50.25")
    L2 = landed_cost(2, o, v, pol)  # $80 extended -> free shipping
    assert L2.shipping == Decimal("0")


def test_captured_shipping_beats_vendor_rule():
    o = offer([(1, 10)], shipping=12.5, shipping_source="cart")
    L = landed_cost(1, o, vendor(shipping_flat=Decimal("1")), Policy())
    assert L.shipping == Decimal("12.5") and L.shipping_source == "cart"


def test_unknown_shipping_is_lower_bound():
    L = landed_cost(1, offer([(1, 10)]), vendor(), Policy())
    assert L.shipping is None and not L.shipping_known and L.total == Decimal("10.00")


def test_tax_exempt_vendor_and_fees():
    o = offer([(1, 100)], fees=7)
    L = landed_cost(1, o, vendor(charges_tax=False), Policy(tax_rate=Decimal("0.1")))
    assert L.tax == Decimal("0.00") and L.total == Decimal("107.00")


def test_tax_on_shipping_when_policy_says_so():
    o = offer([(1, 100)], shipping=10)
    L = landed_cost(1, o, vendor(), Policy(tax_rate=Decimal("0.1"), tax_shipping=True))
    assert L.tax == Decimal("11.00")


@pytest.mark.parametrize(
    "patch, msg",
    [
        ({"price_breaks": []}, "price_breaks"),
        ({"price_breaks": [{"qty": 0, "unit_price": 1}]}, ">= 1"),
        ({"price_breaks": [{"qty": 1, "unit_price": 1}, {"qty": 1, "unit_price": 2}]}, "duplicate"),
        ({"currency": "dollars"}, "currency"),
        ({"url": "example.com"}, "url"),
        ({"surprise": 1}, "unknown field"),
        ({"spec_checks": {"w": {"value": "x"}}}, "pass"),
        ({"captured_at": "yesterday"}, "timestamp"),
    ],
)
def test_offer_validation(patch, msg):
    d = {
        "id": "v:1", "item": "x", "option": "o", "vendor": "v", "url": "https://example.com",
        "currency": "USD", "price_breaks": [{"qty": 1, "unit_price": 1}], "source": "t", "captured_at": T,
    }
    d.update(patch)
    with pytest.raises(ValidationError, match=msg):
        Offer.from_dict(d)
