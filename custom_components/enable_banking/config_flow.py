"""Config flow: credentials, bank choice, consent at the bank, ownership share."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    FileSelector,
    FileSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)
from homeassistant.util import dt as dt_util

from .api import (
    Account,
    ApplicationInactive,
    CannotConnect,
    EnableBankingClient,
    EnableBankingError,
    InvalidAuth,
    InvalidPrivateKey,
    parse_redirect,
)
from .const import (
    CONF_ACCOUNTS,
    CONF_APP_ID,
    CONF_ASPSP_COUNTRY,
    CONF_ASPSP_NAME,
    CONF_PRIVATE_KEY,
    CONF_REDIRECT_URL,
    CONF_SESSION_ID,
    CONF_VALID_UNTIL,
    DEFAULT_COUNTRY,
    DOMAIN,
    MAX_CONSENT,
    OPT_SHARES,
)
from .util import account_label, mask_iban

_LOGGER = logging.getLogger(__name__)

FIELD_PEM_FILE = "pem_file"
FIELD_BANK = "bank"
FIELD_REDIRECT = "redirect_url"

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _read_upload(hass: HomeAssistant, file_id: str) -> tuple[str, str]:
    """Read an uploaded file (runs in the executor)."""
    from homeassistant.components.file_upload import process_uploaded_file

    with process_uploaded_file(hass, file_id) as path:
        return Path(path).read_text(encoding="utf-8", errors="replace"), Path(path).name


def _share_fields(
    accounts: list[dict[str, Any]], shares: Mapping[str, float]
) -> tuple[vol.Schema, dict[str, str]]:
    """One 0-100 % field per account; returns the schema and label -> hash map."""
    labels: dict[str, str] = {}
    schema: dict[Any, Any] = {}
    for account in accounts:
        label = account_label(account)
        masked = mask_iban(account.get("iban"))
        if masked and masked[-4:] not in label:
            label = f"{label} ({masked})"
        if account.get("currency") and account["currency"] != "EUR":
            label = f"{label} [{account['currency']}]"
        base, n = label, 2
        while label in labels:
            label, n = f"{base} #{n}", n + 1
        labels[label] = account["identification_hash"]
        schema[
            vol.Required(label, default=float(shares.get(account["identification_hash"], 100)))
        ] = NumberSelector(
            NumberSelectorConfig(
                min=0, max=100, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="%"
            )
        )
    return vol.Schema(schema), labels


class EnableBankingConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add one bank per config entry."""

    VERSION = 1

    def __init__(self) -> None:
        self._app_id: str | None = None
        self._private_key: str | None = None
        self._redirect_url: str | None = None
        self._client: EnableBankingClient | None = None
        self._aspsps: list[dict[str, Any]] | None = None
        self._aspsp: dict[str, Any] | None = None
        self._state: str | None = None
        self._auth_url: str | None = None
        self._valid_until = None
        self._last_error = ""
        self._session: dict[str, Any] | None = None
        self._accounts: list[dict[str, Any]] = []
        self._reauth = False
        self._reconfigure = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EnableBankingOptionsFlow()

    # ---- credentials -------------------------------------------------

    def _make_client(self) -> EnableBankingClient:
        assert self._app_id and self._private_key
        return EnableBankingClient(
            async_get_clientsession(self.hass), self._app_id, self._private_key
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        for entry in self._async_current_entries(include_ignore=False):
            if entry.data.get(CONF_PRIVATE_KEY):
                self._app_id = entry.data[CONF_APP_ID]
                self._private_key = entry.data[CONF_PRIVATE_KEY]
                self._redirect_url = entry.data[CONF_REDIRECT_URL]
                try:
                    self._client = self._make_client()
                except InvalidPrivateKey:
                    break
                return await self.async_step_bank()
        return await self.async_step_credentials()

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            pem, filename = None, ""
            if user_input.get(FIELD_PEM_FILE):
                pem, filename = await self.hass.async_add_executor_job(
                    _read_upload, self.hass, user_input[FIELD_PEM_FILE]
                )
            elif user_input.get(CONF_PRIVATE_KEY, "").strip():
                pem = user_input[CONF_PRIVATE_KEY]

            app_id = (user_input.get(CONF_APP_ID) or "").strip()
            if not app_id and (match := _UUID.search(filename)):
                app_id = match.group(0)

            if not pem:
                errors["base"] = "missing_key"
            elif not app_id:
                errors["base"] = "missing_app_id"
            else:
                self._app_id, self._private_key = app_id, pem
                try:
                    self._client = self._make_client()
                    app = await self._client.get_application()
                except InvalidPrivateKey:
                    errors["base"] = "invalid_key"
                except InvalidAuth:
                    errors["base"] = "invalid_auth"
                except ApplicationInactive:
                    errors["base"] = "app_inactive"
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except EnableBankingError as err:
                    _LOGGER.warning("Unexpected answer from Enable Banking: %s", err)
                    errors["base"] = "unknown"
                else:
                    redirects = app.get("redirect_urls") or []
                    if app.get("active") is False:
                        errors["base"] = "app_inactive"
                    elif not redirects:
                        errors["base"] = "no_redirect"
                    else:
                        self._redirect_url = redirects[0]
                        return await self.async_step_bank()

        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(
                {
                    vol.Optional(FIELD_PEM_FILE): FileSelector(FileSelectorConfig(accept="*")),
                    vol.Optional(CONF_PRIVATE_KEY): TextSelector(
                        TextSelectorConfig(multiline=True)
                    ),
                    vol.Optional(CONF_APP_ID): str,
                }
            ),
            errors=errors,
        )

    # ---- bank ----------------------------------------------------------

    async def async_step_bank(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._client
        if self._aspsps is None:
            try:
                self._aspsps = await self._client.list_aspsps(DEFAULT_COUNTRY)
            except EnableBankingError as err:
                return self.async_abort(
                    reason="cannot_connect", description_placeholders={"error": str(err)[:200]}
                )

        errors: dict[str, str] = {}
        if user_input is not None:
            wanted = user_input[FIELD_BANK].strip().casefold()
            self._aspsp = next(
                (a for a in self._aspsps if a.get("name", "").casefold() == wanted), None
            )
            if self._aspsp is None:
                errors["base"] = "unknown_bank"
            else:
                await self.async_set_unique_id(
                    f"{self._aspsp.get('country', DEFAULT_COUNTRY)}_{self._aspsp['name']}"
                )
                self._abort_if_unique_id_configured()
                return await self.async_step_authorize()

        names = sorted({a["name"] for a in self._aspsps if a.get("name")}, key=str.casefold)
        return self.async_show_form(
            step_id="bank",
            data_schema=vol.Schema(
                {
                    vol.Required(FIELD_BANK): SelectSelector(
                        SelectSelectorConfig(
                            options=names, custom_value=True, mode=SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
            errors=errors,
        )

    # ---- consent at the bank -----------------------------------------------

    async def _start_authorization(self) -> str | None:
        """Ask Enable Banking for the bank login URL; returns an error text on failure."""
        assert self._client and self._aspsp and self._redirect_url
        max_seconds = self._aspsp.get("maximum_consent_validity")
        validity = MAX_CONSENT
        if isinstance(max_seconds, (int, float)) and max_seconds > 0:
            validity = min(validity, timedelta(seconds=max_seconds))
        self._valid_until = dt_util.utcnow() + validity - timedelta(hours=1)
        self._state = str(uuid.uuid4())
        try:
            auth = await self._client.start_authorization(
                aspsp_name=self._aspsp["name"],
                country=self._aspsp.get("country", DEFAULT_COUNTRY),
                state=self._state,
                redirect_url=self._redirect_url,
                valid_until=self._valid_until,
            )
        except EnableBankingError as err:
            return str(err)[:300]
        self._auth_url = auth.get("url")
        return None if self._auth_url else "Enable Banking no devolvió enlace"

    async def async_step_authorize(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._client and self._aspsp
        errors: dict[str, str] = {}

        if user_input is not None:
            params = parse_redirect(user_input[FIELD_REDIRECT])
            if params.get("error"):
                errors["base"] = "bank_error"
                self._last_error = params.get("error_description") or params["error"]
                self._auth_url = None
            elif params.get("state") and params["state"] != self._state:
                errors["base"] = "state_mismatch"
            elif not params.get("code"):
                errors["base"] = "no_code"
            else:
                try:
                    session = await self._client.create_session(params["code"])
                except EnableBankingError as err:
                    errors["base"] = "session_failed"
                    self._last_error = str(err)[:300]
                    self._auth_url = None
                else:
                    accounts = [
                        Account.from_api(a).as_dict()
                        for a in session.get("accounts", [])
                        if isinstance(a, dict) and a.get("uid")
                    ]
                    if not accounts:
                        errors["base"] = "no_accounts"
                        self._auth_url = None
                    else:
                        self._session, self._accounts = session, accounts
                        if self._reauth:
                            return await self._async_finish_reauth()
                        return await self.async_step_shares()

        if self._auth_url is None:
            if failure := await self._start_authorization():
                return self.async_abort(
                    reason="auth_start_failed", description_placeholders={"error": failure}
                )

        return self.async_show_form(
            step_id="authorize",
            data_schema=vol.Schema({vol.Required(FIELD_REDIRECT): TextSelector()}),
            description_placeholders={
                "bank": self._aspsp["name"],
                "url": self._auth_url or "",
                "error": self._last_error,
            },
            errors=errors,
        )

    def _entry_data(self) -> dict[str, Any]:
        assert self._session is not None and self._aspsp is not None
        valid_until = (self._session.get("access") or {}).get("valid_until")
        if not valid_until and self._valid_until:
            valid_until = self._valid_until.isoformat()
        return {
            CONF_APP_ID: self._app_id,
            CONF_PRIVATE_KEY: self._private_key,
            CONF_REDIRECT_URL: self._redirect_url,
            CONF_ASPSP_NAME: self._aspsp["name"],
            CONF_ASPSP_COUNTRY: self._aspsp.get("country", DEFAULT_COUNTRY),
            CONF_SESSION_ID: self._session["session_id"],
            CONF_VALID_UNTIL: valid_until,
            CONF_ACCOUNTS: self._accounts,
        }

    async def async_step_shares(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        schema, labels = _share_fields(self._accounts, {})
        if user_input is not None:
            shares = {labels[k]: float(v) for k, v in user_input.items() if k in labels}
            assert self._aspsp
            return self.async_create_entry(
                title=self._aspsp["name"],
                data=self._entry_data(),
                options={OPT_SHARES: shares},
            )
        return self.async_show_form(
            step_id="shares",
            data_schema=schema,
            description_placeholders={"bank": self._aspsp["name"] if self._aspsp else ""},
        )

    # ---- reauth (consent expired) -----------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._reauth = True
        self._app_id = entry_data[CONF_APP_ID]
        self._private_key = entry_data[CONF_PRIVATE_KEY]
        self._redirect_url = entry_data[CONF_REDIRECT_URL]
        self._aspsp = {
            "name": entry_data[CONF_ASPSP_NAME],
            "country": entry_data.get(CONF_ASPSP_COUNTRY, DEFAULT_COUNTRY),
        }
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._aspsp
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                description_placeholders={"bank": self._aspsp["name"]},
            )
        try:
            self._client = self._make_client()
            aspsps = await self._client.list_aspsps(self._aspsp["country"])
        except (InvalidPrivateKey, EnableBankingError) as err:
            return self.async_abort(
                reason="cannot_connect", description_placeholders={"error": str(err)[:200]}
            )
        for aspsp in aspsps:
            if aspsp.get("name") == self._aspsp["name"]:
                self._aspsp = aspsp
                break
        return await self.async_step_authorize()

    async def _async_finish_reauth(self) -> ConfigFlowResult:
        if self._reconfigure:
            entry = self._get_reconfigure_entry()
        else:
            entry = self._get_reauth_entry()
        return self.async_update_reload_and_abort(entry, data=self._entry_data())

    # ---- reconfigure (renew access before it expires) ------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Log in to the bank again: 180 more days of access and, if the bank
        allows it, up to two years of past movements."""
        entry = self._get_reconfigure_entry()
        if not self._reconfigure:
            self._reauth = True
            self._reconfigure = True
            data = entry.data
            self._app_id = data[CONF_APP_ID]
            self._private_key = data[CONF_PRIVATE_KEY]
            self._redirect_url = data[CONF_REDIRECT_URL]
            self._aspsp = {
                "name": data[CONF_ASPSP_NAME],
                "country": data.get(CONF_ASPSP_COUNTRY, DEFAULT_COUNTRY),
            }
        if user_input is None:
            return self.async_show_form(
                step_id="reconfigure",
                description_placeholders={"bank": self._aspsp["name"] if self._aspsp else ""},
            )
        return await self.async_step_reauth_confirm(user_input)


class EnableBankingOptionsFlow(OptionsFlow):
    """Change which share of each account counts as yours."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        accounts = self.config_entry.data.get(CONF_ACCOUNTS, [])
        current = self.config_entry.options.get(OPT_SHARES, {})
        schema, labels = _share_fields(accounts, current)
        if user_input is not None:
            shares = {**current, **{labels[k]: float(v) for k, v in user_input.items() if k in labels}}
            return self.async_create_entry(data={**self.config_entry.options, OPT_SHARES: shares})
        return self.async_show_form(step_id="init", data_schema=schema)
