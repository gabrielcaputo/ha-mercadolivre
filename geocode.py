"""Turn distribution center names into coordinates (Nominatim, cached)."""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import (
    GEOCODE_STORAGE_KEY,
    NOMINATIM_URL,
    NOMINATIM_USER_AGENT,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Nominatim usage policy: at most one request per second.
_MIN_INTERVAL = 1.1


class Geocoder:
    """Cached place name -> (lat, lon) lookup."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize."""
        self._session = async_get_clientsession(hass)
        self._store: Store[dict[str, list[float] | None]] = Store(
            hass, STORAGE_VERSION, GEOCODE_STORAGE_KEY
        )
        self._cache: dict[str, list[float] | None] | None = None
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def async_lookup(self, place: str) -> tuple[float, float] | None:
        """Return coordinates for a place in Brazil, or None."""
        async with self._lock:
            if self._cache is None:
                self._cache = await self._store.async_load() or {}
            key = place.casefold()
            if key not in self._cache:
                try:
                    self._cache[key] = await self._resolve(place)
                except (aiohttp.ClientError, TimeoutError) as err:
                    # Don't remember network failures; retry next update.
                    _LOGGER.debug("Geocoding %s failed: %s", place, err)
                    return None
                self._store.async_delay_save(lambda: self._cache or {}, 10)
            coords = self._cache[key]
            return (coords[0], coords[1]) if coords else None

    async def _resolve(self, place: str) -> list[float] | None:
        # "Palmares, Simões Filho" is a neighborhood plus a city; try the
        # full name first, then each part from the broadest.
        parts = [p.strip() for p in place.split(",") if p.strip()]
        candidates = [place, *reversed(parts)] if len(parts) > 1 else [place]
        for query in candidates:
            if coords := await self._query(query):
                return coords
        _LOGGER.debug("No coordinates found for %s", place)
        return None

    async def _query(self, query: str) -> list[float] | None:
        wait = self._last_request + _MIN_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request = time.monotonic()
        async with self._session.get(
            NOMINATIM_URL,
            params={
                "q": query,
                "countrycodes": "br",
                "format": "jsonv2",
                "limit": "1",
            },
            headers={"User-Agent": NOMINATIM_USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            resp.raise_for_status()
            results = await resp.json()
        if not results:
            return None
        return [float(results[0]["lat"]), float(results[0]["lon"])]
