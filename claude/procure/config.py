"""Load config/vendors.yaml and config/policy.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

from .models import ValidationError, opt_decimal, opt_int, to_decimal, to_int

DEFAULT_CONFIG_DIR = Path("config")


@dataclass
class Vendor:
    key: str
    name: str
    approved: bool
    access: str = "browser"  # api | fetch | browser
    domains: list[str] = field(default_factory=list)
    login: bool = False  # prices depend on the logged-in account
    charges_tax: bool = True
    cart_ok: bool = False  # may the agent add to cart to read shipping (never check out)
    shipping_flat: Decimal | None = None
    shipping_free_over: Decimal | None = None
    notes: str = ""


@dataclass
class Policy:
    currency: str = "USD"
    tax_rate: Decimal = Decimal("0")
    tax_shipping: bool = False
    max_offer_age_hours: int = 72
    max_lead_time_days: int = 21
    verify_tolerance: Decimal = Decimal("0.01")
    allow_buy_up: bool = True


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise ValidationError(f"missing config file: {path}")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ValidationError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ValidationError(f"{path}: expected a mapping")
    return data


def load_vendors(config_dir: Path = DEFAULT_CONFIG_DIR) -> dict[str, Vendor]:
    path = config_dir / "vendors.yaml"
    data = _load_yaml(path)
    raw = data.get("vendors") or {}
    if not isinstance(raw, dict):
        raise ValidationError(f"{path}: 'vendors' must be a mapping keyed by vendor id")
    out: dict[str, Vendor] = {}
    for key, v in raw.items():
        w = f"{path}: vendors.{key}"
        if not isinstance(v, dict):
            raise ValidationError(f"{w}: expected a mapping")
        approved = v.get("approved")
        if not isinstance(approved, bool):
            raise ValidationError(f"{w}: 'approved' must be true or false")
        ship = v.get("shipping")
        flat = free_over = None
        if ship is not None:
            if not isinstance(ship, dict):
                raise ValidationError(f"{w}: 'shipping' must be null or {{flat: X, free_over: Y}}")
            flat = opt_decimal(ship.get("flat"), f"{w}: shipping.flat")
            free_over = opt_decimal(ship.get("free_over"), f"{w}: shipping.free_over")
        access = str(v.get("access") or "browser")
        if access not in {"api", "fetch", "browser"}:
            raise ValidationError(f"{w}: 'access' must be api, fetch or browser")
        out[str(key).lower()] = Vendor(
            key=str(key).lower(),
            name=str(v.get("name") or key),
            approved=approved,
            access=access,
            domains=[str(d) for d in (v.get("domains") or [])],
            login=bool(v.get("login", False)),
            charges_tax=bool(v.get("charges_tax", True)),
            cart_ok=bool(v.get("cart_ok", False)),
            shipping_flat=flat,
            shipping_free_over=free_over,
            notes=str(v.get("notes") or ""),
        )
    return out


def load_policy(config_dir: Path = DEFAULT_CONFIG_DIR) -> Policy:
    path = config_dir / "policy.yaml"
    data = _load_yaml(path)
    p = Policy()
    if "currency" in data:
        p.currency = str(data["currency"]).upper()
    if data.get("tax_rate") is not None:
        p.tax_rate = to_decimal(data["tax_rate"], f"{path}: tax_rate")
        if not (Decimal("0") <= p.tax_rate < Decimal("1")):
            raise ValidationError(f"{path}: tax_rate is a fraction, e.g. 0.0825 for 8.25%")
    if "tax_shipping" in data:
        p.tax_shipping = bool(data["tax_shipping"])
    if data.get("max_offer_age_hours") is not None:
        p.max_offer_age_hours = to_int(data["max_offer_age_hours"], f"{path}: max_offer_age_hours", 1)
    lead = opt_int(data.get("max_lead_time_days"), f"{path}: max_lead_time_days", 0)
    if lead is not None:
        p.max_lead_time_days = lead
    if data.get("verify_tolerance") is not None:
        p.verify_tolerance = to_decimal(data["verify_tolerance"], f"{path}: verify_tolerance")
    if "allow_buy_up" in data:
        p.allow_buy_up = bool(data["allow_buy_up"])
    return p
