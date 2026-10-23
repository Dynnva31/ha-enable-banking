"""Balance sensors, one per bank account, plus the consent expiry date."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_MY_SHARE,
    ATTR_SHARE_PERCENT,
    CONF_ASPSP_NAME,
    DOMAIN,
    OPT_SHARES,
)
from .coordinator import EnableBankingConfigEntry, EnableBankingCoordinator
from .util import account_label, mask_iban




async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnableBankingConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        AccountBalanceSensor(coordinator, account) for account in coordinator.accounts
    ]
    entities.append(ConsentExpirySensor(coordinator))
    async_add_entities(entities)


class _BankEntity(CoordinatorEntity[EnableBankingCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: EnableBankingCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data[CONF_ASPSP_NAME],
            manufacturer="Enable Banking",
            model="PSD2 · solo lectura",
            entry_type=DeviceEntryType.SERVICE,
        )


class AccountBalanceSensor(_BankEntity, SensorEntity):
    """Balance of one account."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: EnableBankingCoordinator, account: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._account = account
        self._key = account["identification_hash"]
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_{self._key}"
        self._attr_name = account_label(account)
        self._attr_native_unit_of_measurement = account.get("currency") or "EUR"

    @property
    def _data(self) -> dict[str, Any] | None:
        return (self.coordinator.data or {}).get(self._key)

    @property
    def available(self) -> bool:
        return super().available and self._data is not None

    @property
    def native_value(self) -> float | None:
        data = self._data
        return data["amount"] if data else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        data = self._data
        return (data or {}).get("currency") or self._attr_native_unit_of_measurement

    @property
    def share_percent(self) -> float:
        shares = self.coordinator.config_entry.options.get(OPT_SHARES, {})
        return float(shares.get(self._key, 100))

    @property
    def entity_picture(self) -> str | None:
        return self.coordinator.logo

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._data or {}
        amount = data.get("amount")
        share = self.share_percent
        valid_until = self.coordinator.consent_valid_until
        return {
            "bank": self.coordinator.config_entry.data[CONF_ASPSP_NAME],
            "account": account_label(self._account),
            "iban": mask_iban(self._account.get("iban")),
            "currency": data.get("currency") or self._account.get("currency"),
            ATTR_SHARE_PERCENT: share,
            ATTR_MY_SHARE: round(amount * share / 100, 2) if amount is not None else None,
            "balance_type": data.get("balance_type"),
            "reference_date": data.get("reference_date"),
            "fetched_at": data.get("fetched_at"),
            "stale": data.get("stale", False),
            "consent_valid_until": valid_until.isoformat() if valid_until else None,
        }


class ConsentExpirySensor(_BankEntity, SensorEntity):
    """When the bank consent ends and the bank has to be linked again."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Consentimiento hasta"
    _attr_icon = "mdi:calendar-lock"

    def __init__(self, coordinator: EnableBankingCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_consent"

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.consent_valid_until

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"bank": self.coordinator.config_entry.data[CONF_ASPSP_NAME]}
