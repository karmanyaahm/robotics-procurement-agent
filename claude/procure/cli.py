"""procure — the deterministic half of the procurement agent.

    procure new <slug>                         scaffold requests/<date>-<slug>/
    procure digikey mpn|search ...             capture offers from DigiKey's API
    procure mouser  mpn|search ...             capture offers from Mouser's API
    procure validate <request_dir>             schema-check request/offers/verifications
    procure compare <request_dir> [--json]     rank offers, write comparison.md
    procure verify-list <request_dir>          what the verifier must re-check (JSON)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date
from pathlib import Path

from . import compare as cmp
from .config import DEFAULT_CONFIG_DIR, load_policy, load_vendors
from .models import (
    SLUG_RE,
    Offer,
    SpecCheck,
    ValidationError,
    append_jsonl,
    latest_offers,
    load_offers,
    load_request,
    load_verifications,
    utcnow,
)
from .sources import SourceError, load_dotenv

TEMPLATE_DIR = Path("requests/_template")


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def _load_all(request_dir: Path, config_dir: Path):
    paths = cmp.request_paths(request_dir)
    request = load_request(paths["request"])
    offers = load_offers(paths["offers"])
    verifications = load_verifications(paths["verifications"])
    vendors = load_vendors(config_dir)
    policy = load_policy(config_dir)
    return request, offers, verifications, vendors, policy


# --------------------------------------------------------------------------- commands


def cmd_new(args) -> int:
    slug = args.slug.lower()
    if not SLUG_RE.match(slug):
        _err("slug must be lowercase letters, digits, '-', '_' or '.'")
        return 2
    dest = Path(args.root) / f"{date.today().isoformat()}-{slug}"
    if dest.exists():
        _err(f"{dest} already exists")
        return 1
    if not TEMPLATE_DIR.exists():
        _err(f"template not found at {TEMPLATE_DIR} (run from the repo root)")
        return 1
    shutil.copytree(TEMPLATE_DIR, dest)
    (dest / "evidence").mkdir(exist_ok=True)
    print(dest)
    return 0


def _spec_checks_arg(raw: str | None) -> dict | None:
    """--spec-checks accepts inline JSON or @path/to/file.json."""
    if not raw:
        return None
    text = Path(raw[1:]).read_text() if raw.startswith("@") else raw
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValidationError(f"--spec-checks is not valid JSON: {e.msg}") from e
    if not isinstance(data, dict):
        raise ValidationError("--spec-checks must be a JSON object keyed by requirement id")
    for k, v in data.items():
        SpecCheck.from_dict(v, f"--spec-checks.{k}")  # fail fast, before any API call
    return data


def _emit(offers, args) -> int:
    checks = args.spec_checks_parsed
    if checks is not None:
        # re-validate each record with the spec checks applied
        offers = [Offer.from_dict({**o.to_dict(), "spec_checks": checks}, where=o.id) for o in offers]
    records = [o.to_dict() for o in offers]
    for r in records:
        print(json.dumps(r, ensure_ascii=False))
    if args.append and records:
        append_jsonl(Path(args.append), records)
        print(f"appended {len(records)} offer(s) to {args.append}", file=sys.stderr)
    if not records:
        print("no offers with pricing found", file=sys.stderr)
        return 1
    return 0


def cmd_digikey(args) -> int:
    from .sources import digikey

    now = utcnow()
    if args.mode == "mpn":
        data = digikey.keyword_search(args.query, limit=10)
        products = digikey.exact_matches(data, args.query)
        if not products:
            _err(f"no exact MPN match for {args.query!r} on DigiKey; try `procure digikey search`")
            return 1
    else:
        data = digikey.keyword_search(args.query, limit=args.limit)
        products = data.get("Products") or []
    if args.raw:
        print(json.dumps(data, indent=2))
        return 0
    offers = [o for p in products for o in digikey.product_to_offers(p, args.item, args.option, now)]
    return _emit(offers, args)


def cmd_mouser(args) -> int:
    from .sources import mouser

    now = utcnow()
    if args.mode == "mpn":
        data = mouser.partnumber_search(args.query, exact=True)
    else:
        data = mouser.keyword_search(args.query, records=args.limit, in_stock_only=args.in_stock)
    if args.raw:
        print(json.dumps(data, indent=2))
        return 0
    offers = [o for p in mouser.parts(data) if (o := mouser.part_to_offer(p, args.item, args.option, now))]
    return _emit(offers, args)


def cmd_validate(args) -> int:
    request_dir = Path(args.request_dir)
    try:
        request, offers, verifications, vendors, policy = _load_all(request_dir, Path(args.config))
    except ValidationError as e:
        _err(str(e))
        return 1
    problems = []
    item_ids = {i.id for i in request.items}
    current = latest_offers(offers)
    for o in current:
        if o.item not in item_ids:
            problems.append(f"offer {o.id}: item '{o.item}' not in request.yaml")
        if o.vendor not in vendors:
            problems.append(f"offer {o.id}: vendor '{o.vendor}' not in vendors.yaml")
        it = next((i for i in request.items if i.id == o.item), None)
        if it:
            missing = sorted(set(it.requirements) - set(o.spec_checks))
            if missing:
                problems.append(f"offer {o.id}: no spec_checks for {missing}")
    offer_ids = {o.id for o in current}
    for v in verifications:
        if v.offer_id not in offer_ids:
            problems.append(f"verification for unknown offer id {v.offer_id}")
    if not any(v.approved for v in vendors.values()):
        problems.append("config: no vendor is approved in vendors.yaml")
    for p in problems:
        print(f"warning: {p}")
    print(
        f"ok: {len(request.items)} item(s), {len(current)} current offer(s) "
        f"({len(offers)} capture(s)), {len(verifications)} verification(s)"
    )
    return 0


def cmd_compare(args) -> int:
    request_dir = Path(args.request_dir)
    try:
        request, offers, verifications, vendors, policy = _load_all(request_dir, Path(args.config))
    except ValidationError as e:
        _err(str(e))
        return 1
    if not any(v.approved for v in vendors.values()):
        _err("no approved vendors in vendors.yaml — fill in your approved list first")
        return 1
    now = utcnow()
    results = cmp.compare(request, offers, vendors, policy, verifications, now)
    if args.json:
        print(json.dumps(cmp.results_json(results), indent=2))
        return 0
    md = cmp.render_markdown(request, results, policy, now, cmp.orphan_offers(request, offers))
    cmp.request_paths(request_dir)["comparison"].write_text(md + "\n")
    print(md)
    return 0


def cmd_verify_list(args) -> int:
    request_dir = Path(args.request_dir)
    try:
        request, offers, verifications, vendors, policy = _load_all(request_dir, Path(args.config))
    except ValidationError as e:
        _err(str(e))
        return 1
    results = cmp.compare(request, offers, vendors, policy, verifications, utcnow())
    todo = cmp.verify_list(results, vendors, top=args.top)
    print(json.dumps({"request_dir": str(request_dir), "to_verify": todo}, indent=2))
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="procure", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="scaffold a request folder from requests/_template")
    p.add_argument("slug")
    p.add_argument("--root", default="requests")
    p.set_defaults(func=cmd_new)

    for name, fn in (("digikey", cmd_digikey), ("mouser", cmd_mouser)):
        p = sub.add_parser(name, help=f"capture offers from the {name} API")
        p.add_argument("mode", choices=["mpn", "search"], help="mpn = exact manufacturer/vendor part number; search = keyword")
        p.add_argument("query")
        p.add_argument("--item", required=True, help="item id from request.yaml")
        p.add_argument("--option", help="option label (default: the part's MPN)")
        p.add_argument("--limit", type=int, default=10)
        p.add_argument("--append", metavar="OFFERS_JSONL", help="append results to this offers.jsonl")
        p.add_argument("--raw", action="store_true", help="print the raw API response instead")
        p.add_argument(
            "--spec-checks",
            metavar="JSON|@FILE",
            help="spec_checks object to attach to every emitted offer (after you've checked attributes/datasheet)",
        )
        if name == "mouser":
            p.add_argument("--in-stock", action="store_true")
        p.set_defaults(func=fn)

    for name, fn, hlp in (
        ("validate", cmd_validate, "schema-check a request folder"),
        ("compare", cmd_compare, "rank offers and write comparison.md"),
        ("verify-list", cmd_verify_list, "list offers the verifier must re-check"),
    ):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("request_dir")
        p.add_argument("--config", default=str(DEFAULT_CONFIG_DIR), help="directory with vendors.yaml and policy.yaml")
        if name == "compare":
            p.add_argument("--json", action="store_true")
        if name == "verify-list":
            p.add_argument("--top", type=int, default=2, help="top-N offers per item to verify (plus best per option)")
        p.set_defaults(func=fn)
    return ap


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        if hasattr(args, "spec_checks"):
            args.spec_checks_parsed = _spec_checks_arg(args.spec_checks)
        return args.func(args)
    except (SourceError, ValidationError, OSError) as e:
        _err(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
