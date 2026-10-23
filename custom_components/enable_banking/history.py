"""Past daily balances rebuilt from the stored transactions.

Home Assistant records a balance only from the day the integration is added,
so its history charts start empty. The transactions already read (up to 90
days back on the first read) tell how each balance got to today's value:
walking back from today's balance and undoing every booked movement gives the
closing balance of each earlier day. Those days are imported as
long-term statistics of the balance sensor, only before the integration was
added, so nothing Home Assistant measured itself is overwritten; they are
rebuilt again whenever older movements arrive. All of
it happens inside Home Assistant; nothing is sent anywhere.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


def daily_closing_balances(
    current: float,
    booked: list[dict[str, Any]],
    today: date,
) -> dict[date, float]:
    """Closing balance of every day from the day before the oldest movement to yesterday.

    `current` is today's balance and `booked` the booked movements (signed
    amounts, negative = money out) with their booking dates.
    """
    moves: dict[date, float] = defaultdict(float)
    for tx in booked:
        try:
            day = date.fromisoformat(str(tx.get("date"))[:10])
            amount = float(tx.get("amount"))
        except (TypeError, ValueError):
            continue
        if day <= today:
            moves[day] += amount
    if not moves:
        return {}

    first = min(moves) - timedelta(days=1)
    result: dict[date, float] = {}
    running = current
    day = today
    while day > first:
        running -= moves.get(day, 0.0)
        day -= timedelta(days=1)
        result[day] = round(running, 2)
    return result


def statistic_rows(
    balances: dict[date, float],
    before: datetime,
    anchor_state: float,
    anchor_sum: float,
) -> list[dict[str, Any]]:
    """One hourly row per day at local midnight, holding that day's close.

    Only days that start before `before` (the first recorded statistic) are
    kept. The sums continue backwards from that first row, so the recorded
    series stays continuous.
    """
    rows: list[dict[str, Any]] = []
    for day in sorted(balances):
        start = dt_util.as_utc(datetime.combine(day, time.min, tzinfo=dt_util.get_default_time_zone()))
        if start >= before:
            continue
        state = balances[day]
        rows.append({"start": start, "state": state, "sum": round(anchor_sum - (anchor_state - state), 2)})
    return rows


async def async_backfill(
    hass: HomeAssistant,
    entity_id: str,
    current: float,
    booked: list[dict[str, Any]],
    since: datetime | None = None,
) -> bool:
    """Import the rebuilt days for one balance sensor. True when it is done
    (imported, or nothing to import); False to try again later.

    `since` is when the integration was added: everything before it was
    rebuilt (Home Assistant could not measure it), so those days are
    rebuilt again from the movements known now, which may be more complete."""
    if "recorder" not in hass.config.components:
        return True

    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import (
        async_import_statistics,
        get_metadata,
        statistics_during_period,
    )

    instance = get_instance(hass)
    metadata = await instance.async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={entity_id})
    )
    if entity_id not in metadata:
        return False  # the sensor has not been compiled into statistics yet

    async def first_recorded_hour(start: datetime) -> dict[str, Any] | None:
        hourly = await instance.async_add_executor_job(
            lambda: statistics_during_period(
                hass, start, start + timedelta(days=32), {entity_id}, "hour", None, {"state", "sum"}
            )
        )
        hours = hourly.get(entity_id) or []
        return hours[0] if hours and hours[0].get("state") is not None else None

    first_hour = None
    if since is not None:
        first_hour = await first_recorded_hour(dt_util.as_utc(since).replace(minute=0, second=0, microsecond=0))
    if first_hour is None:
        first = await instance.async_add_executor_job(
            lambda: statistics_during_period(
                hass,
                dt_util.utc_from_timestamp(0),
                None,
                {entity_id},
                "month",
                None,
                {"state", "sum"},
            )
        )
        first_rows = first.get(entity_id) or []
        if not first_rows:
            return False
        start = first_rows[0].get("start")
        first_hour = await first_recorded_hour(
            dt_util.utc_from_timestamp(start) if isinstance(start, (int, float)) else start
        )
        if first_hour is None:
            return False
    first_start = first_hour["start"]
    before = dt_util.utc_from_timestamp(first_start) if isinstance(first_start, (int, float)) else first_start

    balances = daily_closing_balances(current, booked, dt_util.now().date())
    rows = statistic_rows(balances, before, float(first_hour["state"]), float(first_hour.get("sum") or 0))
    if not rows:
        return True

    meta = dict(metadata[entity_id][1])
    meta.pop("has_mean", None)
    async_import_statistics(hass, meta, rows)
    _LOGGER.info("Historial reconstruido para %s: %d días", entity_id, len(rows))
    return True
