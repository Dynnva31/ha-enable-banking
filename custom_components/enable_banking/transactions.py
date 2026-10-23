"""Turning raw PSD2 transactions into a stable, de-duplicated list."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, timedelta
from typing import Any

from .util import mask_iban


def _clean(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def normalize(raw: dict[str, Any], currency: str) -> dict[str, Any] | None:
    """One transaction in a flat shape with a signed amount (negative = money out)."""
    amount_info = raw.get("transaction_amount") or {}
    try:
        amount = float(amount_info.get("amount"))
    except (TypeError, ValueError):
        return None

    indicator = (raw.get("credit_debit_indicator") or "").upper()
    if indicator == "DBIT":
        amount = -abs(amount)
    elif indicator == "CRDT":
        amount = abs(amount)

    booked_on = raw.get("booking_date") or raw.get("value_date") or raw.get("transaction_date")
    if not booked_on:
        return None

    outgoing = amount < 0
    party = (raw.get("creditor") if outgoing else raw.get("debtor")) or {}
    party_account = (raw.get("creditor_account") if outgoing else raw.get("debtor_account")) or {}
    remittance = raw.get("remittance_information") or []
    if isinstance(remittance, str):
        remittance = [remittance]
    description = _clean(" ".join(r for r in remittance if isinstance(r, str))) or _clean(raw.get("note"))
    code = raw.get("bank_transaction_code") or {}
    status = (raw.get("status") or "BOOK").upper()

    counterparty = _clean(party.get("name"))
    tx = {
        "date": str(booked_on)[:10],
        "transaction_date": (raw.get("transaction_date") or None),
        "amount": round(amount, 2),
        "currency": amount_info.get("currency") or currency,
        "counterparty": counterparty,
        "counterparty_iban": party_account.get("iban") or (party_account.get("other") or {}).get("identification"),
        "description": description,
        "status": "PDNG" if status in ("PDNG", "PENDING", "INFO", "HOLD") else "BOOK",
        "mcc": raw.get("merchant_category_code"),
        "bank_code": _clean(code.get("description")) or _clean(code.get("code")),
    }

    reference = raw.get("entry_reference") or raw.get("transaction_id")
    if reference:
        tx["id"] = f"r:{reference}"
    else:
        fingerprint = "|".join(
            str(x) for x in (tx["date"], tx["amount"], tx["currency"], counterparty, description, tx["status"])
        )
        tx["id"] = "h:" + hashlib.sha1(fingerprint.encode()).hexdigest()[:20]
    return tx


def merge(
    stored: dict[str, Any] | None,
    fetched: list[dict[str, Any]],
    *,
    today: date,
    keep_days: int,
) -> dict[str, Any]:
    """Merge a fresh read into what is stored for one account.

    Booked transactions are kept by id. Pending ones are replaced wholesale on
    every read, because banks usually give them a new id once they book.
    """
    booked: dict[str, dict[str, Any]] = dict((stored or {}).get("booked") or {})
    pending: list[dict[str, Any]] = []
    for tx in fetched:
        if tx["status"] == "BOOK":
            booked[tx["id"]] = tx
        else:
            pending.append(tx)

    cutoff = (today - timedelta(days=keep_days)).isoformat()
    booked = {k: v for k, v in booked.items() if v.get("date", "") >= cutoff}
    booked_ids = set(booked)
    pending = [tx for tx in pending if tx["id"] not in booked_ids]
    return {"booked": booked, "pending": pending}


def last_booked_date(stored: dict[str, Any] | None) -> date | None:
    dates = [tx.get("date") for tx in ((stored or {}).get("booked") or {}).values() if tx.get("date")]
    if not dates:
        return None
    try:
        return date.fromisoformat(max(dates))
    except ValueError:
        return None


def _words(name: str | None) -> set[str]:
    if not name:
        return set()
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().upper()
    return {w for w in re.split(r"[^A-Z]+", plain) if len(w) > 1}


def bank_code(iban: str | None) -> str | None:
    """The bank part of an IBAN (it names the bank, not the customer):
    'ES91 1544 …' -> '1544'; German IBANs carry an 8-digit code."""
    compact = (iban or "").replace(" ", "").upper()
    if len(compact) < 12 or not compact[:2].isalpha():
        return None
    return compact[4:12] if compact[:2] == "DE" else compact[4:8]


class OwnAccounts:
    """Recognises transfers between the user's own accounts, also to banks
    that are not connected (the holder's own name on the other side)."""

    def __init__(self, ibans: set[str], holder_names: list[str | None]) -> None:
        self._ibans = {i.replace(" ", "").upper() for i in ibans if i}
        self._holders = [w for w in (_words(n) for n in holder_names) if len(w) >= 2]

    def reason(self, tx: dict[str, Any]) -> str | None:
        """'iban' (a connected account), 'name' (the holder's own name) or None."""
        iban = (tx.get("counterparty_iban") or "").replace(" ", "").upper()
        if iban and iban in self._ibans:
            return "iban"
        words = _words(tx.get("counterparty"))
        if not words:
            return None
        for holder in self._holders:
            if holder <= words:
                return "name"
            # "Daniel Val Trull" for "Daniel Enric Val Trull": three words in common.
            if len(holder) >= 3 and len(holder & words) >= 3:
                return "name"
        return None

    def is_internal(self, tx: dict[str, Any]) -> bool:
        return self.reason(tx) is not None


def public(tx: dict[str, Any], *, account: str | None, bank: str, share: float, internal: bool) -> dict[str, Any]:
    """What the HTTP API returns: no full counterparty IBAN."""
    return {
        "id": tx["id"],
        "account": account,
        "bank": bank,
        "date": tx.get("date"),
        "transaction_date": tx.get("transaction_date"),
        "amount": tx.get("amount"),
        "currency": tx.get("currency"),
        "counterparty": tx.get("counterparty"),
        "counterparty_iban": mask_iban(tx.get("counterparty_iban")),
        "counterparty_bank": bank_code(tx.get("counterparty_iban")),
        "description": tx.get("description"),
        "status": tx.get("status"),
        "mcc": tx.get("mcc"),
        "bank_code": tx.get("bank_code"),
        "internal": internal,
        "share_percent": share,
    }
