"""All stored transactions of every bank, with transfers between the user's
own accounts recognised.

A movement is internal when:
- the other side is one of the connected accounts (its IBAN), or
- the other side carries the holder's own name (an account at a bank that is
  not connected, such as a broker), or
- it pairs with the opposite movement in another connected account: same
  amount and currency, at most four days apart (top-ups by card, transfers
  that arrive without a name or IBAN).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import transactions as tx_utils
from .const import CONF_ACCOUNTS, CONF_ASPSP_NAME, DOMAIN, OPT_SHARES
from .util import account_label, mask_iban

PAIR_MAX_DAYS = 4


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def match_pairs(items: list[dict[str, Any]]) -> dict[str, str]:
    """Pair each outgoing movement with an incoming one of the same amount in
    another account, closest date first. Returns id -> partner id."""
    incoming: dict[tuple[str, float], list[dict[str, Any]]] = defaultdict(list)
    for tx in items:
        if (tx.get("amount") or 0) > 0:
            incoming[(tx.get("currency") or "EUR", round(abs(tx["amount"]), 2))].append(tx)

    pairs: dict[str, str] = {}
    outgoing = sorted((t for t in items if (t.get("amount") or 0) < 0), key=lambda t: t.get("date") or "")
    for out in outgoing:
        out_day = _day(out.get("date"))
        if out_day is None:
            continue
        best: tuple[int, dict[str, Any]] | None = None
        for cand in incoming.get((out.get("currency") or "EUR", round(abs(out["amount"]), 2)), []):
            if cand["id"] in pairs or cand.get("_account") == out.get("_account"):
                continue
            cand_day = _day(cand.get("date"))
            if cand_day is None:
                continue
            gap = abs((cand_day - out_day).days)
            if gap <= PAIR_MAX_DAYS and (best is None or gap < best[0]):
                best = (gap, cand)
        if best:
            pairs[out["id"]] = best[1]["id"]
            pairs[best[1]["id"]] = out["id"]
    return pairs


def collect(hass: HomeAssistant, since: str | None = None) -> dict[str, Any]:
    """Accounts and transactions of every loaded bank, newest first."""
    all_entries = hass.config_entries.async_entries(DOMAIN)
    entries = [e for e in all_entries if e.state is ConfigEntryState.LOADED]
    own = tx_utils.OwnAccounts(
        {a.get("iban") or "" for e in all_entries for a in e.data.get(CONF_ACCOUNTS, [])},
        [a.get("name") for e in all_entries for a in e.data.get(CONF_ACCOUNTS, [])],
    )
    registry = er.async_get(hass)

    accounts: list[dict[str, Any]] = []
    raw: list[dict[str, Any]] = []
    fetched: list[str] = []
    for entry in entries:
        coordinator = entry.runtime_data
        bank = entry.data[CONF_ASPSP_NAME]
        shares = entry.options.get(OPT_SHARES, {})
        if coordinator.transactions_fetched_at:
            fetched.append(coordinator.transactions_fetched_at.isoformat())
        for account in coordinator.accounts:
            key = account["identification_hash"]
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.unique_id or entry.entry_id}_{key}")
            share = float(shares.get(key, 100))
            accounts.append({
                "entity_id": entity_id,
                "bank": bank,
                "account": account_label(account),
                "iban": mask_iban(account.get("iban")),
                "currency": account.get("currency") or "EUR",
                "share_percent": share,
            })
            for tx in coordinator.account_transactions(key):
                raw.append({**tx, "_account": entity_id or key, "_bank": bank, "_share": share})

    # Pairs are found over everything stored, so a "since" filter does not
    # hide the other half of a transfer.
    pairs = match_pairs(raw)
    items: list[dict[str, Any]] = []
    for tx in raw:
        if since and (tx.get("date") or "") < since:
            continue
        reason = own.reason(tx) or ("pair" if tx["id"] in pairs else None)
        item = tx_utils.public(
            tx, account=tx["_account"], bank=tx["_bank"], share=tx["_share"], internal=reason is not None
        )
        item["internal_reason"] = reason
        items.append(item)

    items.sort(key=lambda t: (t["status"] == "PDNG", t.get("date") or "", t.get("transaction_date") or ""), reverse=True)
    return {"accounts": accounts, "transactions": items, "fetched_at": max(fetched) if fetched else None}
