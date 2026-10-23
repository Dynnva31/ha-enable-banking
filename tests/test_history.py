"""Rebuilding past balances into the recorder's long-term statistics."""

from datetime import timedelta

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.enable_banking.history import async_backfill

ENTITY = "sensor.test_bank_1234"


@pytest.fixture
def mock_recorder_before_hass(async_test_recorder):
    """Use the real recorder for these tests."""


async def test_backfill_imports_days_before_the_first_statistic(hass: HomeAssistant, recorder_mock) -> None:
    today = dt_util.now().date()
    midnight = dt_util.start_of_local_day()
    first_hour = dt_util.as_utc(midnight) - timedelta(hours=10)
    meta = {
        "source": "recorder",
        "statistic_id": ENTITY,
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "name": None,
        "unit_of_measurement": "EUR",
        "unit_class": None,
    }
    # What Home Assistant recorded itself: one hour, yesterday.
    async_import_statistics(hass, meta, [{"start": first_hour, "state": 900.0, "sum": 0.0}])
    await async_wait_recording_done(hass)

    booked = [
        {"date": (today - timedelta(days=1)).isoformat(), "amount": -50.0},
        {"date": (today - timedelta(days=3)).isoformat(), "amount": 200.0},
    ]
    assert await async_backfill(hass, ENTITY, 850.0, booked)
    await async_wait_recording_done(hass)

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.as_utc(midnight) - timedelta(days=10),
        None,
        {ENTITY},
        "hour",
        None,
        {"state", "sum"},
    )
    rows = stats[ENTITY]
    states = [r["state"] for r in rows]
    # Days before the recorded hour: close of today-4 (700), today-3 (900), today-2 (900);
    # the recorded row itself is untouched.
    assert states[:3] == [700.0, 900.0, 900.0]
    assert rows[-1]["start"] == first_hour.timestamp() and rows[-1]["state"] == 900.0
    assert [r["sum"] for r in rows[:3]] == [-200.0, 0.0, 0.0]


async def test_backfill_waits_for_the_sensor_statistics(hass: HomeAssistant, recorder_mock) -> None:
    assert not await async_backfill(hass, "sensor.not_recorded_yet", 10.0, [{"date": "2026-01-01", "amount": 1.0}])
