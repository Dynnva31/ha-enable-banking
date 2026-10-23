"""Read-only JSON endpoint with the stored transactions of every bank.

GET /api/enable_banking/transactions?since=YYYY-MM-DD
Authenticated like the rest of the Home Assistant API (long-lived token).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from aiohttp import web
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView

from . import transactions as tx_utils
from .const import CONF_ACCOUNTS, CONF_ASPSP_NAME, DOMAIN, OPT_SHARES
from .util import account_label, mask_iban


class TransactionsView(HomeAssistantView):
    url = "/api/enable_banking/transactions"
    name = "api:enable_banking:transactions"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app[KEY_HASS]
        since = request.query.get("since")
        if since:
            try:
                since = date.fromisoformat(since).isoformat()
            except ValueError:
                return self.json_message("since debe ser YYYY-MM-DD", 400)

        entries = [
            e for e in hass.config_entries.async_entries(DOMAIN)
            if e.state is ConfigEntryState.LOADED
        ]
        own = tx_utils.OwnAccounts(
            {a.get("iban") or "" for e in hass.config_entries.async_entries(DOMAIN) for a in e.data.get(CONF_ACCOUNTS, [])},
            [a.get("name") for e in hass.config_entries.async_entries(DOMAIN) for a in e.data.get(CONF_ACCOUNTS, [])],
        )
        registry = er.async_get(hass)

        accounts: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        fetched: list[str] = []
        for entry in entries:
            coordinator = entry.runtime_data
            bank = entry.data[CONF_ASPSP_NAME]
            shares = entry.options.get(OPT_SHARES, {})
            if coordinator.transactions_fetched_at:
                fetched.append(coordinator.transactions_fetched_at.isoformat())
            for account in coordinator.accounts:
                key = account["identification_hash"]
                entity_id = registry.async_get_entity_id(
                    "sensor", DOMAIN, f"{entry.unique_id or entry.entry_id}_{key}"
                )
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
                    if since and (tx.get("date") or "") < since:
                        continue
                    items.append(tx_utils.public(
                        tx, account=entity_id, bank=bank, share=share, internal=own.is_internal(tx)
                    ))

        items.sort(key=lambda t: (t["status"] == "PDNG", t.get("date") or "", t.get("transaction_date") or ""), reverse=True)
        return self.json({
            "accounts": accounts,
            "transactions": items,
            "fetched_at": max(fetched) if fetched else None,
        })
