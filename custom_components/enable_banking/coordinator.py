"""Balance polling with a small on-disk cache.

PSD2 limits unattended reads to four per account per day, so every successful
read is cached. A Home Assistant restart reuses a fresh cache instead of
spending another read, and a refused read (rate limit, bank hiccup) keeps the
last known balance instead of making the sensor unavailable.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
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
from . import history
from . import transactions as tx_utils
from .util import mask_iban
from .const import (
    CACHE_REUSE_ON_START,
    CONF_ACCOUNTS,
    CONF_ASPSP_NAME,
    CONF_SESSION_ID,
    CONF_VALID_UNTIL,
    DOMAIN,
    TRANSACTIONS_HISTORY_DAYS,
    TRANSACTIONS_INITIAL_DAYS,
    TRANSACTIONS_INTERVAL,
    TRANSACTIONS_KEEP_DAYS,
    TRANSACTIONS_OVERLAP_DAYS,
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
        self._tx_store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.transactions.{entry.entry_id}"
        )
        self._tx: dict[str, Any] = {}
        self._started = False

    @property
    def accounts(self) -> list[dict[str, Any]]:
        return self.config_entry.data[CONF_ACCOUNTS]

    @property
    def consent_valid_until(self) -> datetime | None:
        return dt_util.parse_datetime(self.config_entry.data.get(CONF_VALID_UNTIL, ""))

    @property
    def transactions_fetched_at(self) -> datetime | None:
        return dt_util.parse_datetime(self._tx.get("fetched_at", ""))

    def account_transactions(self, key: str) -> list[dict[str, Any]]:
        """Pending first, then booked, newest first."""
        stored = (self._tx.get("accounts") or {}).get(key) or {}
        booked = sorted(
            (stored.get("booked") or {}).values(),
            key=lambda t: (t.get("date") or "", t.get("transaction_date") or ""),
            reverse=True,
        )
        return list(stored.get("pending") or []) + booked

    async def _async_setup(self) -> None:
        self._cache = await self._store.async_load() or {}
        self._tx = await self._tx_store.async_load() or {}

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        data = await self._async_update_balances()
        await self._async_update_transactions()
        await self._async_backfill_history(data)
        return data

    async def _async_backfill_history(self, data: dict[str, dict[str, Any]]) -> None:
        """Once per account: rebuild the past daily balances from the stored
        transactions so the history charts do not start empty."""
        stored = self._tx.get("accounts") or {}
        if not stored:
            return
        done = set(self._tx.get("history_done") or [])
        registry = er.async_get(self.hass)
        prefix = self.config_entry.unique_id or self.config_entry.entry_id
        changed = False
        for account in self.accounts:
            key = account["identification_hash"]
            balance = data.get(key) or {}
            if key in done or balance.get("stale") or balance.get("amount") is None:
                continue
            booked = list(((stored.get(key) or {}).get("booked") or {}).values())
            if not booked:
                continue
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{prefix}_{key}")
            if not entity_id:
                continue
            try:
                finished = await history.async_backfill(self.hass, entity_id, float(balance["amount"]), booked)
            except Exception:  # noqa: BLE001 - the history is a bonus, never break the balances
                _LOGGER.exception("No se pudo reconstruir el historial de %s", entity_id)
                finished = True
            if finished:
                done.add(key)
                changed = True
        if changed:
            self._tx = {**self._tx, "history_done": sorted(done)}
            await self._tx_store.async_save(self._tx)

    async def _async_update_transactions(self) -> None:
        """Read new transactions twice a day; failures keep what is stored.

        Right after a login (a new connection or renewed access) it reads up
        to two years back at once, because many banks only release movements
        older than 90 days in the minutes after the customer authenticates.
        """
        now = dt_util.utcnow()
        session = self.config_entry.data.get(CONF_SESSION_ID)
        if "session_id" not in self._tx and self._tx.get("accounts"):
            # Stored before sessions were tracked: that was this same login.
            self._tx = {**self._tx, "session_id": session}
        fresh_login = self._tx.get("session_id") != session
        fetched_at = self.transactions_fetched_at
        if not fresh_login and fetched_at and now - fetched_at < TRANSACTIONS_INTERVAL:
            return

        today = dt_util.now().date()
        stored_accounts: dict[str, Any] = dict(self._tx.get("accounts") or {})
        updated = 0
        for account in self.accounts:
            key = account["identification_hash"]
            last = tx_utils.last_booked_date(stored_accounts.get(key))
            if fresh_login:
                date_from = today - timedelta(days=TRANSACTIONS_HISTORY_DAYS)
            elif last:
                date_from = last - timedelta(days=TRANSACTIONS_OVERLAP_DAYS)
            else:
                date_from = today - timedelta(days=TRANSACTIONS_INITIAL_DAYS)
            try:
                try:
                    if fresh_login:
                        # Everything the bank still holds; the start date is a hint.
                        raw = await self.client.get_transactions(
                            account["uid"], date_from=date_from, strategy="longest", max_pages=200
                        )
                    else:
                        raw = await self.client.get_transactions(account["uid"], date_from=date_from)
                except (RateLimited, CannotConnect, SessionExpired):
                    raise
                except EnableBankingError as err:
                    # Some banks reject a start date they consider too old.
                    _LOGGER.warning(
                        "%s no da movimientos desde %s (%s); se leen los recientes",
                        mask_iban(account.get("iban")) or key[:8], date_from, str(err)[:200],
                    )
                    raw = await self.client.get_transactions(account["uid"])
            except (RateLimited, CannotConnect, EnableBankingError) as err:
                _LOGGER.warning("No se pudieron leer los movimientos de %s: %s", mask_iban(account.get("iban")) or key[:8], err)
                continue

            currency = account.get("currency") or "EUR"
            normalized = [t for t in (tx_utils.normalize(r, currency) for r in raw) if t]
            stored_accounts[key] = tx_utils.merge(
                stored_accounts.get(key), normalized, today=today, keep_days=TRANSACTIONS_KEEP_DAYS
            )
            updated += 1

        if updated:
            self._tx = {
                **self._tx,
                "fetched_at": now.isoformat(),
                "accounts": stored_accounts,
                "session_id": session,
            }
            if fresh_login:
                # Older days may have arrived: rebuild the history again.
                self._tx["history_done"] = []
            await self._tx_store.async_save(self._tx)

    async def async_read_history(self) -> list[dict[str, Any]]:
        """Read everything each bank still holds, now.

        Meant for the hour after logging in to the bank, when most banks
        release movements older than 90 days. Tries a few ways of asking,
        because banks differ, and reports how far back each one got.
        """
        today = dt_util.now().date()
        short = today - timedelta(days=100)
        stored_accounts: dict[str, Any] = dict(self._tx.get("accounts") or {})
        report: list[dict[str, Any]] = []
        for account in self.accounts:
            key = account["identification_hash"]
            currency = account.get("currency") or "EUR"
            attempts: list[dict[str, Any]] = []
            ways = (
                ("longest_730", {"date_from": today - timedelta(days=730), "strategy": "longest"}),
                ("longest", {"strategy": "longest"}),
                ("default_365", {"date_from": today - timedelta(days=365)}),
            )
            for name, kwargs in ways:
                try:
                    raw = await self.client.get_transactions(account["uid"], max_pages=200, **kwargs)
                except (RateLimited, SessionExpired) as err:
                    attempts.append({"way": name, "error": str(err)[:200]})
                    break
                except EnableBankingError as err:
                    attempts.append({"way": name, "error": str(err)[:200]})
                    continue
                normalized = [t for t in (tx_utils.normalize(r, currency) for r in raw) if t]
                dates = sorted(t["date"] for t in normalized if t.get("date"))
                attempts.append({"way": name, "read": len(normalized), "oldest": dates[0] if dates else None})
                stored_accounts[key] = tx_utils.merge(
                    stored_accounts.get(key), normalized, today=today, keep_days=TRANSACTIONS_KEEP_DAYS
                )
                if dates and date.fromisoformat(dates[0][:10]) < short:
                    break
            report.append({
                "account": mask_iban(account.get("iban")) or key[:8],
                "currency": currency,
                "attempts": attempts,
            })

        self._tx = {**self._tx, "accounts": stored_accounts, "history_done": []}
        await self._tx_store.async_save(self._tx)
        await self._async_backfill_history(self.data or {})
        self.async_update_listeners()
        return report

    async def async_import_statement(self, key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Add movements from a bank statement before the first stored one,
        then rebuild that account's past balances."""
        account = next((a for a in self.accounts if a["identification_hash"] == key), None)
        if account is None:
            return {"imported": 0}
        stored_accounts: dict[str, Any] = dict(self._tx.get("accounts") or {})
        stored = stored_accounts.get(key) or {}
        booked: dict[str, Any] = dict(stored.get("booked") or {})
        # The cut is the first movement read from the bank, not an imported one.
        dates = [t["date"] for k, t in booked.items() if t.get("date") and not k.startswith("s:")]
        first = min(dates) if dates else None
        rows_in = tx_utils.from_statement(rows, account.get("currency") or "EUR", first)
        new = [t for t in rows_in if t["id"] not in booked]
        for tx in new:
            booked[tx["id"]] = tx
        stored_accounts[key] = {"booked": booked, "pending": list(stored.get("pending") or [])}
        done = [k for k in (self._tx.get("history_done") or []) if k != key]
        self._tx = {**self._tx, "accounts": stored_accounts, "history_done": done}
        await self._tx_store.async_save(self._tx)
        await self._async_backfill_history(self.data or {})
        self.async_update_listeners()
        kept = sorted(t["date"] for t in rows_in)
        return {
            "imported": len(new),
            "already_there": len(rows_in) - len(new),
            "ignored": len(rows) - len(rows_in),
            "from": kept[0] if kept else None,
            "to": kept[-1] if kept else None,
        }

    async def _async_update_balances(self) -> dict[str, dict[str, Any]]:
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
                    "No se pudo leer la cuenta %s: %s", mask_iban(account.get("iban")) or key[:8], err
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
        await self._tx_store.async_remove()
