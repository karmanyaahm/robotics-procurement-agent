"""Structured sources (distributor APIs) that emit offer records."""

from __future__ import annotations

import os
import re
from pathlib import Path


class SourceError(RuntimeError):
    """API failure. Messages never include secrets or full request URLs."""


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader (KEY=VALUE lines); existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def require_env(name: str, hint: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise SourceError(f"{name} is not set ({hint})")
    return v


def parse_money(s: object) -> str | None:
    """'$1,234.56' -> '1234.56'. Assumes '.' decimal separator (US locale)."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return str(s)
    cleaned = re.sub(r"[^\d.]", "", str(s))
    return cleaned or None


def parse_leading_int(s: object) -> int | None:
    """'1,234 In Stock' -> 1234; None if no leading number."""
    if s is None:
        return None
    if isinstance(s, int):
        return s
    m = re.match(r"\s*([\d,]+)", str(s))
    return int(m.group(1).replace(",", "")) if m else None


def parse_lead_time_days(s: object) -> int | None:
    """'14 Weeks' -> 98, '10 Days' -> 10, '27' (weeks, DigiKey) handled by caller."""
    if s is None:
        return None
    m = re.match(r"\s*(\d+)\s*(day|week)", str(s), re.IGNORECASE)
    if not m:
        return None
    n = int(m.group(1))
    return n * 7 if m.group(2).lower() == "week" else n
