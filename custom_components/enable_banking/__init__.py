"""Enable Banking: read-only bank balances through PSD2 open banking."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .api import EnableBankingClient, InvalidPrivateKey
from .const import CONF_APP_ID, CONF_PRIVATE_KEY, DOMAIN
from .coordinator import STORAGE_VERSION, EnableBankingConfigEntry, EnableBankingCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


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
    """Drop the balance cache when the bank is removed."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.cache.{entry.entry_id}").async_remove()
