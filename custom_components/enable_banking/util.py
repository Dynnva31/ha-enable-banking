"""Small helpers shared by the config flow and the sensors."""

from __future__ import annotations

from typing import Any


def mask_iban(iban: str | None) -> str | None:
    """'ES9100000000000000001234' -> 'ES91 •••• 1234'."""
    if not iban:
        return None
    compact = iban.replace(" ", "")
    return f"{compact[:4]} •••• {compact[-4:]}" if len(compact) > 8 else compact


def account_label(account: dict[str, Any]) -> str:
    """Short human name such as 'Cuenta ··1234'."""
    # The account "name" banks send is usually the holder's name, so prefer the
    # product name and otherwise a neutral word.
    base = account.get("product") or "Cuenta"
    iban = (account.get("iban") or "").replace(" ", "")
    return f"{base} ··{iban[-4:]}" if len(iban) >= 4 else base
