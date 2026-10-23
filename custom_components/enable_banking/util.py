"""Small helpers shared by the config flow and the sensors."""

from __future__ import annotations

import re
from typing import Any

# Some banks send an internal product code instead of a product name.
_PRODUCT_CODES = {
    "CACC": "Cuenta corriente",
    "CHECKING_ACCOUNT": "Cuenta corriente",
    "CURRENT_ACCOUNT": "Cuenta corriente",
    "SVGS": "Cuenta de ahorro",
    "SAVINGS_ACCOUNT": "Cuenta de ahorro",
    "CARD": "Tarjeta",
}


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
    product = (account.get("product") or "").strip()
    if product.upper() in _PRODUCT_CODES:
        base = _PRODUCT_CODES[product.upper()]
    elif product and not re.fullmatch(r"[A-Z0-9_]+", product):
        base = product
    else:
        base = "Cuenta"
    iban = (account.get("iban") or "").replace(" ", "")
    return f"{base} ··{iban[-4:]}" if len(iban) >= 4 else base
