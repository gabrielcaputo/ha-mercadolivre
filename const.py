"""Constants for the Mercado Livre integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "mercadolivre"

CONF_SSID: Final = "ssid"

# Subentries: one per Mercado Livre account (added by the user), plus two
# created by the integration to group the totals and the package devices.
SUBENTRY_ACCOUNT: Final = "account"
SUBENTRY_SUMMARY: Final = "summary"
SUBENTRY_DELIVERIES: Final = "deliveries"

BASE_URL: Final = "https://myaccount.mercadolivre.com.br"
# What the purchases page loads its lists from (first page, no filter).
LIST_URL: Final = f"{BASE_URL}/post-purchase/my-purchases/api/list"
PURCHASES_PAGE_URL: Final = f"{BASE_URL}/post-purchase/my-purchases/list"
# Public page behind the "share" button on the live map. The status page of
# a package out for delivery links to it, so no paste from the app is needed.
SHARED_TRACKING_URL: Final = f"{BASE_URL}/post-purchase/shared-tracking"

USER_AGENT: Final = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
)
NOMINATIM_URL: Final = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT: Final = (
    "ha-mercadolivre (+https://github.com/gabrielcaputo/ha-mercadolivre)"
)

CONF_USER_AGENT: Final = "user_agent"
CONF_QUIET_HOURS: Final = "quiet_hours"

# Polling is kept slow and irregular so it looks like a person checking their
# purchases, not a bot. Each interval gets a random extra of up to its jitter.
UPDATE_INTERVAL: Final = timedelta(minutes=60)
UPDATE_JITTER: Final = timedelta(minutes=15)
UPDATE_INTERVAL_OUT_FOR_DELIVERY: Final = timedelta(minutes=10)
UPDATE_JITTER_OUT_FOR_DELIVERY: Final = timedelta(minutes=3)
# No requests between these hours (local time) unless a package is out for
# delivery.
QUIET_HOURS_START: Final = 0
QUIET_HOURS_END: Final = 7
# A package's status page (the heaviest request) is re-read only when the
# purchases list shows a change, or after this long.
STATUS_REFRESH: Final = timedelta(hours=3)
# Seconds to wait between consecutive requests.
REQUEST_DELAY: Final = (2.0, 6.0)
# After a 403/429/challenge, stop for this long, doubling up to the maximum.
BACKOFF_BASE: Final = timedelta(hours=3)
BACKOFF_MAX: Final = timedelta(hours=12)

# Live driver position, read from the shared tracking link. The page itself
# refreshes every 15 s; this is no cookie and no account, so it's only kept
# reasonable, not disguised.
DRIVER_INTERVAL: Final = timedelta(seconds=30)
DRIVER_JITTER: Final = timedelta(seconds=15)
# Used as the GPS accuracy when Mercado Livre doesn't give a radius.
DRIVER_ACCURACY: Final = 30
# Stop following the driver this long after the promised window ends.
DRIVER_GRACE: Final = timedelta(hours=1)
DRIVER_STORAGE_KEY: Final = f"{DOMAIN}.shared_tracking"
# Last driver positions, so the map doesn't fall back to the distribution
# center for a moment after a restart.
DRIVER_POSITIONS_STORAGE_KEY: Final = f"{DOMAIN}.driver_positions"
DRIVER_POSITIONS_SAVE_DELAY: Final = 60

# How long a package that was delivered (or cancelled) keeps its device, so
# the final state is visible to automations before removal.
KEEP_FINAL_FOR: Final = timedelta(hours=24)
# Deliveries listed by the "delivered recently" sensor.
DELIVERED_DAYS: Final = 7

STATUS_OUT_FOR_DELIVERY: Final = "out_for_delivery"
# Shipping sub-statuses that mean the driver is on the way. "soon_deliver" is
# the last stretch ("Seu endereço é a próxima parada.").
OUT_FOR_DELIVERY_SUB_STATUSES: Final = {STATUS_OUT_FOR_DELIVERY, "soon_deliver"}
STATUS_DELIVERED: Final = "delivered"
FINAL_STATES: Final = {STATUS_DELIVERED, "cancelled"}

PACKAGE_STATES: Final = [
    "pending",
    "handling",
    "ready_to_ship",
    "shipped",
    "out_for_delivery",
    "delivered",
    "not_delivered",
    "cancelled",
    "unknown",
]

# Per-package entity unique ids are "<shipment_id>_<kind>".
KIND_STATUS: Final = "status"
KIND_ETA: Final = "eta"
KIND_TRACKING: Final = "tracking"
KIND_PLACE: Final = "place"
KIND_LOCATION: Final = "location"

STORAGE_VERSION: Final = 1
GEOCODE_STORAGE_KEY: Final = f"{DOMAIN}.geocode_cache"
# Per account: "<key>.<user id>".
HISTORY_STORAGE_KEY: Final = f"{DOMAIN}.history"
HISTORY_SAVE_DELAY: Final = 30
