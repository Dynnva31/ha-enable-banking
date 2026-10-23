"""End-to-end tests against a mocked Enable Banking API."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import jwt
import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.enable_banking.api import normalize_pem, parse_redirect, pick_balance
from custom_components.enable_banking.const import DOMAIN, OPT_SHARES

API = "https://api.enablebanking.com"
APP_ID = "11111111-2222-4333-8444-555555555555"
IBAN = "ES1234567890123456789012"


def _mock_api(aioclient_mock, *, balances_status=200):
    aioclient_mock.get(
        f"{API}/application",
        json={"name": "Patrimonio", "kid": APP_ID, "environment": "PRODUCTION",
              "redirect_urls": ["https://localhost/"], "active": True},
    )
    aioclient_mock.get(
        f"{API}/aspsps",
        params={"country": "ES", "psu_type": "personal"},
        json={"aspsps": [
            {"name": "Ibercaja Banco", "country": "ES", "maximum_consent_validity": 15552000},
            {"name": "Revolut", "country": "ES", "maximum_consent_validity": 7776000},
        ]},
    )
    aioclient_mock.post(f"{API}/auth", json={"url": "https://bank.example/login?x=1", "authorization_id": "a1"})
    aioclient_mock.post(
        f"{API}/sessions",
        json={
            "session_id": "s-1",
            "aspsp": {"name": "Ibercaja Banco", "country": "ES"},
            "psu_type": "personal",
            "access": {"valid_until": (dt_util.utcnow() + timedelta(days=170)).isoformat()},
            "accounts": [{
                "uid": "uid-1", "identification_hash": "hash-1", "currency": "EUR",
                "account_id": {"iban": IBAN}, "name": "Ana García López",
            }],
        },
    )
    # Most specific mock first: the mock matches on a subset of the query.
    aioclient_mock.get(
        f"{API}/accounts/uid-1/transactions",
        params={"date_from": (dt_util.now().date() - timedelta(days=89)).isoformat(), "continuation_key": "page2"},
        json={"transactions": [
            {"transaction_amount": {"currency": "EUR", "amount": "12.00"}, "credit_debit_indicator": "DBIT",
             "status": "PDNG", "booking_date": "2026-10-23", "creditor": {"name": "SPOTIFY"}},
        ]},
    )
    aioclient_mock.get(
        f"{API}/accounts/uid-1/transactions",
        params={"date_from": (dt_util.now().date() - timedelta(days=89)).isoformat()},
        json={
            "transactions": [
                {"entry_reference": "e1", "transaction_amount": {"currency": "EUR", "amount": "54.20"},
                 "credit_debit_indicator": "DBIT", "status": "BOOK", "booking_date": "2026-10-20",
                 "creditor": {"name": "MERCADONA S.A."}, "remittance_information": ["COMPRA TARJ. MERCADONA"],
                 "merchant_category_code": "5411"},
                {"transaction_amount": {"currency": "EUR", "amount": "300.00"},
                 "credit_debit_indicator": "CRDT", "status": "BOOK", "booking_date": "2026-10-21",
                 "debtor": {"name": "García López Ana"}, "debtor_account": {"iban": "ES7600000000000000000001"},
                 "remittance_information": ["Traspaso"]},
            ],
            "continuation_key": "page2",
        },
    )
    if balances_status == 200:
        aioclient_mock.get(
            f"{API}/accounts/uid-1/balances",
            json={"balances": [
                {"balance_type": "ITAV", "balance_amount": {"currency": "EUR", "amount": "999.00"}},
                {"balance_type": "CLBD", "balance_amount": {"currency": "EUR", "amount": "1234.56"},
                 "reference_date": "2026-10-23"},
            ]},
        )
    else:
        aioclient_mock.get(f"{API}/accounts/uid-1/balances", status=balances_status,
                           text='{"error": "EXPIRED_SESSION"}')


async def _run_flow(hass: HomeAssistant, pem: str, share: float = 50):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "credentials"

    # Line breaks collapsed into spaces, as a phone paste sometimes does.
    flattened = pem.replace("\n", " ")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"private_key": flattened, "app_id": APP_ID}
    )
    assert result["step_id"] == "bank", result

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"bank": "Ibercaja Banco"})
    assert result["step_id"] == "authorize", result
    assert result["description_placeholders"]["url"] == "https://bank.example/login?x=1"

    flow = hass.config_entries.flow._progress[result["flow_id"]]
    state = flow._state

    bad = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"redirect_url": "https://localhost/?state=wrong&code=abc"}
    )
    assert bad["errors"] == {"base": "state_mismatch"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"redirect_url": f"https://localhost/?state={state}&code=abc"}
    )
    assert result["step_id"] == "shares", result
    label = next(iter(result["data_schema"].schema))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {str(label): share})
    return result


async def test_full_flow_and_sensor(hass: HomeAssistant, aioclient_mock, pem) -> None:
    _mock_api(aioclient_mock)
    result = await _run_flow(hass, pem, share=50)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Ibercaja Banco"
    assert result["options"] == {OPT_SHARES: {"hash-1": 50.0}}
    entry = result["result"]
    await hass.async_block_till_done()

    states = [s for s in hass.states.async_all("sensor") if s.attributes.get("currency")]
    assert len(states) == 1, [s.entity_id for s in hass.states.async_all()]
    state = states[0]
    assert float(state.state) == 1234.56  # CLBD preferred over ITAV
    assert state.attributes["my_share"] == 617.28
    assert state.attributes["share_percent"] == 50.0
    assert state.attributes["iban"] == "ES12 •••• 9012"
    assert state.attributes["bank"] == "Ibercaja Banco"
    assert state.attributes["account"] == "Cuenta ··9012"
    assert state.attributes["unit_of_measurement"] == "EUR"

    # The JWT is signed with the uploaded key and carries the app id as kid.
    auth_header = next(h for (_, _, _, h) in aioclient_mock.mock_calls if h and "Authorization" in h)
    token = auth_header["Authorization"].split()[1]
    assert jwt.get_unverified_header(token)["kid"] == APP_ID

    # Second bank reuses the stored credentials and goes straight to the bank list.
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["step_id"] == "bank"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"bank": "Ibercaja Banco"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"

    # Options flow changes the share.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    label = next(iter(result["data_schema"].schema))
    result = await hass.config_entries.options.async_configure(result["flow_id"], {str(label): 100})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    state = hass.states.get(state.entity_id)
    assert state.attributes["my_share"] == 1234.56


async def test_expired_session_starts_reauth(hass: HomeAssistant, aioclient_mock, pem) -> None:
    _mock_api(aioclient_mock, balances_status=401)
    result = await _run_flow(hass, pem)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_rate_limit_keeps_cached_balance(hass: HomeAssistant, aioclient_mock, pem) -> None:
    _mock_api(aioclient_mock)
    result = await _run_flow(hass, pem, share=100)
    await hass.async_block_till_done()
    entry = result["result"]
    coordinator = entry.runtime_data

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/accounts/uid-1/balances", status=429,
                       text='{"error": "ASPSP_RATE_LIMIT_EXCEEDED"}')
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    states = [s for s in hass.states.async_all("sensor") if s.attributes.get("currency")]
    assert float(states[0].state) == 1234.56
    assert states[0].attributes["stale"] is True


async def test_helpers(pem) -> None:
    assert normalize_pem(pem.replace("\n", " ")) == normalize_pem(pem)
    assert parse_redirect("https://localhost/?state=s&code=c") == {"state": "s", "code": "c"}
    assert pick_balance([{"balance_type": "XPCD"}, {"balance_type": "ITBD"}])["balance_type"] == "ITBD"
    assert pick_balance([{"balance_type": "OTHR"}])["balance_type"] == "OTHR"
    assert pick_balance([]) is None


async def test_upload_pem_derives_app_id(hass: HomeAssistant, aioclient_mock, pem) -> None:
    _mock_api(aioclient_mock)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    with patch(
        "custom_components.enable_banking.config_flow._read_upload",
        return_value=(pem, f"{APP_ID}.pem"),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"pem_file": "0b1c2d3e-0000-4000-8000-000000000001"})
    assert result["step_id"] == "bank", result


async def test_missing_key_error(hass: HomeAssistant, aioclient_mock, pem) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"app_id": APP_ID})
    assert result["errors"] == {"base": "missing_key"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"private_key": "not a key", "app_id": APP_ID}
    )
    assert result["errors"] == {"base": "invalid_key"}


async def test_transactions_endpoint(
    hass: HomeAssistant, aioclient_mock, pem, hass_client, hass_owner_user
) -> None:
    _mock_api(aioclient_mock)
    result = await _run_flow(hass, pem, share=50)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    # Admins that are not the owner are refused.
    admin = await hass_client()
    resp = await admin.get("/api/enable_banking/transactions")
    assert resp.status == 403

    refresh = await hass.auth.async_create_refresh_token(hass_owner_user, "https://example.com/app")
    client = await hass_client(hass.auth.async_create_access_token(refresh))
    resp = await client.get("/api/enable_banking/transactions")
    assert resp.status == 200
    body = await resp.json()
    assert body["accounts"][0]["share_percent"] == 50.0
    assert body["accounts"][0]["entity_id"].startswith("sensor.")
    txs = body["transactions"]
    assert [t["status"] for t in txs] == ["PDNG", "BOOK", "BOOK"]
    assert txs[0]["amount"] == -12.0 and txs[0]["counterparty"] == "SPOTIFY"
    assert txs[1]["amount"] == 300.0 and txs[1]["internal"] is True
    assert txs[1]["counterparty_iban"] == "ES76 •••• 0001"
    assert txs[2]["amount"] == -54.2 and txs[2]["internal"] is False and txs[2]["mcc"] == "5411"
    assert txs[2]["id"] == "r:e1"

    resp = await client.get("/api/enable_banking/transactions?since=2026-10-21")
    assert len((await resp.json())["transactions"]) == 2
    resp = await client.get("/api/enable_banking/transactions?since=nope")
    assert resp.status == 400

    # A second refresh inside the interval does not read transactions again.
    calls = sum(1 for c in aioclient_mock.mock_calls if "/transactions" in str(c[1]))
    await result["result"].runtime_data.async_refresh()
    assert sum(1 for c in aioclient_mock.mock_calls if "/transactions" in str(c[1])) == calls


def test_merge_replaces_pending():
    from datetime import date

    from custom_components.enable_banking import transactions as t

    booked = t.normalize({"entry_reference": "a", "transaction_amount": {"amount": "5"},
                          "credit_debit_indicator": "DBIT", "booking_date": "2026-10-01"}, "EUR")
    pending = t.normalize({"transaction_amount": {"amount": "7"}, "credit_debit_indicator": "DBIT",
                           "status": "PDNG", "booking_date": "2026-10-02"}, "EUR")
    first = t.merge(None, [booked, pending], today=date(2026, 10, 3), keep_days=1100)
    assert len(first["booked"]) == 1 and len(first["pending"]) == 1
    second = t.merge(first, [], today=date(2026, 10, 3), keep_days=1100)
    assert len(second["booked"]) == 1 and second["pending"] == []
    assert t.last_booked_date(second) == date(2026, 10, 1)
    old = t.merge(first, [], today=date(2030, 1, 1), keep_days=30)
    assert old["booked"] == {}


def test_account_label_product_codes():
    from custom_components.enable_banking.util import account_label

    iban = "ES0000000000000000004321"
    assert account_label({"product": "CHECKING_ACCOUNT", "iban": iban}) == "Cuenta corriente ··4321"
    assert account_label({"product": "XYZ_123", "iban": iban}) == "Cuenta ··4321"
    assert account_label({"product": "Cuenta Online", "iban": iban}) == "Cuenta Online ··4321"
    assert account_label({"iban": iban}) == "Cuenta ··4321"
