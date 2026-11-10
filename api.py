"""Client for the Mercado Livre purchases pages.

Mercado Livre has no buyer-facing public API, so this talks to the same
endpoints the website uses, authenticated with the ``ssid`` session cookie.
Everything here is undocumented and may change without notice; parsing is
defensive and keyed on ``ui_type`` values rather than generated brick ids.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import json
import logging
import re
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import aiohttp

from .const import (
    BASE_URL,
    LIST_URL,
    OUT_FOR_DELIVERY_SUB_STATUSES,
    PACKAGE_STATES,
    PURCHASES_PAGE_URL,
    SHARED_TRACKING_URL,
    STATUS_OUT_FOR_DELIVERY,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

TZ = ZoneInfo("America/Sao_Paulo")

_CTX_RE = re.compile(
    r'<script id="__NORDIC_RENDERING_CTX__"[^>]*>(.*?)</script>', re.S
)
# The purchases page has no "userId" anymore; every purchase names the buyer.
_USER_ID_RE = re.compile(r'"userId":(\d+)|"buyer":\{"id":(\d+)')
_SSID_RE = re.compile(r"(?:^|;\s*)ssid=([^;\s]+)")
_SHARED_TOKEN_RE = re.compile(r"shared-tracking/([A-Za-z0-9_-]{20,})")
_BARE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
# The map's refresh timer is named after the shipment: "reload-maps-48223340971".
_MAP_SHIPMENT_RE = re.compile(r"reload-maps-(\d+)")
# And so is the map itself: "google-map-48399383541".
_MAP_ID_RE = re.compile(r"^google-map-(\d+)$")
# Lists of the purchases page that hold packages still to be received are
# named after when they arrive ("arrive-today-list", "in-preparation-list").
_PAST_LIST: Final = "past-list"
_SKIPPED_LISTS: Final = {"loading-list"}
# "Comprado em 27 de out. | R$ 54,99"
_BOUGHT_RE = re.compile(r"Comprado em (\d{1,2}) de ([a-zç]{3})", re.I)
# Delivered packages: "Out 28 - 15h40", and likely "Hoje - 15h10" / "Ontem".
_DELIVERED_RE = re.compile(
    r"\b(?:([a-zç]{3})[a-zç]*\.?\s+(\d{1,2})|(hoje|ontem))\s*-\s*"
    r"(\d{1,2})(?::|h)(\d{2})?",
    re.I,
)

_MONTHS = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}  # fmt: skip
# "12 out. 13:31" or "12 de outubro às 13:31:16"
_DATE_RE = re.compile(
    r"(\d{1,2})\s+(?:de\s+)?([a-zç]{3})[a-zç]*\.?\s+(?:às\s+)?"
    r"(\d{1,2}):(\d{2})(?::(\d{2}))?",
    re.I,
)
# "Saiu do centro de distribuição de Palmares, Simões Filho e está em trânsito."
_PLACE_RE = re.compile(
    r"(?:centro de distribuição|agência|unidade)\s+(?:de|do|da|em)\s+"
    r"(.+?)(?:\s+e\s+está\b.*)?\.?$",
    re.I,
)


class MercadoLivreError(Exception):
    """Base error."""


class AuthError(MercadoLivreError):
    """The session cookie is missing, invalid or expired."""


class ParseError(MercadoLivreError):
    """A page did not have the expected structure."""


class BlockedError(MercadoLivreError):
    """Mercado Livre is rate limiting or challenging the requests."""


class TrackingEndedError(MercadoLivreError):
    """The shared tracking link no longer works."""


@dataclass
class TrackingEvent:
    """One line of the shipment history."""

    step: str
    label: str
    date_text: str
    date: datetime | None
    place: str | None
    latitude: float | None = None
    longitude: float | None = None


@dataclass
class Package:
    """A shipment the user is waiting for."""

    # Empty until known: the purchases list names a shipment only when it
    # shows its map; otherwise it comes from the status page.
    shipment_id: str
    account: str | None = None
    order_id: str | None = None
    pack_id: str | None = None
    item_title: str | None = None
    item_info: str | None = None
    # Every product in the pack, as {"title", "image"} (first one = item_title).
    products: list[dict[str, str | None]] = field(default_factory=list)
    image: str | None = None
    status_url: str | None = None
    seller: str | None = None
    intro: str | None = None
    headline: str | None = None
    description: str | None = None
    raw_status: str | None = None
    sub_status: str | None = None
    logistic_type: str | None = None
    steps: list[dict[str, str]] = field(default_factory=list)
    events: list[TrackingEvent] = field(default_factory=list)
    # What the purchases list said; the status page is re-read when it changes.
    list_signature: tuple[str | None, ...] = ()
    # Listed under "Histórico de compras" rather than as still to be received.
    past: bool = False
    bought_on: date | None = None
    # Token of the public live map, from the status page while out for
    # delivery. A bearer link to the user's address: never put in a state.
    share_token: str | None = None
    # Filled by the coordinator.
    eta_start: date | None = None
    eta_end: date | None = None
    eta_window_start: datetime | None = None
    eta_window_end: datetime | None = None
    delivered_on: date | None = None
    latitude: float | None = None
    longitude: float | None = None
    final_since: datetime | None = None
    status_fetched_at: datetime | None = None

    @property
    def status(self) -> str:
        """Normalized status, one of PACKAGE_STATES."""
        if self.sub_status in OUT_FOR_DELIVERY_SUB_STATUSES:
            return STATUS_OUT_FOR_DELIVERY
        if self.raw_status in PACKAGE_STATES:
            return self.raw_status
        return "unknown"

    @property
    def last_event(self) -> TrackingEvent | None:
        """Most recent history entry."""
        return self.events[-1] if self.events else None

    @property
    def location(self) -> str | None:
        """Last known place (distribution center city) from the history."""
        for event in reversed(self.events):
            if event.place:
                return event.place
        return None


@dataclass
class DriverPosition:
    """What one read of the shared tracking map gave."""

    shipment_id: str | None
    latitude: float | None = None
    longitude: float | None = None
    # Meters; None when Mercado Livre shows an exact position.
    accuracy: float | None = None
    low_accuracy: bool = False
    # Where the page would ask next; None once it stops refreshing.
    next_path: str | None = None

    @property
    def has_position(self) -> bool:
        """Whether the driver marker was on the map."""
        return self.latitude is not None and self.longitude is not None


def extract_shared_token(value: str) -> str:
    """Accept a shared tracking link or the bare token at its end."""
    value = value.strip()
    if match := _SHARED_TOKEN_RE.search(value):
        return match.group(1)
    if _BARE_TOKEN_RE.match(value):
        return value
    raise ParseError("Not a Mercado Livre shared tracking link")


def _is_driver_marker(marker: dict[str, Any]) -> bool:
    # "marker-driver" on the shared page, "delivery-marker-driver-<shipment>"
    # on the purchases pages.
    marker_id = str(marker.get("id", ""))
    return marker_id == "marker-driver" or marker_id.startswith("delivery-marker-driver")


def parse_shared_map(payload: Any) -> DriverPosition:
    """Parse the JSON the shared tracking page polls for its map."""
    result = DriverPosition(shipment_id=None)
    for brick in _bricks(payload, "google-map"):
        data = brick.get("data", {})
        for marker in data.get("markers", []):
            if not _is_driver_marker(marker):
                continue
            position = marker.get("position") or {}
            try:
                result.latitude = float(position["lat"])
                result.longitude = float(position["lng"])
            except (KeyError, TypeError, ValueError):
                continue
            result.low_accuracy = bool(marker.get("lowAccuracy"))
            if (radius := marker.get("accuracyRadius")) is not None:
                result.accuracy = float(radius)
        action = data.get("actions", {}).get("visible", {})
        if match := _MAP_SHIPMENT_RE.search(str(action.get("intervalId", ""))):
            result.shipment_id = match.group(1)
        request_url = action.get("action", {}).get("requestUrl")
        if isinstance(request_url, str) and request_url.startswith("/"):
            result.next_path = request_url
        break
    return result


def extract_ssid(value: str) -> str:
    """Accept either a bare ssid value or a full Cookie header."""
    value = value.strip()
    if match := _SSID_RE.search(value):
        return match.group(1)
    if "=" in value or ";" in value:
        raise AuthError("No ssid cookie found in the given text")
    return value


def _walk(obj: Any) -> Iterator[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def _ui_type(brick: dict[str, Any]) -> Any:
    return brick.get("uiType") or brick.get("ui_type")


def _bricks(obj: Any, ui_type: str) -> Iterator[dict[str, Any]]:
    return (d for d in _walk(obj) if _ui_type(d) == ui_type)


def _text(brick: dict[str, Any]) -> str:
    text = brick.get("data", {}).get("text")
    return text.strip() if isinstance(text, str) else ""


def _page_config(state: Any) -> Any:
    """The page's own bricks.

    The rendering state repeats them (``floxResponse``, the preloaded brick
    stack); walking only one copy keeps lookups unambiguous.
    """
    try:
        return state["appProps"]["pageProps"]["children"]["props"]["middleendData"][
            "pageConfig"
        ]
    except (KeyError, TypeError):
        return state


def _click_url(brick: dict[str, Any]) -> str | None:
    action = brick.get("data", {}).get("actions", {}).get("click", {})
    url = action.get("redirectUrl") if isinstance(action, dict) else None
    return url if isinstance(url, str) else None


def _share_token(map_brick: dict[str, Any]) -> str | None:
    """The shared tracking token behind a purchases page map.

    The map refreshes from an URL that carries the page's "share" link,
    percent-encoded, as ``shareTrackingUrl``.
    """
    action = map_brick.get("data", {}).get("actions", {}).get("visible", {})
    request_url = action.get("action", {}).get("requestUrl")
    if not isinstance(request_url, str):
        return None
    for url in parse_qs(urlsplit(request_url).query).get("shareTrackingUrl", []):
        try:
            return extract_shared_token(url)
        except ParseError:
            continue
    return None


def _shipping_info(obj: Any, shipment_id: str) -> dict[str, Any] | None:
    """Find the melidata shipping entry for a shipment.

    With no shipment id yet, the first one found: a pack's status page is
    about a single shipment.
    """
    for node in _walk(obj):
        shipping = node.get("shipping")
        if not isinstance(shipping, list):
            continue
        for entry in shipping:
            if (
                isinstance(entry, dict)
                and entry.get("shipping_id")
                and (not shipment_id or str(entry["shipping_id"]) == shipment_id)
            ):
                return entry
    return None


def parse_date(text: str, now: datetime | None = None) -> datetime | None:
    """Parse "12 out. 13:31" style dates, inferring the year."""
    match = _DATE_RE.search(text)
    if not match:
        return None
    day, month_name, hour, minute, second = match.groups()
    month = _MONTHS.get(month_name.lower()[:3])
    if month is None:
        return None
    now = now or datetime.now(TZ)
    try:
        date = datetime(
            now.year, month, int(day), int(hour), int(minute), int(second or 0),
            tzinfo=TZ,
        )
    except ValueError:
        return None
    # Dates have no year; one far in the future belongs to last year.
    if date - now > timedelta(days=60):
        date = date.replace(year=now.year - 1)
    return date


_WEEKDAYS = {
    "segunda": 0, "terça": 1, "terca": 1, "quarta": 2, "quinta": 3,
    "sexta": 4, "sábado": 5, "sabado": 5, "domingo": 6,
}  # fmt: skip
_ETA_TOKEN_RE = re.compile(
    r"\b(hoje|amanhã|amanha"
    r"|segunda|terça|terca|quarta|quinta|sexta|sábado|sabado|domingo"
    r"|\d{1,2}(?:\s+de\s+[a-zç]{3,}|/\d{1,2})?)\b",
    re.I,
)
# "10:30", "10h30" or "19h", as in "Chega hoje entre as 10:30 e 12:15 h" and
# "Chegará hoje entre 16h e 21h".
_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2})|h(\d{2})?)\b", re.I)


def parse_eta(text: str, today: date) -> tuple[date, date] | None:
    """Turn "Chega amanhã" / "Chega entre 14 e 16 de outubro" into dates.

    Mercado Livre only shows relative or partial dates, so this has to run
    against the day the text was fetched. Returns (earliest, latest).
    """
    # Times of day would otherwise be read as days of the month.
    text, times = _TIME_RE.subn(" ", text)
    if match := re.search(r"em\s+(\d+)\s+dias?\b", text, re.I):
        target = today + timedelta(days=int(match.group(1)))
        return target, target
    found: list[date] = []
    tokens = [m.group(1).lower() for m in _ETA_TOKEN_RE.finditer(text)]
    # In "entre 14 e 16 de outubro" the month only follows the last day.
    months = [
        _MONTHS.get(m.group(1)[:3].lower())
        for m in re.finditer(r"\d{1,2}\s+de\s+([a-zç]{3,})", text, re.I)
    ]
    fallback_month = next((m for m in reversed(months) if m), None)
    for token in tokens:
        if token == "hoje":
            found.append(today)
        elif token in ("amanhã", "amanha"):
            found.append(today + timedelta(days=1))
        elif token in _WEEKDAYS:
            ahead = (_WEEKDAYS[token] - today.weekday()) % 7 or 7
            found.append(today + timedelta(days=ahead))
        else:
            day_s, _, rest = token.partition(" ")
            month: int | None
            if "/" in day_s:
                day_s, month_s = day_s.split("/")
                month = int(month_s)
            elif rest:
                month = _MONTHS.get(rest.split()[-1][:3])
            else:
                month = fallback_month
            day = int(day_s)
            if month is None:
                # "dia 15": the next 15th.
                month = today.month if day >= today.day else today.month % 12 + 1
            year = today.year
            # Far in the past means next year ("2 de janeiro" seen in December).
            if (month, day) < (today.month, today.day) and today.month - month > 6:
                year += 1
            try:
                found.append(date(year, month, day))
            except ValueError:
                continue
    if not found:
        # Only a time, as in "Chegará antes das 11h25" (the last stretch).
        return (today, today) if times else None
    return min(found), max(found)


def parse_eta_window(
    text: str, day: date
) -> tuple[datetime | None, datetime | None]:
    """Turn "entre as 10:30 e 12:15 h" on ``day`` into (start, end).

    Shown once the package is out for delivery. "até as 12:00" and "antes
    das 11h25" have no start, "a partir das 14:00" no end, and a single
    "às 11:40" is both.
    """
    times: list[datetime] = []
    for hour, minute, h_minute in _TIME_RE.findall(text):
        try:
            times.append(
                datetime(
                    day.year, day.month, day.day,
                    int(hour), int(minute or h_minute or 0), tzinfo=TZ,
                )  # fmt: skip
            )
        except ValueError:
            continue
    if not times:
        return None, None
    if len(times) > 1:
        return times[0], times[-1]
    if re.search(r"\b(?:até|antes)\b", text, re.I):
        return None, times[0]
    if re.search(r"\b(?:a partir|depois)\b", text, re.I):
        return times[0], None
    return times[0], times[0]


def _past_date(day: int, month_name: str, today: date) -> date | None:
    """A day/month that already happened, in this year or the last."""
    month = _MONTHS.get(month_name.lower()[:3])
    if month is None:
        return None
    try:
        result = date(today.year, month, day)
    except ValueError:
        return None
    if result > today:
        result = result.replace(year=today.year - 1)
    return result


def parse_delivered(text: str, today: date) -> datetime | None:
    """When a package was delivered, from "Out 28 - 15h40"."""
    match = _DELIVERED_RE.search(text)
    if not match:
        return None
    month_name, day, relative, hour, minute = match.groups()
    if relative:
        day_date: date | None = today - timedelta(
            days=0 if relative.lower() == "hoje" else 1
        )
    else:
        day_date = _past_date(int(day), month_name, today)
    if day_date is None:
        return None
    try:
        return datetime(
            day_date.year, day_date.month, day_date.day,
            int(hour), int(minute or 0), tzinfo=TZ,
        )  # fmt: skip
    except ValueError:
        return None


def parse_place(label: str) -> str | None:
    """Pull the city out of a history line, if it names one."""
    match = _PLACE_RE.search(label)
    if not match:
        return None
    place = match.group(1).strip()
    if re.sub(r"\s", "", place).lower() == "mercadolivre":
        return None
    return place


def parse_list(payload: Any, today: date | None = None) -> list[Package]:
    """Parse the purchases lists into packages.

    The lists only give each purchase's pack, status link, image and two
    lines of text ("A caminho" / "Chegará hoje entre 16h e 21h"); status and
    shipment come from the status page, except the shipment of a package
    shown with its map. Packages are keyed by ``pack_id`` until then.
    """
    if isinstance(payload, dict) and payload.get("type") == "server_redirect":
        raise ParseError(
            f"Purchases list moved to {payload.get('data', {}).get('url')}"
        )
    today = today or datetime.now(TZ).date()
    lists: dict[str, dict[str, Any]] = {}
    for brick in _bricks(_page_config(payload), "list"):
        list_id = str(brick.get("id", ""))
        if list_id.endswith("-list") and list_id not in _SKIPPED_LISTS:
            lists.setdefault(list_id, brick)
    if not lists:
        # Never report "no packages" because the page changed.
        raise ParseError("No purchase lists found")

    packages: list[Package] = []
    seen: set[str] = set()
    for list_id, brick in lists.items():
        past = list_id == _PAST_LIST
        for item in brick.get("bricks", []):
            package = _parse_list_item(item, past, today)
            if package is not None and package.pack_id not in seen:
                seen.add(package.pack_id)
                packages.append(package)
    return packages


def _parse_list_item(item: dict[str, Any], past: bool, today: date) -> Package | None:
    status_url: str | None = None
    tracking: dict[str, Any] = {}
    for node in _walk(item):
        url = _click_url(node)
        if url and "my-purchases/" in url and "/status" in url:
            status_url = url
            tracking = (
                node["data"]["actions"]["click"].get("tracking", {}).get("data", {})
            )
            break
    if status_url is None:
        return None
    query = parse_qs(urlsplit(status_url).query)
    pack_id = str(tracking.get("pack_id") or next(iter(query.get("packId", [])), ""))
    if not pack_id:
        return None

    shipment_id = ""
    for map_brick in _bricks(item, "google-map"):
        if match := _MAP_ID_RE.match(str(map_brick.get("id", ""))):
            shipment_id = match.group(1)
    texts = [text for p in _bricks(item, "paragraph") if (text := _text(p))]
    image = next(
        (
            path
            for img in _bricks(item, "image")
            if isinstance(path := img.get("data", {}).get("path"), str)
        ),
        None,
    )
    package = Package(
        shipment_id=shipment_id,
        order_id=str(tracking.get("purchase_id") or "") or None,
        pack_id=pack_id,
        image=image,
        status_url=status_url,
        past=past,
    )
    if past:
        # "Óleo Lubrificante Motul 3000+ 20w50 Motos" / "Comprado em 27 de out."
        package.item_title = texts[0] if texts else None
        for text in texts:
            if match := _BOUGHT_RE.search(text):
                package.bought_on = _past_date(int(match.group(1)), match.group(2), today)
        package.list_signature = (_PAST_LIST,)
    else:
        # "A caminho" / "Chegará hoje antes das 19h"
        package.intro = texts[0] if texts else None
        package.headline = texts[1] if len(texts) > 1 else None
        package.list_signature = (package.intro, package.headline, shipment_id)
    return package


def parse_status_page(html: str) -> dict[str, Any]:
    """Extract the server-rendered state from a purchase status page."""
    match = _CTX_RE.search(html)
    if not match:
        raise ParseError("Rendering context not found in status page")
    body = match.group(1)
    start = body.find("_n.ctx.r=")
    if start < 0:
        raise ParseError("Rendering state not found in status page")
    try:
        state, _ = json.JSONDecoder().raw_decode(body, start + len("_n.ctx.r="))
    except ValueError as err:
        raise ParseError("Invalid rendering state in status page") from err
    return state


def apply_status(package: Package, state: dict[str, Any]) -> None:
    """Update a package with what the status page shows."""
    page = _page_config(state)
    # The page has a short tracker (no events) and a "Detalhe do envio"
    # sidebar with the full history; keep whichever lists the most events.
    best: dict[str, Any] | None = None
    best_count = -1
    for brick in _bricks(page, "tracking-steps"):
        steps = brick.get("data", {}).get("steps") or []
        count = sum(len(step.get("events") or []) for step in steps)
        if steps and count > best_count:
            best, best_count = brick, count

    if best is not None:
        now = datetime.now(TZ)
        data = best["data"]
        # 1-based; the last step is current once delivered.
        current = int(data.get("currentStep") or 0)
        package.steps = []
        package.events = []
        for index, step in enumerate(data["steps"], 1):
            step_title = str(step.get("title") or "")
            if index < current or (index == current == len(data["steps"])):
                step_state = "completed"
            elif index == current:
                step_state = "active"
            else:
                step_state = "pending"
            package.steps.append({"title": step_title, "state": step_state})
            for event in step.get("events") or []:
                label = str(event.get("description") or "").strip()
                date_text = str(event.get("dateTime") or "").strip()
                package.events.append(
                    TrackingEvent(
                        step=step_title,
                        label=label,
                        date_text=date_text,
                        date=parse_date(date_text, now),
                        place=parse_place(label),
                    )
                )

    _apply_status_header(package, page)

    if info := _shipping_info(page, package.shipment_id):
        package.shipment_id = package.shipment_id or str(info["shipping_id"])
        package.raw_status = info.get("shipping_status") or package.raw_status
        package.sub_status = info.get("shipping_sub_status") or None
        package.logistic_type = info.get("logistic_type") or package.logistic_type

    package.share_token = None
    for map_brick in _bricks(page, "google-map"):
        match = _MAP_ID_RE.match(str(map_brick.get("id", "")))
        if match and package.shipment_id and match.group(1) != package.shipment_id:
            continue
        if any(_is_driver_marker(m) for m in map_brick.get("data", {}).get("markers", [])):
            package.share_token = _share_token(map_brick)
            break


def _apply_status_header(package: Package, page: Any) -> None:
    """Read the texts and products at the top of the status page.

    It reads "No prazo" (or "Entregue"), then the arrival ("Chegará hoje
    antes das 19h", "Out 28 - 15h40"), a title ("Estamos fazendo entregas na
    sua região"; the product once delivered) and sometimes a description.
    """
    header = next(
        (
            b
            for b in _walk(page)
            if str(b.get("id", "")).startswith("purchase-status_")
        ),
        None,
    )
    if header is None:
        return
    label = headline = title = description = None
    for brick in _walk(header):
        brick_id = str(brick.get("id", ""))
        ui_type = _ui_type(brick)
        if ui_type == "wrapper" and label is None and any(
            _ui_type(b) == "dot" for b in brick.get("bricks", [])
        ):
            label = next(
                (t for b in _bricks(brick, "text-fragment") if (t := _text(b))), None
            )
        elif ui_type == "paragraph" and brick_id.startswith(
            "purchase-status-description"
        ):
            description = _text(brick) or None
        elif ui_type == "paragraph" and headline is None and title is None:
            headline = _text(brick) or None
        elif ui_type == "typography" and title is None:
            title = _text(brick) or None

    # Products of this pack: one thumbnail each, the name in its alt text.
    products = [
        {"title": alt.strip(), "image": data.get("path") if isinstance(data.get("path"), str) else None}
        for img in _bricks(header, "image")
        if isinstance(alt := (data := img.get("data", {})).get("alt"), str) and alt.strip()
    ]
    if products:
        package.products = products
        package.item_title = products[0]["title"]
        extra = len(products) - 1
        package.item_info = (
            f"+{extra} produto{'s' if extra > 1 else ''}" if extra else None
        )
        if path := next((p["image"] for p in products if p["image"]), None):
            package.image = path
    if title and title == package.item_title:
        title = None
    package.intro = label or package.intro
    package.headline = headline or package.headline
    package.description = " ".join(t for t in (title, description) if t) or None


class MercadoLivreClient:
    """Fetches purchases using the website's session cookie."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        ssid: str,
        user_agent: str | None = None,
    ) -> None:
        """Initialize."""
        self._session = session
        self._ssid = ssid
        self._user_agent = user_agent or USER_AGENT

    async def _get(self, url: str, accept: str, **params: str) -> str:
        headers = {
            "Cookie": f"ssid={self._ssid}",
            "User-Agent": self._user_agent,
            "Accept": accept,
            "Accept-Language": "pt-BR,pt;q=0.9",
        }
        try:
            async with self._session.get(
                url,
                params=params or None,
                headers=headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location", "")
                    if "login" in location or "lgz" in location:
                        raise AuthError("Session expired")
                    # Anything else here is most likely a bot challenge.
                    raise BlockedError(f"Redirected to {location}")
                if resp.status == 401:
                    raise AuthError("HTTP 401")
                if resp.status in (403, 429):
                    raise BlockedError(f"HTTP {resp.status}")
                resp.raise_for_status()
                return await resp.text()
        except aiohttp.ClientError as err:
            raise MercadoLivreError(str(err)) from err
        except TimeoutError as err:
            raise MercadoLivreError("Timeout") from err

    async def async_get_user_id(self) -> str:
        """Return the logged in user's id (also validates the session)."""
        html = await self._get(PURCHASES_PAGE_URL, "text/html")
        if match := _USER_ID_RE.search(html):
            return match.group(1) or match.group(2)
        raise ParseError("User id not found")

    async def async_get_packages(self) -> list[Package]:
        """Return the packages to be received plus the recent purchases.

        One request: the first page lists every package still to be received
        ("Chegam hoje", "Em preparação"...) above the purchase history.
        """
        text = await self._get(LIST_URL, "application/json")
        try:
            payload = json.loads(text)
        except ValueError as err:
            raise ParseError("Purchases list is not JSON") from err
        return parse_list(payload)

    async def async_update_status(self, package: Package) -> None:
        """Fill history and status from the package's status page."""
        if not package.status_url:
            raise ParseError(f"No status URL for shipment {package.shipment_id}")
        html = await self._get(package.status_url, "text/html")
        apply_status(package, parse_status_page(html))


class SharedTrackingClient:
    """Reads the public shared tracking map; needs no session cookie."""

    def __init__(
        self, session: aiohttp.ClientSession, token: str, user_agent: str | None = None
    ) -> None:
        """Initialize."""
        self._session = session
        self.token = token
        self._user_agent = user_agent or USER_AGENT
        self._path: str | None = None

    async def async_get_position(self) -> DriverPosition:
        """Read the map once, following the page's own refresh URL."""
        url = (
            f"{BASE_URL}{self._path}"
            if self._path
            else f"{SHARED_TRACKING_URL}/{self.token}/map?count=1&site.id=MLB"
        )
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9",
        }
        try:
            async with self._session.get(
                url,
                headers=headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status in (403, 429) or 300 <= resp.status < 400:
                    raise BlockedError(f"HTTP {resp.status}")
                if 400 <= resp.status < 500:
                    raise TrackingEndedError(f"HTTP {resp.status}")
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise MercadoLivreError(str(err)) from err
        except TimeoutError as err:
            raise MercadoLivreError("Timeout") from err
        except ValueError as err:
            raise ParseError("Shared tracking map is not JSON") from err
        position = parse_shared_map(payload)
        if position.next_path and self.token in position.next_path:
            self._path = position.next_path
        return position
