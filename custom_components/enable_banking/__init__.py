"""Enable Banking: read-only bank balances through PSD2 open banking."""

from __future__ import annotations

from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError, Unauthorized
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from . import ledger

from .api import EnableBankingClient, InvalidPrivateKey
from .const import CONF_APP_ID, CONF_PRIVATE_KEY, DOMAIN
from .coordinator import STORAGE_VERSION, EnableBankingConfigEntry, EnableBankingCoordinator
from .api_view import TransactionsView

PLATFORMS: list[Platform] = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


IMPORT_SCHEMA = vol.Schema({
    vol.Required("account"): cv.entity_id,
    vol.Required("transactions"): vol.All(cv.ensure_list, vol.Length(max=20000), [dict]),
})
AUDIT_SCHEMA = vol.Schema({vol.Optional("days", default=120): vol.All(int, vol.Range(min=1, max=1100))})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Expose the stored transactions to authenticated clients (the iPhone app)."""
    hass.http.register_view(TransactionsView())

    async def audit(call: ServiceCall) -> ServiceResponse:
        """Movements with how each one was classified, to check that no
        transfer between your own accounts counts as income or spending.
        Administrators only (they can already read the stored files)."""
        user = await hass.auth.async_get_user(call.context.user_id) if call.context.user_id else None
        if user is None or not user.is_admin:
            raise Unauthorized(context=call.context)
        since = (dt_util.now().date() - timedelta(days=call.data["days"])).isoformat()
        data = ledger.collect(hass, since)
        return {
            "accounts": [
                {"account": a["entity_id"], "bank": a["bank"], "share_percent": a["share_percent"]}
                for a in data["accounts"]
            ],
            "transactions": [
                {
                    "date": t["date"],
                    "account": t["account"],
                    "amount": t["amount"],
                    "status": t["status"],
                    "counterparty": t["counterparty"],
                    "counterparty_bank": t.get("counterparty_bank"),
                    "description": t["description"],
                    "bank_code": t["bank_code"],
                    "mcc": t["mcc"],
                    "internal": t.get("internal_reason"),
                }
                for t in data["transactions"]
            ],
        }

    hass.services.async_register(
        DOMAIN, "audit", audit, schema=AUDIT_SCHEMA, supports_response=SupportsResponse.ONLY
    )

    async def read_history(call: ServiceCall) -> ServiceResponse:
        """Read all the history the banks still hold (best within the hour
        after renewing access). Administrators only."""
        user = await hass.auth.async_get_user(call.context.user_id) if call.context.user_id else None
        if user is None or not user.is_admin:
            raise Unauthorized(context=call.context)
        banks = []
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            banks.append({"bank": entry.title, "accounts": await entry.runtime_data.async_read_history()})
        return {"banks": banks}

    hass.services.async_register(
        DOMAIN, "read_history", read_history, supports_response=SupportsResponse.OPTIONAL
    )

    async def import_statement(call: ServiceCall) -> ServiceResponse:
        """Movements from a bank statement (CSV export) for the days the bank
        no longer gives. Administrators only."""
        user = await hass.auth.async_get_user(call.context.user_id) if call.context.user_id else None
        if user is None or not user.is_admin:
            raise Unauthorized(context=call.context)
        registry = er.async_get(hass)
        entity = registry.async_get(call.data["account"])
        entry = hass.config_entries.async_get_entry(entity.config_entry_id) if entity else None
        if entity is None or entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(f"{call.data['account']} no es una cuenta de Enable Banking")
        prefix = f"{entry.unique_id or entry.entry_id}_"
        key = entity.unique_id[len(prefix):] if entity.unique_id.startswith(prefix) else entity.unique_id
        return await entry.runtime_data.async_import_statement(key, call.data["transactions"])

    hass.services.async_register(
        DOMAIN, "import_statement", import_statement, schema=IMPORT_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EnableBankingConfigEntry) -> bool:
    """Set up one bank connection."""
    try:
        client = EnableBankingClient(
            async_get_clientsession(hass),
            entry.data[CONF_APP_ID],
            entry.data[CONF_PRIVATE_KEY],
        )
    except InvalidPrivateKey as err:
        raise ConfigEntryNotReady("La clave privada guardada no es válida") from err

    coordinator = EnableBankingCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: EnableBankingConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EnableBankingConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: EnableBankingConfigEntry) -> None:
    """Drop the cached balances and transactions when the bank is removed."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.cache.{entry.entry_id}").async_remove()
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.transactions.{entry.entry_id}").async_remove()
