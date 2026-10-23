"""Constants for the Enable Banking integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "enable_banking"

API_BASE: Final = "https://api.enablebanking.com"

# Config entry data keys
CONF_APP_ID: Final = "app_id"
CONF_PRIVATE_KEY: Final = "private_key"
CONF_REDIRECT_URL: Final = "redirect_url"
CONF_ASPSP_NAME: Final = "aspsp_name"
CONF_ASPSP_COUNTRY: Final = "aspsp_country"
CONF_SESSION_ID: Final = "session_id"
CONF_VALID_UNTIL: Final = "valid_until"
CONF_ACCOUNTS: Final = "accounts"

# Options keys
OPT_SHARES: Final = "shares"

DEFAULT_COUNTRY: Final = "ES"
PSU_TYPE: Final = "personal"

# PSD2 allows four unattended account reads per day, so poll every six hours.
UPDATE_INTERVAL: Final = timedelta(hours=6)
# On a Home Assistant restart, reuse cached balances younger than this instead
# of spending one of the four daily reads.
CACHE_REUSE_ON_START: Final = timedelta(hours=5, minutes=45)

MAX_CONSENT: Final = timedelta(days=180)

# Transactions are read twice a day, alongside a balance read, to stay well
# inside the daily access limit some banks apply across all account endpoints.
TRANSACTIONS_INTERVAL: Final = timedelta(hours=11, minutes=30)
# First read: most banks only give 90 days without a fresh login.
TRANSACTIONS_INITIAL_DAYS: Final = 89
# Right after a login (new connection or renewed access) PSD2 lets banks hand
# out older movements, often only for a few minutes: ask for two years then.
TRANSACTIONS_HISTORY_DAYS: Final = 730
# Later reads overlap the last booked day to catch late bookings.
TRANSACTIONS_OVERLAP_DAYS: Final = 5
# Booked transactions older than this are dropped from storage.
TRANSACTIONS_KEEP_DAYS: Final = 1100

# Booked balance first: that is the number that belongs in a net-worth total.
BALANCE_TYPE_PREFERENCE: Final = ("CLBD", "ITBD", "CLAV", "ITAV", "XPCD")

ATTR_SHARE_PERCENT: Final = "share_percent"
ATTR_MY_SHARE: Final = "my_share"
