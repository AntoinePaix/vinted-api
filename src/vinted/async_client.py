"""Asynchronous client for Vinted's undocumented ("hidden") web API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from collections.abc import Awaitable
from collections.abc import Callable
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


class AsyncVinted(BaseVinted):
    """Async version of `Vinted`: same methods, to be awaited.

    Args:
        domain: Vinted site to use (`vinted.fr`, `vinted.de`, `vinted.co.uk`...).
        proxies: a proxy URL or a list of them, see `Vinted`.
        rotate_proxies: use the proxies in turn for each request (round-robin).
        retries: retries for requests failing with HTTP 429 or 5xx, see `Vinted`.
        backoff: seconds before the first retry, doubled for each following one. While
            waiting, all the client's requests pause.
        max_concurrency: maximum number of requests in flight at once. Vinted throttles
            aggressive clients, so keep it low (it applies to the client as a whole,
            whatever the number of proxies).
        impersonate: browser whose TLS/HTTP fingerprint curl-cffi mimics.
        timeout: request timeout in seconds.

    Example:
        >>> async with AsyncVinted() as vinted:
        ...     page = await vinted.search("lacoste", price_to=20)
        ...     details = await asyncio.gather(*(vinted.get_item(item.id) for item in page))
    """

    def __init__(
        self,
        domain: str = "vinted.fr",
        *,
        proxies: str | Sequence[str | None] | None = None,
        rotate_proxies: bool = False,
        retries: int = 4,
        backoff: float = 5.0,
        max_concurrency: int = 4,
        impersonate: str = "chrome",
        timeout: float = 30,
    ):
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        super().__init__(
            domain,
            proxies=proxies,
            rotate_proxies=rotate_proxies,
            retries=retries,
            backoff=backoff,
            impersonate=impersonate,
            timeout=timeout,
        )
        self.max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._pool: ProxyPool[requests.AsyncSession] = ProxyPool(
            [
                Slot(requests.AsyncSession(**self._session_kwargs(proxy)), proxy, lock=asyncio.Lock())
                for proxy in self._proxies
            ],
            rotate=rotate_proxies,
        )

    # -- plumbing ---------------------------------------------------------------------------

    async def close(self) -> None:
        for slot in self._pool.slots:
            await slot.session.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _http(self, slot: Slot[requests.AsyncSession], call: Call[Any]) -> requests.Response:
        async with self._semaphore:
            # A rate limit pauses all tasks, not only the one that hit it. Checked once the
            # semaphore is acquired, as the cooldown may have started while waiting for it.
            while (wait := self._cooldown_remaining()) > 0:
                await asyncio.sleep(wait)
            try:
                return await slot.session.request(
                    call.method,
                    self.base_url + call.path,
                    params=call.params,
                    headers=call.headers,
                    timeout=self.timeout,
                )
            except requests.exceptions.RequestException as exc:
                message = f"{call.method} {call.path} failed via {redact_proxy(slot.proxy)}: {exc}"
                raise NetworkError(message) from exc

    async def _renew_cookies(self, slot: Slot[requests.AsyncSession], generation: int) -> None:
        async with slot.lock:
            if slot.generation != generation:  # another task renewed them while we waited
                return
            slot.session.cookies.clear()
            response = await self._http(slot, home_call())
            check_response(response, "GET", "/")
            slot.has_cookies = True
            slot.generation += 1

    async def _fetch(self, slot: Slot[requests.AsyncSession], call: Call[Any]) -> requests.Response:
        if call.authenticate and not slot.has_cookies:
            await self._renew_cookies(slot, slot.generation)
        generation = slot.generation
        response = await self._http(slot, call)
        # The anonymous token expires: get a new one and retry once.
        if response.status_code == 401 and call.authenticate:
            await self._renew_cookies(slot, generation)
            response = await self._http(slot, call)
        check_response(response, call.method, call.path)
        return response

    async def _send(self, call: Call[T]) -> T:
        retry = 0
        while True:
            try:
                return await self._send_once(call)
            except VintedError as error:
                delay = self._retry_delay(error, retry)
                if delay is None:
                    raise
                self._start_cooldown(delay)
                retry += 1

    async def _send_once(self, call: Call[T]) -> T:
        """Send the call, failing over to the next proxy on network errors, 403 and 429."""

        attempts = len(self._pool)
        for attempt in range(1, attempts + 1):
            slot = self._pool.pick()
            try:
                response = await self._fetch(slot, call)
            except VintedError as error:
                retryable = isinstance(error, NetworkError) or error.status_code in FAILOVER_STATUSES
                if retryable and attempt < attempts:
                    self._pool.failed(slot)
                    continue
                raise
            return call.parse(response)
        raise AssertionError("unreachable")

    async def _paginate(
        self,
        fetch: Callable[[int], Awaitable[Page[Any]]],
        max_pages: int | None,
        delay: float,
    ) -> AsyncIterator[Any]:
        number = 1
        while True:
            page = await fetch(number)
            for item in page:
                yield item
            if not has_next(page, number, max_pages):
                return
            number += 1
            await asyncio.sleep(delay)

    # -- search -----------------------------------------------------------------------------

    async def search(
        self,
        query: str | None = None,
        *,
        page: int = 1,
        **filters: Unpack[SearchFilters],
    ) -> Page[Item]:
        """Search the catalog. Returns one page of (up to 96) items. See `Vinted.search`."""

        return await self._send(self._search_call(self.build_search_params(query, page=page, **filters)))

    async def search_url(self, url: str, *, page: int | None = None) -> Page[Item]:
        """Run a search copied from the browser, e.g. ``https://www.vinted.fr/catalog?...``."""

        return await self._send(self._search_call(self.params_from_url(url, page)))

    def iter_search(
        self,
        query: str | None = None,
        *,
        max_pages: int | None = None,
        delay: float = 1.0,
        **filters: Unpack[SearchFilters],
    ) -> AsyncIterator[Item]:
        """Iterate over search results across pages: ``async for item in vinted.iter_search(...)``."""

        return self._paginate(lambda page: self.search(query, page=page, **filters), max_pages, delay)

    # -- items ------------------------------------------------------------------------------

    async def get_item(self, item_id: int) -> ItemDetails:
        """Full details of an item (description, photos, color, category...)."""

        return await self._send(self._item_call(item_id))

    # -- users ------------------------------------------------------------------------------

    async def get_user(self, user_id: int) -> User:
        return await self._send(self._user_call(user_id))

    async def get_user_items(
        self,
        user_id: int,
        *,
        page: int = 1,
        per_page: int = 96,
        order: Order | str | None = None,
    ) -> Page[Item]:
        """Items in a member's wardrobe, sorted by `order` (see `Order`)."""

        return await self._send(self._user_items_call(user_id, page, per_page, order))

    def iter_user_items(
        self,
        user_id: int,
        *,
        max_pages: int | None = None,
        delay: float = 0.5,
        **kwargs: Any,
    ) -> AsyncIterator[Item]:
        return self._paginate(lambda page: self.get_user_items(user_id, page=page, **kwargs), max_pages, delay)

    async def get_user_feedbacks(self, user_id: int, *, page: int = 1, per_page: int = 20) -> Page[Feedback]:
        return await self._send(self._user_feedbacks_call(user_id, page, per_page))

    async def get_user_followers(self, user_id: int, *, page: int = 1, per_page: int = 20) -> Page[User]:
        return await self._send(self._user_followers_call(user_id, page, per_page))

    # -- reference data ---------------------------------------------------------------------

    async def search_brands(self, keyword: str) -> list[Brand]:
        """Find brands (and their ids, for `search(brand_ids=...)`) by name."""

        return await self._send(self._brands_call(keyword))

    async def get_brand(self, brand_id: int) -> Brand:
        return await self._send(self._brand_call(brand_id))

    async def get_colors(self) -> list[Color]:
        """All colors (and their ids, for `search(color_ids=...)`)."""

        return await self._send(self._colors_call())

    async def get_countries(self) -> list[dict]:
        return await self._send(self._countries_call())
