"""Read-only JSON endpoint with the stored transactions of every bank.

GET /api/enable_banking/transactions?since=YYYY-MM-DD
Authenticated like the rest of the Home Assistant API (long-lived token) and
limited to the owner of the Home Assistant instance: other users, even admins,
get 403.
"""

from __future__ import annotations

from datetime import date

from aiohttp import web
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView

from . import ledger


class TransactionsView(HomeAssistantView):
    url = "/api/enable_banking/transactions"
    name = "api:enable_banking:transactions"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        user = request.get("hass_user")
        if user is None or not user.is_owner:
            return self.json_message("Solo el propietario de Home Assistant", 403)

        hass = request.app[KEY_HASS]
        since = request.query.get("since")
        if since:
            try:
                since = date.fromisoformat(since).isoformat()
            except ValueError:
                return self.json_message("since debe ser YYYY-MM-DD", 400)
        return self.json(ledger.collect(hass, since))
