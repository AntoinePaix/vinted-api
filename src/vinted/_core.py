"""I/O-free logic shared by the sync and async clients.

Each endpoint is described by a `Call`: the HTTP request to send, and the function that turns
the response into a result. The clients only differ in how they send requests.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Generic
from typing import TypeVar
from typing import Unpack
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlsplit

from curl_cffi.requests import Response

from . import _parsing
from .errors import BlockedError
from .errors import NotFoundError
from .errors import RateLimitError
from .errors import VintedError
from .filters import Order
from .filters import SearchFilters
from .models import Brand
from .models import Color
from .models import Feedback
from .models import Item
from .models import ItemDetails
from .models import Page
from .models import Pagination
from .models import User

T = TypeVar("T")
S = TypeVar("S")

# Statuses for which another proxy may succeed.
FAILOVER_STATUSES = frozenset({403, 429})

# Statuses worth retrying later on the same connection.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

_JSON_HEADERS = {"Accept": "application/json, text/plain, */*"}


# -- calls ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Call(Generic[T]):
    """A request to send, and how to parse its response."""

    path: str
    parse: Callable[[Response], T]
    params: Any = None
    json_api: bool = False
    authenticate: bool = True
    method: str = "GET"

    @property
    def headers(self) -> dict[str, str] | None:
        return _JSON_HEADERS if self.json_api else None


def check_response(response: Response, method: str, path: str) -> None:
    """Raise the matching `VintedError` for HTTP error statuses."""

    if response.status_code < 400:
        return
    payload: Any = None
    message = f"HTTP {response.status_code} on {method} {path}"
    if "json" in response.headers.get("content-type", ""):
        try:
            payload = response.json()
        except ValueError:
            pass
        if isinstance(payload, dict) and payload.get("message"):
            message += f": {payload['message']}"
    try:
        retry_after = float(response.headers.get("retry-after") or "")
    except ValueError:
        retry_after = None
    error = {403: BlockedError, 404: NotFoundError, 429: RateLimitError}.get(response.status_code, VintedError)
    raise error(message, response.status_code, payload, retry_after)


def _api(path: str, parse: Callable[[dict], T], **params: Any) -> Call[T]:
    params = {key: value for key, value in params.items() if value is not None}
    return Call(
        path=f"/api/v2{path}",
        parse=lambda response: parse(response.json()),
        params=params or None,
        json_api=True,
    )


def _page(parse_item: Callable[[dict], S], key: str) -> Callable[[dict], Page[S]]:
    return lambda data: Page(
        [parse_item(raw) for raw in data[key]],
        Pagination.from_api(data["pagination"]),
    )


def _parse_search(base_url: str) -> Callable[[Response], Page[Item]]:
    def parse(response: Response) -> Page[Item]:
        payload = _parsing.flight_payload(response.text)
        try:
            block = _parsing.find_json(payload, '"items":{"items":', '"items":')
        except KeyError as exc:
            raise VintedError("Search results not found in the page; Vinted's layout may have changed.") from exc
        items = [Item.from_catalog(raw, base_url) for raw in block["items"] if "productItem" in raw]
        return Page(items, Pagination.from_api(block.get("pagination") or {}))

    return parse


def _parse_item(item_id: int) -> Callable[[Response], ItemDetails]:
    def parse(response: Response) -> ItemDetails:
        ld = _parsing.json_ld(response.text, "Product")
        if ld is None:
            raise NotFoundError(f"Item {item_id} not found or no longer available.", 404)
        plugins = _parsing.plugins(_parsing.flight_payload(response.text))
        return ItemDetails.from_page(int(item_id), ld, plugins)

    return parse


def home_call() -> Call[None]:
    """Visiting the home page hands out the anonymous `access_token_web` cookie the API requires."""

    return Call(path="/", parse=lambda response: None, authenticate=False)


# -- proxies --------------------------------------------------------------------------------


def normalize_proxies(proxies: str | Sequence[str | None] | None) -> list[str | None]:
    """One entry per session to create: `[None]` means a direct connection."""

    if proxies is None:
        return [None]
    if isinstance(proxies, str):
        return [proxies]
    proxies = list(proxies)
    if not proxies:
        raise ValueError("proxies must not be empty; pass None for a direct connection")
    return proxies


def redact_proxy(proxy: str | None) -> str:
    """Proxy URL without its credentials, safe to show in errors and logs."""

    if proxy is None:
        return "direct connection"
    parts = urlsplit(proxy)
    host = parts.hostname or proxy
    return f"proxy {parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")


@dataclass
class Slot(Generic[S]):
    """A session bound to one proxy (or to none), with its own cookies."""

    session: S
    proxy: str | None
    has_cookies: bool = False
    # Incremented each time cookies are renewed, so concurrent 401s renew them only once.
    generation: int = 0
    lock: Any = field(default=None, repr=False)


class ProxyPool(Generic[S]):
    """Choose which session sends the next request.

    By default the same session is used until it fails (failover); with `rotate=True`,
    sessions are used in turn (round-robin).
    """

    def __init__(self, slots: list[Slot[S]], rotate: bool):
        self.slots = slots
        self.rotate = rotate
        self._index = 0

    def __len__(self) -> int:
        return len(self.slots)

    def pick(self) -> Slot[S]:
        slot = self.slots[self._index % len(self.slots)]
        if self.rotate:
            self._index += 1
        return slot

    def failed(self, slot: Slot[S]) -> None:
        """Move on from a session that failed (no-op if another request already did)."""

        if not self.rotate and self.slots[self._index % len(self.slots)] is slot:
            self._index += 1


# -- shared client logic --------------------------------------------------------------------


class BaseVinted:
    """Configuration and I/O-free helpers common to `Vinted` and `AsyncVinted`."""

    def __init__(
        self,
        domain: str = "vinted.fr",
        *,
        proxies: str | Sequence[str | None] | None = None,
        rotate_proxies: bool = False,
        retries: int = 4,
        backoff: float = 5.0,
        impersonate: str = "chrome",
        timeout: float = 30,
    ):
        if retries < 0 or backoff < 0:
            raise ValueError("retries and backoff must be positive")
        self.base_url = f"https://www.{domain.removeprefix('https://').removeprefix('www.').rstrip('/')}"
        self.impersonate = impersonate
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._cooldown_until = 0.0
        self._proxies = normalize_proxies(proxies)
        self._rotate_proxies = rotate_proxies

    @property
    def proxies(self) -> list[str]:
        return [proxy for proxy in self._proxies if proxy is not None]

    def _session_kwargs(self, proxy: str | None) -> dict[str, Any]:
        return {"impersonate": self.impersonate, "proxy": proxy}

    def _start_cooldown(self, delay: float) -> None:
        """Pause every request of this client for `delay` seconds (rate limits apply to the IP)."""

        self._cooldown_until = max(self._cooldown_until, time.monotonic() + delay)

    def _cooldown_remaining(self) -> float:
        return self._cooldown_until - time.monotonic()

    def _retry_delay(self, error: VintedError, retry: int) -> float | None:
        """Seconds to wait before retry number `retry + 1`, or None to give up."""

        if retry >= self.retries or error.status_code not in RETRY_STATUSES:
            return None
        return error.retry_after if error.retry_after is not None else self.backoff * 2**retry

    # -- search parameters ------------------------------------------------------------------

    @staticmethod
    def build_search_params(
        query: str | None = None,
        *,
        catalog_ids: Iterable[int] = (),
        brand_ids: Iterable[int] = (),
        size_ids: Iterable[int] = (),
        status_ids: Iterable[int] = (),
        color_ids: Iterable[int] = (),
        material_ids: Iterable[int] = (),
        price_from: float | None = None,
        price_to: float | None = None,
        currency: str | None = None,
        order: Order | str | None = None,
        page: int = 1,
        extra: dict[str, Any] | None = None,
    ) -> list[tuple[str, str]]:
        """Translate keyword filters into Vinted catalog URL parameters."""

        order = Order.parse(order)
        params: list[tuple[str, str]] = []
        if query:
            params.append(("search_text", query))
        for name, values in (
            ("catalog[]", catalog_ids),
            ("brand_ids[]", brand_ids),
            ("size_ids[]", size_ids),
            ("status_ids[]", status_ids),
            ("color_ids[]", color_ids),
            ("material_ids[]", material_ids),
        ):
            params.extend((name, str(value)) for value in values)
        for name, value in (
            ("price_from", price_from),
            ("price_to", price_to),
            ("currency", currency),
            ("order", order),
        ):
            if value is not None:
                params.append((name, str(value)))
        for name, value in (extra or {}).items():
            values = value if isinstance(value, (list, tuple)) else [value]
            params.extend((name, str(v)) for v in values)
        if page > 1:
            params.append(("page", str(page)))
        return params

    @staticmethod
    def params_from_url(url: str, page: int | None = None) -> list[tuple[str, str]]:
        """Search parameters of a catalog URL copied from the browser (optionally for another page)."""

        params = [(key, value) for key, value in parse_qsl(urlsplit(url).query) if key != "page" or page is None]
        if page is not None and page > 1:
            params.append(("page", str(page)))
        return params

    def search_page_url(self, query: str | None = None, **filters: Unpack[SearchFilters]) -> str:
        """The Vinted URL corresponding to a search, handy to open it in a browser."""

        return f"{self.base_url}/catalog?{urlencode(self.build_search_params(query, **filters))}"

    # -- endpoints --------------------------------------------------------------------------

    def _search_call(self, params: list[tuple[str, str]]) -> Call[Page[Item]]:
        return Call(path="/catalog", params=params, parse=_parse_search(self.base_url))

    @staticmethod
    def _item_call(item_id: int) -> Call[ItemDetails]:
        return Call(path=f"/items/{item_id}", parse=_parse_item(item_id))

    @staticmethod
    def _user_call(user_id: int) -> Call[User]:
        return _api(f"/users/{user_id}", lambda data: User.from_api(data["user"]))

    @staticmethod
    def _user_items_call(user_id: int, page: int, per_page: int, order: Order | str | None) -> Call[Page[Item]]:
        order = Order.parse(order)
        return _api(
            f"/wardrobe/{user_id}/items",
            _page(Item.from_api, "items"),
            page=page,
            per_page=per_page,
            order=order.value if order else None,
        )

    @staticmethod
    def _user_feedbacks_call(user_id: int, page: int, per_page: int) -> Call[Page[Feedback]]:
        return _api(
            "/user_feedbacks",
            _page(Feedback.from_api, "user_feedbacks"),
            user_id=user_id,
            page=page,
            per_page=per_page,
        )

    @staticmethod
    def _user_followers_call(user_id: int, page: int, per_page: int) -> Call[Page[User]]:
        return _api(f"/users/{user_id}/followers", _page(User.from_api, "users"), page=page, per_page=per_page)

    @staticmethod
    def _brands_call(keyword: str) -> Call[list[Brand]]:
        return _api("/brands", lambda data: [Brand.from_api(raw) for raw in data["brands"]], keyword=keyword)

    @staticmethod
    def _brand_call(brand_id: int) -> Call[Brand]:
        return _api(f"/brands/{brand_id}", lambda data: Brand.from_api(data["brand"]))

    @staticmethod
    def _colors_call() -> Call[list[Color]]:
        return _api("/colors", lambda data: [Color.from_api(raw) for raw in data["colors"]])

    @staticmethod
    def _countries_call() -> Call[list[dict]]:
        return _api("/countries", lambda data: data["countries"])


def has_next(page: Page[Any], number: int, max_pages: int | None) -> bool:
    """Whether pagination should fetch the page after `number`."""

    return bool(page.items) and page.pagination.has_next and not (max_pages and number >= max_pages)
