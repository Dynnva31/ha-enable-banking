"""Minimal async client for the Enable Banking API (read-only account information)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp
import jwt
from cryptography.hazmat.primitives import serialization

from .const import API_BASE, BALANCE_TYPE_PREFERENCE, PSU_TYPE

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=45)


class EnableBankingError(Exception):
    """Generic API error."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Enable Banking API {status}: {body[:300]}")
        self.status = status
        self.body = body


class CannotConnect(EnableBankingError):
    """Network problem talking to Enable Banking."""


class InvalidAuth(EnableBankingError):
    """The application id / private key pair was rejected."""


class ApplicationInactive(EnableBankingError):
    """The application exists but has not been activated by linking accounts."""


class SessionExpired(EnableBankingError):
    """The bank consent behind this session is no longer usable."""


class RateLimited(EnableBankingError):
    """The bank refused because the daily access limit was reached."""


class InvalidPrivateKey(Exception):
    """The supplied PEM could not be parsed as an RSA private key."""


_SESSION_GONE = re.compile(r"EXPIRED|CLOSED|REVOKED|SESSION_DOES_NOT_EXIST|CONSENT", re.I)
_RATE_LIMIT = re.compile(r"RATE_LIMIT|TOO_MANY", re.I)


def normalize_pem(raw: str) -> str:
    """Return a clean PEM string.

    Pasting a key on a phone sometimes turns the line breaks into spaces; this
    rebuilds the standard 64-character lines so the key still loads.
    """
    text = raw.strip().replace("\r\n", "\n").replace("\r", "\n")
    match = re.search(
        r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", text, re.S
    )
    if not match:
        raise InvalidPrivateKey("No PEM block found")
    label, body = match.group(1), re.sub(r"\s+", "", match.group(2))
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


def load_private_key(pem: str) -> Any:
    """Parse and return the private key object, raising InvalidPrivateKey."""
    try:
        return serialization.load_pem_private_key(
            normalize_pem(pem).encode(), password=None
        )
    except (ValueError, TypeError, InvalidPrivateKey) as err:
        raise InvalidPrivateKey(str(err)) from err


def parse_redirect(url: str) -> dict[str, str]:
    """Pull state / code / error out of the URL the bank sent the user back to."""
    query = parse_qs(urlparse(url.strip()).query)
    return {k: v[0] for k, v in query.items() if v}


def pick_balance(balances: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the balance that best represents money held (booked first)."""
    if not balances:
        return None
    by_type = {b.get("balance_type"): b for b in balances}
    for balance_type in BALANCE_TYPE_PREFERENCE:
        if balance_type in by_type:
            return by_type[balance_type]
    return balances[0]


@dataclass
class Account:
    """An account returned when a session is authorised."""

    uid: str
    identification_hash: str
    currency: str
    iban: str | None
    name: str | None
    product: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Account:
        account_id = data.get("account_id") or {}
        iban = account_id.get("iban") or (account_id.get("other") or {}).get(
            "identification"
        )
        return cls(
            uid=data["uid"],
            identification_hash=data.get("identification_hash") or data["uid"],
            currency=data.get("currency") or "EUR",
            iban=iban,
            name=data.get("name"),
            product=data.get("product"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "identification_hash": self.identification_hash,
            "currency": self.currency,
            "iban": self.iban,
            "name": self.name,
            "product": self.product,
        }


class EnableBankingClient:
    """Signs every request with a short-lived RS256 JWT."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        app_id: str,
        private_key_pem: str,
        api_base: str = API_BASE,
    ) -> None:
        self._session = session
        self._app_id = app_id.strip()
        self._key = load_private_key(private_key_pem)
        self._api_base = api_base.rstrip("/")
        self._token: tuple[str, float] | None = None

    def _jwt(self) -> str:
        now = int(time.time())
        if self._token and self._token[1] - now > 300:
            return self._token[0]
        token = jwt.encode(
            {
                "iss": "enablebanking.com",
                "aud": "api.enablebanking.com",
                "iat": now,
                "exp": now + 3600,
            },
            self._key,
            algorithm="RS256",
            headers={"kid": self._app_id},
        )
        self._token = (token, now + 3600)
        return token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        account_call: bool = False,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._jwt()}",
            "Accept": "application/json",
        }
        try:
            async with self._session.request(
                method,
                f"{self._api_base}{path}",
                json=json_body,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                text = await resp.text()
                status = resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise CannotConnect(0, str(err)) from err

        if status < 400:
            if not text:
                return {}
            try:
                return json.loads(text)
            except ValueError as err:
                raise EnableBankingError(status, text) from err

        if status == 429 or _RATE_LIMIT.search(text):
            raise RateLimited(status, text)
        if status == 403 and re.search(r"not active|inactive", text, re.I):
            raise ApplicationInactive(status, text)
        if account_call and (status in (401, 403, 410) or _SESSION_GONE.search(text)):
            raise SessionExpired(status, text)
        if status in (401, 403):
            raise InvalidAuth(status, text)
        if status >= 500:
            raise CannotConnect(status, text)
        raise EnableBankingError(status, text)

    async def get_application(self) -> dict[str, Any]:
        return await self._request("GET", "/application")

    async def list_aspsps(self, country: str) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", "/aspsps", params={"country": country, "psu_type": PSU_TYPE}
        )
        return data.get("aspsps", [])

    async def start_authorization(
        self,
        *,
        aspsp_name: str,
        country: str,
        state: str,
        redirect_url: str,
        valid_until: datetime,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/auth",
            json_body={
                "access": {"valid_until": valid_until.isoformat()},
                "aspsp": {"name": aspsp_name, "country": country},
                "state": state,
                "redirect_url": redirect_url,
                "psu_type": PSU_TYPE,
            },
        )

    async def create_session(self, code: str) -> dict[str, Any]:
        return await self._request("POST", "/sessions", json_body={"code": code})

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/sessions/{session_id}", account_call=True
        )

    async def get_balances(self, account_uid: str) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", f"/accounts/{account_uid}/balances", account_call=True
        )
        return data.get("balances", [])

    async def get_transactions(
        self,
        account_uid: str,
        *,
        date_from: date | None = None,
        max_pages: int = 25,
    ) -> list[dict[str, Any]]:
        """All transactions since date_from, following continuation keys."""
        transactions: list[dict[str, Any]] = []
        continuation: str | None = None
        for _ in range(max_pages):
            params: dict[str, str] = {}
            if date_from:
                params["date_from"] = date_from.isoformat()
            if continuation:
                params["continuation_key"] = continuation
            data = await self._request(
                "GET",
                f"/accounts/{account_uid}/transactions",
                params=params or None,
                account_call=True,
            )
            transactions.extend(data.get("transactions") or [])
            continuation = data.get("continuation_key")
            if not continuation:
                break
        return transactions
