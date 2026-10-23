"""Balance polling with a small on-disk cache.

PSD2 limits unattended reads to four per account per day, so every successful
read is cached. A Home Assistant restart reuses a fresh cache instead of
spending another read, and a refused read (rate limit, bank hiccup) keeps the
last known balance instead of making the sensor unavailable.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    CannotConnect,
    EnableBankingClient,
    EnableBankingError,
    RateLimited,
    SessionExpired,
    pick_balance,
)
from .const import (
    CACHE_REUSE_ON_START,
    CONF_ACCOUNTS,
    CONF_ASPSP_NAME,
    CONF_VALID_UNTIL,
    DOMAIN,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

type EnableBankingConfigEntry = ConfigEntry[EnableBankingCoordinator]


class EnableBankingCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Keeps the latest balance of every account of one bank connection.

    Data is keyed by the account's identification_hash, which, unlike the
    session-scoped uid, stays the same after the consent is renewed.
    """

    config_entry: EnableBankingConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: EnableBankingConfigEntry,
        client: EnableBankingClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_ASPSP_NAME]}",
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.cache.{entry.entry_id}"
        )
        self._cache: dict[str, Any] = {}
        self._started = False

    @property
    def accounts(self) -> list[dict[str, Any]]:
        return self.config_entry.data[CONF_ACCOUNTS]

    @property
    def consent_valid_until(self) -> datetime | None:
        return dt_util.parse_datetime(self.config_entry.data.get(CONF_VALID_UNTIL, ""))

    async def _async_setup(self) -> None:
        self._cache = await self._store.async_load() or {}

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        now = dt_util.utcnow()
        cached: dict[str, dict[str, Any]] = self._cache.get("accounts", {})
        wanted = {a["identification_hash"] for a in self.accounts}

        valid_until = self.consent_valid_until
        if valid_until and valid_until <= now:
            raise ConfigEntryAuthFailed("El consentimiento del banco ha caducado")

        if not self._started:
            self._started = True
            fetched_at = dt_util.parse_datetime(self._cache.get("fetched_at", ""))
            if (
                fetched_at
                and now - fetched_at < CACHE_REUSE_ON_START
                and wanted <= cached.keys()
            ):
                _LOGGER.debug("Reusing cached balances from %s", fetched_at)
                return {k: v for k, v in cached.items() if k in wanted}

        result: dict[str, dict[str, Any]] = {}
        failures: list[str] = []
        fresh = 0
        for account in self.accounts:
            key = account["identification_hash"]
            try:
                balances = await self.client.get_balances(account["uid"])
            except SessionExpired as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except (RateLimited, CannotConnect, EnableBankingError) as err:
                _LOGGER.warning(
                    "No se pudo leer la cuenta %s: %s", account.get("iban") or key, err
                )
                failures.append(str(err))
                if key in cached:
                    result[key] = {**cached[key], "stale": True}
                continue

            chosen = pick_balance(balances)
            if chosen is None:
                failures.append("sin saldo")
                if key in cached:
                    result[key] = {**cached[key], "stale": True}
                continue

            amount_info = chosen.get("balance_amount") or {}
            try:
                amount = round(float(amount_info.get("amount")), 2)
            except (TypeError, ValueError):
                failures.append("importe no numérico")
                continue

            result[key] = {
                "amount": amount,
                "currency": amount_info.get("currency") or account.get("currency"),
                "balance_type": chosen.get("balance_type"),
                "balance_name": chosen.get("name"),
                "reference_date": chosen.get("reference_date"),
                "fetched_at": now.isoformat(),
                "stale": False,
                "all_balances": [
                    {
                        "type": b.get("balance_type"),
                        "amount": (b.get("balance_amount") or {}).get("amount"),
                        "name": b.get("name"),
                    }
                    for b in balances
                ],
            }
            fresh += 1

        if not result:
            raise UpdateFailed("; ".join(failures) or "No se obtuvo ningún saldo")

        if fresh:
            self._cache = {"fetched_at": now.isoformat(), "accounts": {**cached, **result}}
            await self._store.async_save(self._cache)
        return result

    async def async_remove_cache(self) -> None:
        await self._store.async_remove()
