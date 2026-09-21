"""DigiKey Product Information API v4.

Auth: 2-legged OAuth (client_credentials). Register a *production* app at
https://developer.digikey.com and put DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET
in .env. Sandbox apps return dummy data.

Limitations worth knowing:
- Returns DigiKey *standard* pricing. Account-specific pricing ("MyPricing")
  needs 3-legged OAuth and is not populated by KeywordSearch; if you have
  negotiated pricing, capture it from the logged-in site instead.
- No shipping cost. Offers come back with shipping = null unless
  config/vendors.yaml has a shipping rule for digikey.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

from ..models import Offer, iso
from . import SourceError, parse_money, require_env

VENDOR = "digikey"
TIMEOUT = 20


def _base() -> str:
    return os.environ.get("DIGIKEY_API_BASE", "https://api.digikey.com").rstrip("/")


def _cache_path() -> Path:
    return Path(os.environ.get("PROCURE_CACHE_DIR", ".cache")) / "digikey_token.json"


def get_token() -> str:
    cache = _cache_path()
    if cache.exists():
        try:
            data = json.loads(cache.read_text())
            if data.get("expires_at", 0) > time.time() + 30:
                return data["access_token"]
        except (json.JSONDecodeError, KeyError):
            pass
    client_id = require_env("DIGIKEY_CLIENT_ID", "register a production app at developer.digikey.com")
    secret = require_env("DIGIKEY_CLIENT_SECRET", "register a production app at developer.digikey.com")
    try:
        r = requests.post(
            f"{_base()}/v1/oauth2/token",
            data={"client_id": client_id, "client_secret": secret, "grant_type": "client_credentials"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise SourceError(f"DigiKey token request failed: {type(e).__name__}") from None
    if r.status_code != 200:
        raise SourceError(f"DigiKey token request failed: HTTP {r.status_code} {r.text[:300]}")
    tok = r.json()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"access_token": tok["access_token"], "expires_at": time.time() + int(tok.get("expires_in", 600))})
    )
    try:
        cache.chmod(0o600)
    except OSError:
        pass
    return tok["access_token"]


def _headers() -> dict:
    token = get_token()
    return {
        "X-DIGIKEY-Client-Id": require_env("DIGIKEY_CLIENT_ID", "see README"),
        "Authorization": f"Bearer {token}",
        "X-DIGIKEY-Locale-Site": os.environ.get("DIGIKEY_LOCALE_SITE", "US"),
        "X-DIGIKEY-Locale-Language": os.environ.get("DIGIKEY_LOCALE_LANGUAGE", "en"),
        "X-DIGIKEY-Locale-Currency": os.environ.get("DIGIKEY_LOCALE_CURRENCY", "USD"),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def keyword_search(keywords: str, limit: int = 10) -> dict:
    try:
        r = requests.post(
            f"{_base()}/products/v4/search/keyword",
            headers=_headers(),
            json={"Keywords": keywords, "Limit": max(1, min(limit, 50)), "Offset": 0},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise SourceError(f"DigiKey keyword search failed: {type(e).__name__}") from None
    if r.status_code != 200:
        raise SourceError(f"DigiKey keyword search failed: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


def exact_matches(data: dict, mpn: str) -> list[dict]:
    """Products whose manufacturer part number equals `mpn` (case-insensitive)."""
    target = mpn.strip().lower()
    pool = (data.get("ExactMatches") or []) + (data.get("Products") or [])
    out, seen = [], set()
    for p in pool:
        m = str(p.get("ManufacturerProductNumber") or "").lower()
        key = (m, str((p.get("Manufacturer") or {}).get("Name") or "").lower())
        if m == target and key not in seen:
            seen.add(key)
            out.append(p)
    return out


def product_to_offers(product: dict, item: str, option: str | None, captured_at: datetime) -> list[Offer]:
    """One offer per package variation (cut tape, tape & reel, Digi-Reel, bulk...)."""
    mpn = product.get("ManufacturerProductNumber")
    title = (product.get("Description") or {}).get("ProductDescription")
    manufacturer = (product.get("Manufacturer") or {}).get("Name")
    attrs = {
        str(p.get("ParameterText")): str(p.get("ValueText"))
        for p in (product.get("Parameters") or [])
        if p.get("ParameterText") and p.get("ValueText") not in (None, "", "-")
    }
    if manufacturer:
        attrs["Manufacturer"] = manufacturer
    status = (product.get("ProductStatus") or {}).get("Status")
    if status:
        attrs["Product Status"] = status
    if product.get("DatasheetUrl"):
        attrs["Datasheet"] = product["DatasheetUrl"]

    lead_weeks = product.get("ManufacturerLeadWeeks")
    lead_days = int(lead_weeks) * 7 if str(lead_weeks or "").strip().isdigit() else None

    offers = []
    for var in product.get("ProductVariations") or []:
        pricing = var.get("StandardPricing") or []
        breaks = [
            {"qty": int(b["BreakQuantity"]), "unit_price": float(parse_money(b["UnitPrice"]))}
            for b in pricing
            if b.get("BreakQuantity") and b.get("UnitPrice") is not None
        ]
        if not breaks:
            continue
        dkpn = var.get("DigiKeyProductNumber")
        pkg = (var.get("PackageType") or {}).get("Name")
        min_break = min(b["qty"] for b in breaks)
        notes = []
        if var.get("MarketPlace"):
            notes.append("DigiKey Marketplace (third-party seller)")
        if var.get("TariffActive"):
            notes.append("tariff flag active")
        url = product.get("ProductUrl") or f"https://www.digikey.com/en/products/result?keywords={quote(str(dkpn))}"
        offers.append(
            Offer.from_dict(
                {
                    "id": f"{VENDOR}:{dkpn}",
                    "item": item,
                    "option": option or mpn or dkpn,
                    "vendor": VENDOR,
                    "sku": dkpn,
                    "mpn": mpn,
                    "title": f"{title} [{pkg}]" if pkg else title,
                    "url": url,
                    "currency": os.environ.get("DIGIKEY_LOCALE_CURRENCY", "USD"),
                    "price_breaks": breaks,
                    "moq": int(var.get("MinimumOrderQuantity") or 0) or min_break,
                    "order_multiple": 1,
                    "stock_qty": int(var.get("QuantityAvailableforPackageType") or 0),
                    "lead_time_days": lead_days,
                    "shipping": None,
                    "fees": float(var.get("DigiReelFee") or 0),
                    "spec_checks": {},
                    "attributes": {**attrs, **({"Package": pkg} if pkg else {})},
                    "source": "api:digikey",
                    "captured_at": iso(captured_at),
                    "notes": "; ".join(notes) or None,
                },
                where=f"digikey {dkpn}",
            )
        )
    return offers
