"""Data model for requests, offers and verifications.

Everything the agent captures goes through these parsers, so malformed or
incomplete records fail loudly instead of silently skewing a comparison.
Money is Decimal end to end.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml


class ValidationError(ValueError):
    """Raised when a record does not match the schema."""


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


# --------------------------------------------------------------------------- helpers


def to_decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValidationError(f"{name}: expected a number, got {value!r}")
    try:
        d = Decimal(str(value))
    except InvalidOperation as e:
        raise ValidationError(f"{name}: not a number: {value!r}") from e
    if not d.is_finite():
        raise ValidationError(f"{name}: not finite: {value!r}")
    return d


def opt_decimal(value: Any, name: str) -> Decimal | None:
    return None if value is None else to_decimal(value, name)


def to_int(value: Any, name: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or value is None:
        raise ValidationError(f"{name}: expected an integer, got {value!r}")
    if isinstance(value, float) and not value.is_integer():
        raise ValidationError(f"{name}: expected an integer, got {value!r}")
    try:
        i = int(value)
    except (TypeError, ValueError) as e:
        raise ValidationError(f"{name}: expected an integer, got {value!r}") from e
    if minimum is not None and i < minimum:
        raise ValidationError(f"{name}: must be >= {minimum}, got {i}")
    return i


def opt_int(value: Any, name: str, minimum: int | None = None) -> int | None:
    return None if value is None else to_int(value, name, minimum)


def parse_dt(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{name}: expected ISO-8601 timestamp string, got {value!r}")
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise ValidationError(f"{name}: bad timestamp {value!r}") from e
    if dt.tzinfo is None:  # treat naive timestamps as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def req_str(d: dict, key: str, where: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValidationError(f"{where}: '{key}' is required (non-empty string)")
    return v.strip()


def opt_str(d: dict, key: str, where: str) -> str | None:
    v = d.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValidationError(f"{where}: '{key}' must be a string")
    return v


def money_json(d: Decimal | None) -> float | None:
    return None if d is None else float(d)


def _default(value: Any, fallback: Any) -> Any:
    return fallback if value is None else value


# --------------------------------------------------------------------------- offers


@dataclass(frozen=True)
class PriceBreak:
    qty: int
    unit_price: Decimal


@dataclass
class SpecCheck:
    passed: bool | None  # JSON key is "pass"; None = not determined
    value: str | None = None
    evidence: str | None = None

    @classmethod
    def from_dict(cls, d: Any, where: str) -> "SpecCheck":
        if not isinstance(d, dict):
            raise ValidationError(f"{where}: spec check must be an object with 'pass'")
        if "pass" not in d:
            raise ValidationError(f"{where}: spec check is missing 'pass' (true/false/null)")
        p = d["pass"]
        if p is not None and not isinstance(p, bool):
            raise ValidationError(f"{where}: 'pass' must be true, false or null")
        value = d.get("value")
        return cls(
            passed=p,
            value=None if value is None else str(value),
            evidence=None if d.get("evidence") is None else str(d.get("evidence")),
        )

    def to_dict(self) -> dict:
        return {"pass": self.passed, "value": self.value, "evidence": self.evidence}


OFFER_FIELDS = {
    "id", "item", "option", "vendor", "url", "currency", "price_breaks", "captured_at",
    "source", "sku", "mpn", "title", "moq", "order_multiple", "stock_qty",
    "lead_time_days", "shipping", "shipping_source", "fees", "spec_checks",
    "attributes", "evidence", "notes",
}


@dataclass
class Offer:
    id: str
    item: str
    option: str
    vendor: str
    url: str
    currency: str
    price_breaks: list[PriceBreak]
    captured_at: datetime
    source: str
    sku: str | None = None
    mpn: str | None = None
    title: str | None = None
    moq: int = 1
    order_multiple: int = 1
    stock_qty: int | None = None
    lead_time_days: int | None = None
    shipping: Decimal | None = None
    shipping_source: str | None = None
    fees: Decimal = Decimal("0")
    spec_checks: dict[str, SpecCheck] = field(default_factory=dict)
    attributes: dict[str, str] = field(default_factory=dict)
    evidence: str | None = None
    notes: str | None = None

    @classmethod
    def from_dict(cls, d: Any, where: str = "offer") -> "Offer":
        if not isinstance(d, dict):
            raise ValidationError(f"{where}: expected a JSON object")
        unknown = set(d) - OFFER_FIELDS
        if unknown:
            raise ValidationError(f"{where}: unknown field(s) {sorted(unknown)}")
        oid = req_str(d, "id", where)
        where = f"{where} [{oid}]"

        raw_breaks = d.get("price_breaks")
        if not isinstance(raw_breaks, list) or not raw_breaks:
            raise ValidationError(f"{where}: 'price_breaks' must be a non-empty list")
        breaks: list[PriceBreak] = []
        for i, b in enumerate(raw_breaks):
            if not isinstance(b, dict):
                raise ValidationError(f"{where}: price_breaks[{i}] must be an object")
            q = to_int(b.get("qty"), f"{where}: price_breaks[{i}].qty", minimum=1)
            p = to_decimal(b.get("unit_price"), f"{where}: price_breaks[{i}].unit_price")
            if p < 0:
                raise ValidationError(f"{where}: price_breaks[{i}].unit_price is negative")
            breaks.append(PriceBreak(q, p))
        breaks.sort(key=lambda b: b.qty)
        if len({b.qty for b in breaks}) != len(breaks):
            raise ValidationError(f"{where}: duplicate price-break quantities")

        raw_checks = d.get("spec_checks") or {}
        if not isinstance(raw_checks, dict):
            raise ValidationError(f"{where}: 'spec_checks' must be an object keyed by requirement id")
        checks = {k: SpecCheck.from_dict(v, f"{where}: spec_checks.{k}") for k, v in raw_checks.items()}

        attrs = d.get("attributes") or {}
        if not isinstance(attrs, dict):
            raise ValidationError(f"{where}: 'attributes' must be an object")

        currency = req_str(d, "currency", where).upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValidationError(f"{where}: currency must be a 3-letter ISO code")

        url = req_str(d, "url", where)
        if not url.startswith(("http://", "https://")):
            raise ValidationError(f"{where}: url must be http(s)")

        fees = opt_decimal(d.get("fees"), f"{where}: fees")
        shipping = opt_decimal(d.get("shipping"), f"{where}: shipping")
        if shipping is not None and shipping < 0:
            raise ValidationError(f"{where}: shipping is negative")

        return cls(
            id=oid,
            item=req_str(d, "item", where),
            option=req_str(d, "option", where),
            vendor=req_str(d, "vendor", where).lower(),
            url=url,
            currency=currency,
            price_breaks=breaks,
            captured_at=parse_dt(d.get("captured_at"), f"{where}: captured_at"),
            source=req_str(d, "source", where),
            sku=opt_str(d, "sku", where),
            mpn=opt_str(d, "mpn", where),
            title=opt_str(d, "title", where),
            moq=to_int(_default(d.get("moq"), 1), f"{where}: moq", 1),
            order_multiple=to_int(_default(d.get("order_multiple"), 1), f"{where}: order_multiple", 1),
            stock_qty=opt_int(d.get("stock_qty"), f"{where}: stock_qty", 0),
            lead_time_days=opt_int(d.get("lead_time_days"), f"{where}: lead_time_days", 0),
            shipping=shipping,
            shipping_source=opt_str(d, "shipping_source", where),
            fees=fees if fees is not None else Decimal("0"),
            spec_checks=checks,
            attributes={str(k): str(v) for k, v in attrs.items()},
            evidence=opt_str(d, "evidence", where),
            notes=opt_str(d, "notes", where),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "item": self.item,
            "option": self.option,
            "vendor": self.vendor,
            "sku": self.sku,
            "mpn": self.mpn,
            "title": self.title,
            "url": self.url,
            "currency": self.currency,
            "price_breaks": [{"qty": b.qty, "unit_price": float(b.unit_price)} for b in self.price_breaks],
            "moq": self.moq,
            "order_multiple": self.order_multiple,
            "stock_qty": self.stock_qty,
            "lead_time_days": self.lead_time_days,
            "shipping": money_json(self.shipping),
            "shipping_source": self.shipping_source,
            "fees": float(self.fees),
            "spec_checks": {k: v.to_dict() for k, v in self.spec_checks.items()},
            "attributes": self.attributes,
            "source": self.source,
            "captured_at": iso(self.captured_at),
            "evidence": self.evidence,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- verifications


VERIFICATION_FIELDS = {
    "offer_id", "checked_at", "qty_checked", "observed_unit_price", "specs_confirmed",
    "observed_shipping", "in_stock", "evidence", "notes",
}


@dataclass
class Verification:
    offer_id: str
    checked_at: datetime
    qty_checked: int
    observed_unit_price: Decimal
    specs_confirmed: bool
    observed_shipping: Decimal | None = None
    in_stock: bool | None = None
    evidence: str | None = None
    notes: str | None = None

    @classmethod
    def from_dict(cls, d: Any, where: str = "verification") -> "Verification":
        if not isinstance(d, dict):
            raise ValidationError(f"{where}: expected a JSON object")
        unknown = set(d) - VERIFICATION_FIELDS
        if unknown:
            raise ValidationError(f"{where}: unknown field(s) {sorted(unknown)}")
        sc = d.get("specs_confirmed")
        if not isinstance(sc, bool):
            raise ValidationError(f"{where}: 'specs_confirmed' must be true or false")
        ins = d.get("in_stock")
        if ins is not None and not isinstance(ins, bool):
            raise ValidationError(f"{where}: 'in_stock' must be true, false or null")
        return cls(
            offer_id=req_str(d, "offer_id", where),
            checked_at=parse_dt(d.get("checked_at"), f"{where}: checked_at"),
            qty_checked=to_int(d.get("qty_checked"), f"{where}: qty_checked", 1),
            observed_unit_price=to_decimal(d.get("observed_unit_price"), f"{where}: observed_unit_price"),
            specs_confirmed=sc,
            observed_shipping=opt_decimal(d.get("observed_shipping"), f"{where}: observed_shipping"),
            in_stock=ins,
            evidence=opt_str(d, "evidence", where),
            notes=opt_str(d, "notes", where),
        )


# --------------------------------------------------------------------------- requests


@dataclass
class Item:
    id: str
    qty: int
    description: str = ""
    requirements: dict[str, str] = field(default_factory=dict)
    compare_options: bool = False
    max_lead_time_days: int | None = None


@dataclass
class Request:
    title: str
    items: list[Item]
    need_by: date | None = None

    def item(self, item_id: str) -> Item | None:
        return next((i for i in self.items if i.id == item_id), None)


def load_request(path: Path) -> Request:
    where = str(path)
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ValidationError(f"{where}: invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ValidationError(f"{where}: expected a mapping")
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValidationError(f"{where}: 'items' must be a non-empty list")
    items: list[Item] = []
    for n, it in enumerate(raw_items):
        w = f"{where}: items[{n}]"
        if not isinstance(it, dict):
            raise ValidationError(f"{w}: expected a mapping")
        iid = req_str(it, "id", w)
        if not SLUG_RE.match(iid):
            raise ValidationError(f"{w}: id {iid!r} must be a lowercase slug")
        reqs = it.get("requirements") or {}
        if not isinstance(reqs, dict):
            raise ValidationError(f"{w}: 'requirements' must be a mapping of id -> testable statement")
        for rid, text in reqs.items():
            if not SLUG_RE.match(str(rid)):
                raise ValidationError(f"{w}: requirement id {rid!r} must be a lowercase slug")
            if not isinstance(text, str) or not text.strip():
                raise ValidationError(f"{w}: requirement {rid!r} needs a testable statement")
        co = it.get("compare_options", False)
        if not isinstance(co, bool):
            raise ValidationError(f"{w}: 'compare_options' must be true/false")
        items.append(
            Item(
                id=iid,
                qty=to_int(it.get("qty"), f"{w}: qty", 1),
                description=str(it.get("description") or ""),
                requirements={str(k): str(v) for k, v in reqs.items()},
                compare_options=co,
                max_lead_time_days=opt_int(it.get("max_lead_time_days"), f"{w}: max_lead_time_days", 0),
            )
        )
    if len({i.id for i in items}) != len(items):
        raise ValidationError(f"{where}: duplicate item ids")
    need_by = data.get("need_by")
    if need_by is not None and not isinstance(need_by, date):
        try:
            need_by = date.fromisoformat(str(need_by))
        except ValueError as e:
            raise ValidationError(f"{where}: need_by must be YYYY-MM-DD") from e
    return Request(title=str(data.get("title") or path.parent.name), items=items, need_by=need_by)


# --------------------------------------------------------------------------- JSONL io


def read_jsonl(path: Path) -> list[tuple[int, Any]]:
    """Return (line_number, parsed_object) for each non-blank line."""
    if not path.exists():
        return []
    rows = []
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append((n, json.loads(line)))
        except json.JSONDecodeError as e:
            raise ValidationError(f"{path}:{n}: invalid JSON: {e.msg}") from e
    return rows


def load_offers(path: Path) -> list[Offer]:
    """Load offers.jsonl, which is append-only.

    Re-capturing an offer means appending a new line with the same id and a
    newer captured_at; the newest capture wins and older lines are history.
    """
    offers, errors = [], []
    for n, obj in read_jsonl(path):
        try:
            offers.append(Offer.from_dict(obj, f"{path.name}:{n}"))
        except ValidationError as e:
            errors.append(str(e))
    seen: dict[tuple[str, datetime], dict] = {}
    ambiguous = set()
    unique: list[Offer] = []
    for o in offers:
        key, rec = (o.id, o.captured_at), o.to_dict()
        if key not in seen:
            seen[key] = rec
            unique.append(o)
        elif seen[key] != rec:
            ambiguous.add(o.id)
        # identical duplicate lines (same id, timestamp and content) are harmless; keep one
    if ambiguous:
        errors.append(
            f"{path.name}: offer id(s) {sorted(ambiguous)} have conflicting records with the same captured_at; "
            "a re-capture needs a newer timestamp"
        )
    if errors:
        raise ValidationError("\n".join(errors))
    return unique


def latest_offers(offers: list[Offer]) -> list[Offer]:
    """Keep only the newest capture per offer id, preserving first-seen order."""
    newest: dict[str, Offer] = {}
    for o in offers:
        cur = newest.get(o.id)
        if cur is None or o.captured_at > cur.captured_at:
            newest[o.id] = o
    return list(newest.values())


def load_verifications(path: Path) -> list[Verification]:
    out, errors = [], []
    for n, obj in read_jsonl(path):
        try:
            out.append(Verification.from_dict(obj, f"{path.name}:{n}"))
        except ValidationError as e:
            errors.append(str(e))
    if errors:
        raise ValidationError("\n".join(errors))
    return out


def append_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
