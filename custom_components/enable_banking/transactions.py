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


def from_statement(rows: list[dict[str, Any]], currency: str, before: str | None) -> list[dict[str, Any]]:
    """Movements copied from a bank statement (a CSV export), for the days
    the bank no longer hands out. Only days before `before` (the first
    movement already stored) are kept, so nothing is counted twice. Ids are
    stable, so importing the same statement again changes nothing."""
    result: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for row in rows:
        day = str(row.get("date") or "")[:10]
        try:
            date.fromisoformat(day)
            amount = round(float(row["amount"]), 2)
        except (KeyError, TypeError, ValueError):
            continue
        if before and day >= before:
            continue
        tx = {
            "date": day,
            "transaction_date": None,
            "amount": amount,
            "currency": _clean(row.get("currency")) or currency,
            "counterparty": _clean(row.get("counterparty")),
            "counterparty_iban": None,
            "description": _clean(row.get("description")),
            "status": "BOOK",
            "mcc": None,
            "bank_code": _clean(row.get("bank_code")),
        }
        fingerprint = "|".join(str(x) for x in (day, amount, tx["currency"], tx["counterparty"], tx["description"]))
        occurrence = seen.get(fingerprint, 0)
        seen[fingerprint] = occurrence + 1
        tx["id"] = "s:" + hashlib.sha1(f"{fingerprint}|{occurrence}".encode()).hexdigest()[:20]
        result.append(tx)
    return result


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


_TRANSFER_WORDS = {"TRANSFERENCIA", "TRANSFER", "TRASPASO", "TRANSF", "ORDEN", "BENEF", "ORDENANTE", "BENEFICIARIO"}


class OwnAccounts:
    """Recognises transfers between the user's own accounts, also to banks
    that are not connected (the holder's own name on the other side)."""

    def __init__(self, ibans: set[str], holder_names: list[str | None]) -> None:
        self._ibans = {i.replace(" ", "").upper() for i in ibans if i}
        self._holders = [w for w in (_words(n) for n in holder_names) if len(w) >= 2]

    def reason(self, tx: dict[str, Any], own_iban: str | None = None) -> str | None:
        """'iban' (another connected account), 'name' (the holder's own name) or None.

        Some banks (Ibercaja) put the account's own IBAN as the other side of
        incoming movements; that says nothing about who paid, so it is ignored.
        """
        iban = (tx.get("counterparty_iban") or "").replace(" ", "").upper()
        own = (own_iban or "").replace(" ", "").upper()
        if iban and iban in self._ibans and iban != own:
            return "iban"
        words = _words(tx.get("counterparty"))
        if not words:
            # No name on the other side (Ibercaja): a transfer whose concept
            # carries the holder's full name ("ORDEN: ANA MARIA GARCIA LOPEZ").
            text = _words(tx.get("description"))
            if not text & _TRANSFER_WORDS:
                return None
            if any(len(holder) >= 3 and holder <= text for holder in self._holders):
                return "name"
            return None
        for holder in self._holders:
            if holder <= words:
                return "name"
            # "Ana García López" for "Ana María García López": three words in common.
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
