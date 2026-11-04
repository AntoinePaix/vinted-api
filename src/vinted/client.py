"""Synchronous client for Vinted's undocumented ("hidden") web API."""

from __future__ import annotations

import time
from collections.abc import Iterator
from collections.abc import Sequence
from typing import Any
from typing import Self
from typing import TypeVar
from typing import Unpack

from curl_cffi import requests

from ._core import FAILOVER_STATUSES
from ._core import BaseVinted
from ._core import Call
from ._core import ProxyPool
from ._core import Slot
from ._core import check_response
from ._core import has_next
from ._core import home_call
from ._core import redact_proxy
from .errors import NetworkError
from .errors import VintedError
from .filters import Order
from .filters import SearchFilters
from .models import Brand
from .models import Color
from .models import Feedback
from .models import Item
from .models import ItemDetails
from .models import Page
from .models import User

T = TypeVar("T")


class Vinted(BaseVinted):
    """Access Vinted's public data with a browser-like session.

    Args:
        domain: Vinted site to use (`vinted.fr`, `vinted.de`, `vinted.co.uk`...). Results,
            currency and language depend on it.
        proxies: a proxy URL (``"http://user:pass@host:port"``, ``"socks5://host:port"``...)
            or a list of them. Each proxy gets its own session and cookies. When a request
            fails on a proxy (network error, HTTP 403 or 429), it is retried on the next one.
            Use `None` in the list for a direct connection, e.g. ``["http://proxy:8080", None]``.
        rotate_proxies: use the proxies in turn for each request (round-robin) instead of
            sticking to one until it fails.
        retries: how many times to retry a request that failed with HTTP 429 (rate limited)
            or 5xx, once every proxy has been tried. 0 disables retries.
        backoff: seconds to wait before the first retry, doubled for each following one
            (5, 10, 20, 40s by default). While waiting, the whole client pauses. Vinted's
            `Retry-After` header is used when present.
        impersonate: browser whose TLS/HTTP fingerprint curl-cffi mimics.
        timeout: request timeout in seconds.

    Example:
        >>> with Vinted() as vinted:
        ...     for item in vinted.search("lacoste", price_to=20):
        ...         print(item.title, item.price)
    """

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
        super().__init__(
            domain,
            proxies=proxies,
            rotate_proxies=rotate_proxies,
            retries=retries,
            backoff=backoff,
            impersonate=impersonate,
            timeout=timeout,
        )
        self._pool: ProxyPool[requests.Session] = ProxyPool(
            [Slot(requests.Session(**self._session_kwargs(proxy)), proxy) for proxy in self._proxies],
            rotate=rotate_proxies,
        )

    # -- plumbing ---------------------------------------------------------------------------

    def close(self) -> None:
        for slot in self._pool.slots:
            slot.session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _http(self, slot: Slot[requests.Session], call: Call[Any]) -> requests.Response:
        if (wait := self._cooldown_remaining()) > 0:
            time.sleep(wait)
        try:
            return slot.session.request(
                call.method,
                self.base_url + call.path,
                params=call.params,
                headers=call.headers,
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as exc:
            raise NetworkError(f"{call.method} {call.path} failed via {redact_proxy(slot.proxy)}: {exc}") from exc

    def _renew_cookies(self, slot: Slot[requests.Session], generation: int) -> None:
        if slot.generation != generation:  # already renewed since the caller's request
            return
        slot.session.cookies.clear()
        response = self._http(slot, home_call())
        check_response(response, "GET", "/")
        slot.has_cookies = True
        slot.generation += 1

    def _fetch(self, slot: Slot[requests.Session], call: Call[Any]) -> requests.Response:
        if call.authenticate and not slot.has_cookies:
            self._renew_cookies(slot, slot.generation)
        generation = slot.generation
        response = self._http(slot, call)
        # The anonymous token expires: get a new one and retry once.
        if response.status_code == 401 and call.authenticate:
            self._renew_cookies(slot, generation)
            response = self._http(slot, call)
        check_response(response, call.method, call.path)
        return response

    def _send(self, call: Call[T]) -> T:
        retry = 0
        while True:
            try:
                return self._send_once(call)
            except VintedError as error:
                delay = self._retry_delay(error, retry)
                if delay is None:
                    raise
                self._start_cooldown(delay)
                retry += 1

    def _send_once(self, call: Call[T]) -> T:
        """Send the call, failing over to the next proxy on network errors, 403 and 429."""

        attempts = len(self._pool)
        for attempt in range(1, attempts + 1):
            slot = self._pool.pick()
            try:
                response = self._fetch(slot, call)
            except VintedError as error:
                retryable = isinstance(error, NetworkError) or error.status_code in FAILOVER_STATUSES
                if retryable and attempt < attempts:
                    self._pool.failed(slot)
                    continue
                raise
            return call.parse(response)
        raise AssertionError("unreachable")

    def _paginate(self, fetch: Any, max_pages: int | None, delay: float) -> Iterator[Any]:
        number = 1
        while True:
            page = fetch(number)
            yield from page
            if not has_next(page, number, max_pages):
                return
            number += 1
            time.sleep(delay)

    # -- search -----------------------------------------------------------------------------

    def search(self, query: str | None = None, *, page: int = 1, **filters: Unpack[SearchFilters]) -> Page[Item]:
        """Search the catalog. Returns one page of (up to 96) items.

        Args:
            query: free text search.
            page: page number, starting at 1.
            **filters: `catalog_ids`, `brand_ids`, `size_ids`, `status_ids`, `color_ids`,
                `material_ids` (lists of ids), `price_from`, `price_to`, `currency`,
                `order` (an `Order`, e.g. ``Order.NEWEST_FIRST``), and `extra` (dict of raw
                URL parameters). See `SearchFilters`.
        """

        return self._send(self._search_call(self.build_search_params(query, page=page, **filters)))

    def search_url(self, url: str, *, page: int | None = None) -> Page[Item]:
        """Run a search copied from the browser, e.g. ``https://www.vinted.fr/catalog?...``."""

        return self._send(self._search_call(self.params_from_url(url, page)))

    def iter_search(
        self,
        query: str | None = None,
        *,
        max_pages: int | None = None,
        delay: float = 1.0,
        **filters: Unpack[SearchFilters],
    ) -> Iterator[Item]:
        """Iterate over search results across pages (waiting `delay` seconds between pages)."""

        return self._paginate(lambda page: self.search(query, page=page, **filters), max_pages, delay)

    # -- items ------------------------------------------------------------------------------

    def get_item(self, item_id: int) -> ItemDetails:
        """Full details of an item (description, photos, color, category...)."""

        return self._send(self._item_call(item_id))

    # -- users ------------------------------------------------------------------------------

    def get_user(self, user_id: int) -> User:
        return self._send(self._user_call(user_id))

    def get_user_items(
        self,
        user_id: int,
        *,
        page: int = 1,
        per_page: int = 96,
        order: Order | str | None = None,
    ) -> Page[Item]:
        """Items in a member's wardrobe, sorted by `order` (see `Order`)."""

        return self._send(self._user_items_call(user_id, page, per_page, order))

    def iter_user_items(
        self,
        user_id: int,
        *,
        max_pages: int | None = None,
        delay: float = 0.5,
        **kwargs: Any,
    ) -> Iterator[Item]:
        return self._paginate(lambda page: self.get_user_items(user_id, page=page, **kwargs), max_pages, delay)

    def get_user_feedbacks(self, user_id: int, *, page: int = 1, per_page: int = 20) -> Page[Feedback]:
        return self._send(self._user_feedbacks_call(user_id, page, per_page))

    def get_user_followers(self, user_id: int, *, page: int = 1, per_page: int = 20) -> Page[User]:
        return self._send(self._user_followers_call(user_id, page, per_page))

    # -- reference data ---------------------------------------------------------------------

    def search_brands(self, keyword: str) -> list[Brand]:
        """Find brands (and their ids, for `search(brand_ids=...)`) by name."""

        return self._send(self._brands_call(keyword))

    def get_brand(self, brand_id: int) -> Brand:
        return self._send(self._brand_call(brand_id))

    def get_colors(self) -> list[Color]:
        """All colors (and their ids, for `search(color_ids=...)`)."""

        return self._send(self._colors_call())

    def get_countries(self) -> list[dict]:
        return self._send(self._countries_call())
