import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from procure.sources import digikey, mouser, parse_lead_time_days, parse_leading_int, parse_money

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc)


def test_parsers():
    assert parse_money("$1,234.56") == "1234.56"
    assert parse_money(0.5) == "0.5"
    assert parse_leading_int("12,345 In Stock") == 12345
    assert parse_leading_int("Non-Stocked") is None
    assert parse_lead_time_days("14 Weeks") == 98
    assert parse_lead_time_days("10 days") == 10
    assert parse_lead_time_days("unknown") is None


def test_digikey_variations_become_offers():
    data = json.loads((FIX / "digikey_keyword.json").read_text())
    (product,) = digikey.exact_matches(data, "eca-1vhg102")
    offers = digikey.product_to_offers(product, "caps", None, NOW)
    assert [o.id for o in offers] == ["digikey:P5555-ND", "digikey:P5555DKR-ND"]
    bulk, reel = offers
    assert bulk.option == "ECA-1VHG102" and bulk.stock_qty == 4200 and bulk.lead_time_days == 189
    assert [(b.qty, b.unit_price) for b in bulk.price_breaks][-1] == (100, Decimal("0.56"))
    assert bulk.attributes["Tolerance"] == "±20%" and "ESR (Equivalent Series Resistance)" not in bulk.attributes
    assert bulk.shipping is None and bulk.spec_checks == {}
    assert reel.fees == Decimal("7.0") and reel.moq == 1 and "Marketplace" in reel.notes


def test_mouser_parts_become_offers():
    data = json.loads((FIX / "mouser_search.json").read_text())
    offers = [o for p in mouser.parts(data) if (o := mouser.part_to_offer(p, "standoffs", "nylon", NOW))]
    assert len(offers) == 1  # the part without price breaks is skipped
    o = offers[0]
    assert o.id == "mouser:123-NS-M3-10" and o.option == "nylon"
    assert (o.moq, o.order_multiple, o.stock_qty, o.lead_time_days) == (10, 10, 12345, 56)
    assert [(b.qty, b.unit_price) for b in o.price_breaks] == [(10, Decimal("0.18")), (1000, Decimal("0.09"))]
    assert o.attributes["Packaging"] == "Bulk, Bag"


def test_mouser_error_never_leaks_key(monkeypatch):
    import pytest
    import requests

    class R:
        status_code = 403
        text = "denied"

        def json(self):
            return {"Errors": [{"Code": "Invalid", "Message": "Invalid unique identifier SECRETKEY"}]}

    monkeypatch.setenv("MOUSER_API_KEY", "SECRETKEY")
    monkeypatch.setattr(requests, "post", lambda *a, **k: R())
    with pytest.raises(mouser.SourceError) as e:
        mouser.partnumber_search("X")
    assert "SECRETKEY" not in str(e.value)


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code, self.text = payload, status, json.dumps(payload)

    def json(self):
        return self._p


def test_cli_capture_with_spec_checks_appends_valid_offers(tmp_path, monkeypatch):
    import requests

    from procure.cli import main
    from procure.models import load_offers

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MOUSER_API_KEY", "k")
    fixture = json.loads((FIX / "mouser_search.json").read_text())
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(fixture))
    out = tmp_path / "offers.jsonl"
    checks = '{"thread": {"pass": true, "value": "M3 F-F", "evidence": "attributes"}}'
    assert main(["mouser", "mpn", "NS-M3-10", "--item", "standoffs", "--append", str(out), "--spec-checks", checks]) == 0
    (o,) = load_offers(out)
    assert o.spec_checks["thread"].passed is True and o.source == "api:mouser"


def test_cli_digikey_mpn_with_token_cache(tmp_path, monkeypatch):
    import requests

    from procure.cli import main
    from procure.models import load_offers

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "secret")
    fixture = json.loads((FIX / "digikey_keyword.json").read_text())
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        if url.endswith("/v1/oauth2/token"):
            return _Resp({"access_token": "tok", "expires_in": 599, "token_type": "Bearer"})
        assert kw["headers"]["Authorization"] == "Bearer tok"
        assert kw["json"]["Keywords"] == "ECA-1VHG102"
        return _Resp(fixture)

    monkeypatch.setattr(requests, "post", fake_post)
    out = tmp_path / "offers.jsonl"
    for _ in range(2):
        assert main(["digikey", "mpn", "ECA-1VHG102", "--item", "caps", "--append", str(out)]) == 0
    assert sum(u.endswith("/oauth2/token") for u in calls) == 1  # token cached between runs
    assert {o.id for o in load_offers(out)} == {"digikey:P5555-ND", "digikey:P5555DKR-ND"}
