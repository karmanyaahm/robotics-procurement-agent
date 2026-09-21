"""Mouser Search API.

Free key via https://www.mouser.com/api-search/ (published limits at time of
writing: 50 results/call, 30 calls/min, 1,000 calls/day). Put it in .env as
MOUSER_API_KEY.

Same limitations as DigiKey: list pricing only, no shipping.
The API key travels as a query parameter, so errors are rewritten to never
include the request URL.
"""

from __future__ import annotations

import os
from datetime import datetime
from urllib.parse import quote

import requests

from ..models import Offer, iso
from . import SourceError, parse_lead_time_days, parse_leading_int, parse_money, require_env

VENDOR = "mouser"
TIMEOUT = 20


def _base() -> str:
    return os.environ.get("MOUSER_API_BASE", "https://api.mouser.com/api/v1.0").rstrip("/")


def _post(path: str, body: dict) -> dict:
    key = require_env("MOUSER_API_KEY", "get a Search API key at mouser.com/api-search")
    try:
        r = requests.post(
            f"{_base()}{path}",
            params={"apiKey": key},
            json=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise SourceError(f"Mouser request failed: {type(e).__name__}") from None
    try:
        data = r.json()
    except ValueError:
        raise SourceError(f"Mouser returned HTTP {r.status_code} with a non-JSON body") from None
    errors = data.get("Errors") or []
    if r.status_code != 200 or errors:
        msgs = "; ".join(str(e.get("Message") or e.get("Code") or e) for e in errors) or r.text[:200]
        raise SourceError(f"Mouser API error (HTTP {r.status_code}): {msgs}".replace(key, "***"))
    return data


def partnumber_search(part_number: str, exact: bool = True) -> dict:
    return _post(
        "/search/partnumber",
        {"SearchByPartRequest": {"mouserPartNumber": part_number, "partSearchOptions": "Exact" if exact else "None"}},
    )


def keyword_search(keyword: str, records: int = 10, in_stock_only: bool = False) -> dict:
    return _post(
        "/search/keyword",
        {
            "SearchByKeywordRequest": {
                "keyword": keyword,
                "records": max(1, min(records, 50)),
                "startingRecord": 0,
                "searchOptions": "InStock" if in_stock_only else "None",
                "searchWithYourSignUpLanguage": "",
            }
        },
    )


def parts(data: dict) -> list[dict]:
    return list((data.get("SearchResults") or {}).get("Parts") or [])


def part_to_offer(part: dict, item: str, option: str | None, captured_at: datetime) -> Offer | None:
    breaks = []
    currency = None
    for pb in part.get("PriceBreaks") or []:
        price = parse_money(pb.get("Price"))
        qty = parse_leading_int(pb.get("Quantity"))
        if price and qty:
            breaks.append({"qty": qty, "unit_price": float(price)})
            currency = currency or pb.get("Currency")
    if not breaks:
        return None
    mpn = part.get("ManufacturerPartNumber")
    mouser_pn = part.get("MouserPartNumber")

    stock = parse_leading_int(part.get("AvailabilityInStock"))
    if stock is None:
        stock = parse_leading_int(part.get("Availability"))

    attrs: dict[str, str] = {}
    for a in part.get("ProductAttributes") or []:
        name, value = a.get("AttributeName"), a.get("AttributeValue")
        if name and value:
            attrs[name] = f"{attrs[name]}, {value}" if name in attrs else str(value)
    for k, label in (("Manufacturer", "Manufacturer"), ("LifecycleStatus", "Lifecycle"), ("ROHSStatus", "RoHS"),
                     ("DataSheetUrl", "Datasheet"), ("Availability", "Availability text")):
        if part.get(k):
            attrs[label] = str(part[k])

    url = part.get("ProductDetailUrl") or f"https://www.mouser.com/c/?q={quote(str(mouser_pn))}"
    return Offer.from_dict(
        {
            "id": f"{VENDOR}:{mouser_pn}",
            "item": item,
            "option": option or mpn or mouser_pn,
            "vendor": VENDOR,
            "sku": mouser_pn,
            "mpn": mpn,
            "title": part.get("Description"),
            "url": url,
            "currency": (currency or "USD").upper(),
            "price_breaks": breaks,
            "moq": parse_leading_int(part.get("Min")) or min(b["qty"] for b in breaks),
            "order_multiple": parse_leading_int(part.get("Mult")) or 1,
            "stock_qty": stock,
            "lead_time_days": parse_lead_time_days(part.get("LeadTime")),
            "shipping": None,
            "fees": 0,
            "spec_checks": {},
            "attributes": attrs,
            "source": "api:mouser",
            "captured_at": iso(captured_at),
        },
        where=f"mouser {mouser_pn}",
    )
