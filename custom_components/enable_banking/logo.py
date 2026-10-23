"""The bank's logo, as Enable Banking publishes it for each bank (ASPSP).

The logo URL comes from GET /aspsps. It is served by an image CDN that can
resize and convert it with suffixes, so a small PNG is asked for (apps can
not draw SVG). Each candidate is checked once before it is used.
"""

from __future__ import annotations

import logging

import aiohttp
from yarl import URL

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EnableBankingClient

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_SUFFIXES = (
    "-/resize/160x/-/format/png/",
    "-/rasterize/-/resize/160x/-/format/png/",
    "-/resize/160x/",
    "",
)


async def _image_type(session: aiohttp.ClientSession, url: str) -> str | None:
    """The content type of `url` when it is a raster image, else None."""
    try:
        async with session.get(url, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            kind = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            await resp.read()
    except (aiohttp.ClientError, TimeoutError):
        return None
    if not kind.startswith("image/") or "svg" in kind:
        return None
    return kind


async def async_find_logo(
    hass: HomeAssistant, client: EnableBankingClient, name: str, country: str
) -> str | None:
    """A PNG (or other raster) URL of the bank's logo, or None."""
    wanted = name.casefold()
    aspsp = next(
        (a for a in await client.list_aspsps(country) if (a.get("name") or "").casefold() == wanted),
        None,
    )
    raw = (aspsp or {}).get("logo")
    if not raw:
        return None
    base = raw if raw.endswith("/") else raw + "/"
    session = async_get_clientsession(hass)
    for suffix in _SUFFIXES:
        candidate = str(URL(base + suffix if suffix else raw))
        kind = await _image_type(session, candidate)
        if kind:
            _LOGGER.debug("Logo for %s: %s (%s)", name, candidate, kind)
            return candidate
    _LOGGER.debug("No usable logo for %s at %s", name, raw)
    return None
