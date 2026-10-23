"""A picture for each bank: the icon of its own app, or its logo.

1. The icon of the bank's official app in the App Store (public iTunes
   Search API): Finance apps whose publisher or name contains the bank's
   name; of those, the one most people rate (the main app, not "Business"
   or "Junior" variants).
2. Otherwise, the logo Enable Banking publishes for the bank (GET /aspsps),
   asked for as a small PNG through its image CDN.

Every candidate is downloaded once and checked to be a raster image before
it is used.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any

import aiohttp
from yarl import URL

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EnableBankingClient

_LOGGER = logging.getLogger(__name__)

LOGO_VERSION = 2

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_ITUNES = "https://itunes.apple.com/search"
_FINANCE = "6015"
_GENERIC = {"banco", "bank", "banca", "sa", "s", "a", "ag", "gmbh", "ltd", "plc", "nv", "spa", "group", "grupo", "de", "the"}
_SUFFIXES = (
    "-/resize/160x/-/format/png/",
    "-/rasterize/-/resize/160x/-/format/png/",
    "-/resize/160x/",
    "",
)


def _words(text: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", plain)


def bank_words(name: str) -> str:
    """'Ibercaja Banco' -> 'ibercaja'; 'Trade Republic' -> 'trade republic'."""
    words = [w for w in _words(name) if w not in _GENERIC]
    return " ".join(words) or " ".join(_words(name))


def pick_app(results: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """The bank's main app among App Store search results."""
    wanted = bank_words(name)
    if not wanted:
        return None

    def matches(app: dict[str, Any]) -> bool:
        genres = {str(g) for g in app.get("genreIds") or []}
        if _FINANCE not in genres and app.get("primaryGenreId") != int(_FINANCE):
            return False
        seller = " ".join(_words(app.get("sellerName") or app.get("artistName") or ""))
        title = " ".join(_words(app.get("trackName") or ""))
        return f" {wanted} " in f" {seller} " or title.startswith(wanted)

    candidates = [a for a in results if matches(a) and (a.get("artworkUrl512") or a.get("artworkUrl100"))]
    if not candidates:
        return None
    return max(candidates, key=lambda a: a.get("userRatingCount") or 0)


def icon_url(app: dict[str, Any], size: int = 256) -> str:
    """Artwork URL at `size` px, as PNG ('…/512x512bb.jpg' -> '…/256x256bb.png')."""
    url = app.get("artworkUrl512") or app.get("artworkUrl100") or ""
    return re.sub(r"/\d+x\d+bb\.(jpg|png|webp)$", f"/{size}x{size}bb.png", url)


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


async def _app_icon(session: aiohttp.ClientSession, name: str, country: str) -> tuple[str, str] | None:
    params = {
        "term": bank_words(name),
        "country": country.lower(),
        "entity": "software",
        "limit": "15",
    }
    try:
        async with session.get(_ITUNES, params=params, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
    app = pick_app((data or {}).get("results") or [], name)
    if not app:
        return None
    for candidate in (icon_url(app), app.get("artworkUrl512"), app.get("artworkUrl100")):
        if candidate and await _image_type(session, candidate):
            return candidate, app.get("trackName") or ""
    return None


async def _enable_banking_logo(
    session: aiohttp.ClientSession, client: EnableBankingClient, name: str, country: str
) -> str | None:
    wanted = name.casefold()
    aspsp = next(
        (a for a in await client.list_aspsps(country) if (a.get("name") or "").casefold() == wanted),
        None,
    )
    raw = (aspsp or {}).get("logo")
    if not raw:
        return None
    base = raw if raw.endswith("/") else raw + "/"
    for suffix in _SUFFIXES:
        candidate = str(URL(base + suffix if suffix else raw))
        if await _image_type(session, candidate):
            return candidate
    return None


async def async_find_logo(
    hass: HomeAssistant, client: EnableBankingClient, name: str, country: str
) -> tuple[str | None, str | None]:
    """(picture URL, App Store app it comes from) for the bank."""
    session = async_get_clientsession(hass)
    found = await _app_icon(session, name, country)
    if found:
        _LOGGER.debug("Picture for %s: icon of %s (%s)", name, found[1], found[0])
        return found
    logo = await _enable_banking_logo(session, client, name, country)
    _LOGGER.debug("Picture for %s: %s", name, logo or "none")
    return logo, None
