"""Investment prices, kept up to date on their own.

Optional: it only does something when Home Assistant has positions as
`input_text.<prefix>*` helpers holding compact JSON such as
`{"n":"Fund","isin":"IE00…","t":"fondo","u":12.5,"p":18.2,"v":227.5,"i":200,"d":"2026-11-12","y":4.1}`
(units `u`, price `p`, value `v`, invested `i`, price date `d`, year-to-date
return `y`). Every half hour it looks up each price, always in euros: funds by
their daily NAV (Morningstar, then the Financial Times), ETFs and ETCs on
Xetra (the Financial Times, by the ticker in `f` such as "VGWL:GER:EUR"),
with Yahoo Finance (`s`) as a last resort. It writes back the
newer price, the value, the date and the year's return, plus the portfolio
total and a time-weighted index (`input_number.<prefix>cartera`,
`input_number.cartera_indice`). It never goes back to an older price, never
takes one that moved more than 25 % at once, and leaves units and invested
amounts alone: those belong to whoever records the purchases.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
import re
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PREFIX = "input_text.myinvestor_"
TOTAL = "input_number.myinvestor_cartera"
INDEX = "input_number.cartera_indice"
BENCHMARK = "input_number.msci_world_precio"
# iShares Core MSCI World (EUNL) on Xetra: the reference the app compares with.
BENCHMARK_ISIN = "IE00B4L5Y983"

INTERVAL = timedelta(minutes=30)
MAX_JUMP = 0.25
MORNINGSTAR = "https://lt.morningstar.com/api/rest.svc/klr5zyak8x/security/screener"
MORNINGSTAR_UNIVERSES = ("FOESP$$ALL", "FOEUR$$ALL", "ETEUR$$ALL")
FT_FUND = "https://markets.ft.com/data/funds/tearsheet/summary"
FT_ETF = "https://markets.ft.com/data/etfs/tearsheet/summary"
BENCHMARK_FT = "EUNL:GER:EUR"
COOKIE = "https://fc.yahoo.com"
CRUMB = "https://query1.finance.yahoo.com/v1/test/getcrumb"
SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"}


class PriceUpdater:
    """Looks up and writes the prices; one per Home Assistant instance."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        # Its own cookies: Yahoo answers only with its session cookie and crumb.
        self.session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
        self.crumb: str | None = None
        self.store: Store = Store(hass, 1, f"{DOMAIN}.prices")
        self.symbols: dict[str, str] = {}
        self.loaded = False

    async def async_start(self) -> None:
        async def tick(_now: Any = None) -> None:
            try:
                await self.async_update()
            except Exception:  # noqa: BLE001 - never let a price source break the integration
                _LOGGER.exception("Investment prices could not be updated")

        async_call_later(self.hass, 120, tick)
        async_track_time_interval(self.hass, tick, INTERVAL)

    # MARK: Yahoo Finance

    async def _refresh_crumb(self) -> None:
        """Yahoo's session cookie, then the crumb that goes with it."""
        self.crumb = None
        try:
            async with self.session.get(COOKIE, headers=HEADERS, timeout=20, allow_redirects=True):
                pass
        except Exception as err:  # noqa: BLE001 - fc.yahoo.com answers 404 but sets the cookie
            _LOGGER.debug("Yahoo cookie: %s", err)
        try:
            async with self.session.get(CRUMB, headers=HEADERS, timeout=20) as response:
                text = (await response.text()).strip()
                if response.status == 200 and text and "<" not in text and len(text) < 40:
                    self.crumb = text
                else:
                    _LOGGER.debug("Yahoo crumb answered %s", response.status)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Yahoo crumb failed: %s", err)

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
        for attempt in range(2):
            if self.crumb is None:
                await self._refresh_crumb()
            query = dict(params)
            if self.crumb:
                query["crumb"] = self.crumb
            try:
                async with self.session.get(url, params=query, headers=HEADERS, timeout=20) as response:
                    if response.status == 200:
                        return await response.json(content_type=None)
                    _LOGGER.debug("%s answered %s", url, response.status)
                    if response.status not in (401, 403, 429) or attempt:
                        return None
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("%s failed: %s", url, err)
                return None
            self.crumb = None
        return None

    async def _chart(self, symbol: str) -> dict[str, Any] | None:
        """Price, its time and the year's return for one symbol, in euros."""
        data = await self._get(CHART.format(symbol=symbol), {"range": "ytd", "interval": "1d"})
        try:
            meta = data["chart"]["result"][0]["meta"]
        except (TypeError, KeyError, IndexError):
            return None
        price, when = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
        if meta.get("currency") != "EUR" or not price or not when:
            return None
        start = meta.get("chartPreviousClose") or meta.get("previousClose")
        return {
            "price": float(price),
            "date": dt_util.as_local(datetime.fromtimestamp(int(when), dt_util.UTC)).date().isoformat(),
            "ytd": (float(price) / float(start) - 1) * 100 if start else None,
        }

    async def _symbol(self, isin: str, kind: str) -> str | None:
        """The Yahoo symbol for an ISIN in euros (Xetra for listed products)."""
        if isin in self.symbols:
            return self.symbols[isin]
        data = await self._get(SEARCH, {"q": isin, "quotesCount": 10, "newsCount": 0})
        quotes = [q for q in (data or {}).get("quotes", []) if q.get("symbol")]
        listed = kind in ("etf", "etc")

        def rank(quote: dict[str, Any]) -> int:
            exchange = quote.get("exchange", "")
            if listed:
                return {"GER": 0, "FRA": 2, "MIL": 3, "AMS": 3, "PAR": 3}.get(exchange, 5)
            return 0 if quote.get("quoteType") == "MUTUALFUND" else 4

        for quote in sorted(quotes, key=rank):
            if await self._chart(quote["symbol"]):
                self.symbols[isin] = quote["symbol"]
                await self.store.async_save({"symbols": self.symbols})
                return quote["symbol"]
        return None

    # MARK: Other sources

    async def _text(self, url: str, params: dict[str, Any]) -> str | None:
        try:
            async with self.session.get(url, params=params, headers=HEADERS, timeout=20) as response:
                if response.status != 200:
                    _LOGGER.debug("%s answered %s", url, response.status)
                    return None
                return await response.text()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("%s failed: %s", url, err)
            return None

    async def _morningstar(self, isin: str, listed: bool) -> dict[str, Any] | None:
        """A fund's last NAV and its date (and the year's return)."""
        for universe in MORNINGSTAR_UNIVERSES:
            text = await self._text(MORNINGSTAR, {
                "page": 1, "pageSize": 5, "outputType": "json", "version": 1, "languageId": "es-ES",
                "currencyId": "EUR", "universeIds": universe, "term": isin,
                "securityDataPoints": "SecId|Name|ClosePrice|ClosePriceDate|ReturnM0|PriceCurrency|Isin",
            })
            try:
                rows = json.loads(text or "")["rows"]
            except (ValueError, KeyError, TypeError):
                continue
            for row in rows:
                if row.get("Isin") not in (None, isin) or not row.get("ClosePrice") or not row.get("ClosePriceDate"):
                    continue
                currency = row.get("PriceCurrency")
                if currency not in (None, "EUR") or (listed and currency != "EUR"):
                    continue
                return {"price": float(row["ClosePrice"]), "date": str(row["ClosePriceDate"])[:10],
                        "ytd": row.get("ReturnM0"), "source": "morningstar"}
        return None

    async def _ft(self, url: str, symbol: str) -> dict[str, Any] | None:
        """Price and date from a Financial Times tear sheet, only in euros."""
        html = await self._text(url, {"s": symbol})
        if not html:
            return None
        currency = re.search(r"Price \(([A-Z]{3})\)", html)
        price = re.search(r'mod-ui-data-list__value">\s*([0-9][0-9.,]*)\s*<', html)
        when = re.search(r"[Aa]s of ([A-Z][a-z]{2} \d{1,2} \d{4})", html)
        if not (currency and price and when) or currency.group(1) != "EUR":
            return None
        try:
            date = datetime.strptime(when.group(1), "%b %d %Y").date().isoformat()
            value = float(price.group(1).replace(",", ""))
        except ValueError:
            return None
        return {"price": value, "date": date, "ytd": None, "source": "ft"}

    async def _quote(self, data: dict[str, Any]) -> dict[str, Any] | None:
        isin = str(data["isin"])
        listed = str(data.get("t", "")) in ("etf", "etc")
        if listed:
            sources = []
            if data.get("f"):
                sources.append(lambda: self._ft(FT_ETF, str(data["f"])))
            sources.append(lambda: self._morningstar(isin, True))
        else:
            sources = [lambda: self._morningstar(isin, False), lambda: self._ft(FT_FUND, f"{isin}:EUR")]
        for source in sources:
            quote = await source()
            if quote:
                return quote
        symbol = data.get("s") or await self._symbol(isin, str(data.get("t", "")))
        quote = await self._chart(symbol) if symbol else None
        if quote:
            quote["source"] = "yahoo"
        return quote

    # MARK: Update

    async def async_update(self) -> dict[str, Any]:
        """Look up every position once; returns what changed (for the service)."""
        if not self.loaded:
            stored = await self.store.async_load() or {}
            self.symbols = dict(stored.get("symbols", {}))
            self.loaded = True

        positions: list[tuple[str, dict[str, Any]]] = []
        for state in self.hass.states.async_all("input_text"):
            if not state.entity_id.startswith(PREFIX):
                continue
            try:
                data = json.loads(state.state)
            except (ValueError, TypeError):
                continue
            if isinstance(data, dict) and data.get("isin") and data.get("u") is not None:
                positions.append((state.entity_id, data))
        if not positions:
            return {"positions": 0}

        changed: list[str] = []
        skipped: list[str] = []
        kept: list[str] = []
        before = after = 0.0
        total = 0.0
        for entity_id, data in positions:
            units = float(data.get("u") or 0)
            old = float(data.get("p") or 0)
            new = old
            quote = await self._quote(data)
            if quote and quote["date"] >= str(data.get("d", "")) and (
                old <= 0 or abs(quote["price"] / old - 1) <= MAX_JUMP
            ):
                new = round(quote["price"], 4)
                update = dict(data)
                update["p"] = new
                update["v"] = round(units * new, 2)
                update["d"] = quote["date"]
                if quote.get("ytd") is not None:
                    update["y"] = round(float(quote["ytd"]), 2)
                if update != data:
                    text = json.dumps(update, ensure_ascii=False, separators=(",", ":"))
                    if len(text) <= 255:
                        await self.hass.services.async_call(
                            "input_text", "set_value", {"entity_id": entity_id, "value": text}, blocking=True
                        )
                        changed.append(f"{data.get('n') or entity_id} ({quote['source']} {quote['date']})")
                    else:
                        new = old
            elif not quote:
                skipped.append(data.get("n") or entity_id)
            else:
                kept.append(f"{data.get('n') or entity_id} ({quote['source']} {quote['date']} {quote['price']})")
            before += units * old
            after += units * new
            total += units * new

        if changed:
            await self._set_number(TOTAL, round(total, 2))
            index = self.hass.states.get(INDEX)
            if index and before > 0:
                try:
                    factor = after / before
                    if 0.9 < factor < 1.1 and abs(factor - 1) > 1e-9:
                        await self._set_number(INDEX, round(float(index.state) * factor, 4))
                except ValueError:
                    pass

        benchmark = None
        if self.hass.states.get(BENCHMARK):
            quote = await self._quote({"isin": BENCHMARK_ISIN, "t": "etf", "f": BENCHMARK_FT})
            if quote and 50 < quote["price"] < 400:
                benchmark = round(quote["price"], 2)
                await self._set_number(BENCHMARK, benchmark)

        return {"positions": len(positions), "updated": changed, "no_price": skipped, "older_or_odd": kept,
                "total": round(total, 2), "benchmark": benchmark}

    async def _set_number(self, entity_id: str, value: float) -> None:
        state = self.hass.states.get(entity_id)
        if state is None:
            return
        try:
            if abs(float(state.state) - value) < 1e-9:
                return
        except ValueError:
            pass
        await self.hass.services.async_call(
            "input_number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
        )
