"""Client behaviour (cookies, proxies, failover, pagination) with fake HTTP sessions."""

import asyncio
import json

import pytest
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError

from vinted import AsyncVinted
from vinted import BlockedError
from vinted import NetworkError
from vinted import NotFoundError
from vinted import RateLimitError
from vinted import Vinted
from vinted._core import ProxyPool
from vinted._core import Slot
from vinted._core import redact_proxy

USER = {"user": {"id": 1, "login": "alice"}}


class FakeResponse:
    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.headers = {"content-type": "text/html" if isinstance(body, str) else "application/json"}
        self.headers.update(headers or {})

    def json(self):
        return json.loads(self.text)


class FakeCookies:
    def __init__(self):
        self.cleared = 0

    def clear(self):
        self.cleared += 1


class FakeSession:
    """Answers with `router(proxy, path, params)` -> (status, body) or an exception, and logs requests."""

    def __init__(self, proxy, router, log):
        self.proxy, self.router, self.log = proxy, router, log
        self.cookies = FakeCookies()

    def _answer(self, url, params):
        path = url.split("vinted.fr", 1)[1]
        self.log.append((self.proxy, path))
        answer = self.router(self.proxy, path, params)
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(*answer)

    def request(self, method, url, params=None, headers=None, timeout=None):
        return self._answer(url, params)

    def close(self):
        pass


class FakeAsyncSession(FakeSession):
    in_flight = 0
    max_in_flight = 0

    async def request(self, method, url, params=None, headers=None, timeout=None):
        FakeAsyncSession.in_flight += 1
        FakeAsyncSession.max_in_flight = max(FakeAsyncSession.max_in_flight, FakeAsyncSession.in_flight)
        try:
            await asyncio.sleep(0.01)
            return self._answer(url, params)
        finally:
            FakeAsyncSession.in_flight -= 1

    async def close(self):
        pass


def install(client, router, session_cls=FakeSession):
    log = []
    for slot in client._pool.slots:
        slot.session = session_cls(slot.proxy, router, log)
    return log


def ok_router(proxy, path, params):
    return (200, "<html>home</html>") if path == "/" else (200, USER)


# -- proxy helpers ----------------------------------------------------------------------------


def test_redact_proxy_hides_credentials():
    assert redact_proxy("http://user:secret@1.2.3.4:8080") == "proxy http://1.2.3.4:8080"
    assert redact_proxy(None) == "direct connection"


def test_proxy_pool_failover_and_rotation():
    a, b, c = (Slot(None, name) for name in "abc")
    sticky = ProxyPool([a, b, c], rotate=False)
    assert [sticky.pick() for _ in range(3)] == [a, a, a]
    sticky.failed(a)
    sticky.failed(a)  # a second report about the same slot does not skip `b`
    assert sticky.pick() is b

    rotating = ProxyPool([a, b, c], rotate=True)
    assert [rotating.pick() for _ in range(4)] == [a, b, c, a]


def test_proxies_option():
    assert Vinted().proxies == []
    assert Vinted(proxies="http://p:1").proxies == ["http://p:1"]
    assert len(Vinted(proxies=["http://p:1", "http://q:2"])._pool) == 2
    with pytest.raises(ValueError):
        Vinted(proxies=[])


# -- sync client ------------------------------------------------------------------------------


def test_cookies_fetched_once_then_renewed_on_401():
    with Vinted() as vinted:
        expired = {"done": False}

        def router(proxy, path, params):
            if path == "/api/v2/users/1" and len(log) == 3 and not expired["done"]:
                expired["done"] = True
                return 401, {"message": "invalid token"}
            return ok_router(proxy, path, params)

        log = install(vinted, router)
        assert vinted.get_user(1).login == "alice"
        assert vinted.get_user(1).login == "alice"  # 401, new cookies, retried
        user = "/api/v2/users/1"
        assert [path for _, path in log] == ["/", user, user, "/", user]


def test_failover_to_next_proxy():
    def router(proxy, path, params):
        if proxy == "http://bad:1":
            return CurlConnectionError("refused")
        if proxy == "http://blocked:1":
            return 403, "<html>blocked</html>"
        return ok_router(proxy, path, params)

    with Vinted(proxies=["http://bad:1", "http://blocked:1", "http://good:1"]) as vinted:
        log = install(vinted, router)
        assert vinted.get_user(1).login == "alice"
        assert [proxy for proxy, _ in log] == ["http://bad:1", "http://blocked:1", "http://good:1", "http://good:1"]
        log.clear()
        vinted.get_user(1)  # sticks to the proxy that works
        assert log == [("http://good:1", "/api/v2/users/1")]


def test_all_proxies_failing_raises_last_error():
    with Vinted(proxies=["http://a:1", "http://b:1"]) as vinted:
        install(vinted, lambda proxy, path, params: CurlConnectionError("down"))
        with pytest.raises(NetworkError, match="proxy http://b:1"):
            vinted.get_user(1)


def test_not_found_is_not_retried_on_other_proxies():
    def router(proxy, path, params):
        return (404, {"message": "Contenu non trouvé"}) if path.startswith("/api") else ok_router(proxy, path, params)

    with Vinted(proxies=["http://a:1", "http://b:1"]) as vinted:
        log = install(vinted, router)
        with pytest.raises(NotFoundError, match="Contenu non trouvé"):
            vinted.get_user(1)
        assert {proxy for proxy, _ in log} == {"http://a:1"}


def test_single_session_blocked_raises():
    with Vinted() as vinted:
        install(vinted, lambda proxy, path, params: (403, "<html>blocked</html>"))
        with pytest.raises(BlockedError):
            vinted.get_user(1)


def test_sync_pagination():
    def router(proxy, path, params):
        if path == "/":
            return 200, "<html></html>"
        page = params["page"]
        return 200, {
            "items": [{"id": page * 10 + i, "title": "t"} for i in range(2)],
            "pagination": {"current_page": page, "total_pages": 3, "total_entries": 6, "per_page": 2},
        }

    with Vinted() as vinted:
        install(vinted, router)
        assert [item.id for item in vinted.iter_user_items(1, delay=0)] == [10, 11, 20, 21, 30, 31]
        assert len(list(vinted.iter_user_items(1, delay=0, max_pages=2))) == 4


def rate_limited_router(times, headers=None):
    """Answer HTTP 429 to the first `times` API requests."""

    state = {"left": times}

    def router(proxy, path, params):
        if path != "/" and state["left"] > 0:
            state["left"] -= 1
            return 429, "<html>slow down</html>", headers or {}
        return ok_router(proxy, path, params)

    return router


def test_rate_limit_retried_with_exponential_backoff(monkeypatch):
    sleeps = []
    monkeypatch.setattr("vinted.client.time.sleep", sleeps.append)
    with Vinted(backoff=2) as vinted:
        install(vinted, rate_limited_router(3))
        assert vinted.get_user(1).login == "alice"
    assert sleeps == pytest.approx([2, 4, 8], abs=0.1)


def test_rate_limit_uses_retry_after_and_gives_up(monkeypatch):
    sleeps = []
    monkeypatch.setattr("vinted.client.time.sleep", sleeps.append)
    with Vinted(retries=2) as vinted:
        install(vinted, rate_limited_router(10, {"retry-after": "7"}))
        with pytest.raises(RateLimitError) as info:
            vinted.get_user(1)
    assert sleeps == pytest.approx([7, 7], abs=0.1) and info.value.retry_after == 7


def test_retries_disabled_and_404_not_retried(monkeypatch):
    sleeps = []
    monkeypatch.setattr("vinted.client.time.sleep", sleeps.append)
    with Vinted(retries=0) as vinted:
        install(vinted, rate_limited_router(1))
        with pytest.raises(RateLimitError):
            vinted.get_user(1)
    with Vinted() as vinted:
        install(vinted, lambda proxy, path, params: (404, {}) if path != "/" else (200, ""))
        with pytest.raises(NotFoundError):
            vinted.get_user(1)
    assert sleeps == []


def test_async_rate_limit_retry():
    async def main():
        async with AsyncVinted(backoff=0.01) as vinted:
            install(vinted, rate_limited_router(2), FakeAsyncSession)
            return await vinted.get_user(1)

    assert asyncio.run(main()).login == "alice"


def test_async_rate_limit_pauses_all_tasks():
    times = []

    def router(proxy, path, params):
        now = asyncio.get_running_loop().time()
        if path == "/":
            return 200, ""
        times.append(now)
        return (429, "<html>slow down</html>") if len(times) == 1 else (200, USER)

    async def main():
        async with AsyncVinted(backoff=0.2, max_concurrency=1) as vinted:
            install(vinted, router, FakeAsyncSession)
            return await asyncio.gather(*(vinted.get_user(1) for _ in range(4)))

    assert len(asyncio.run(main())) == 4
    # Requests queued behind the rate-limited one also waited for the cooldown.
    assert all(later - times[0] >= 0.2 for later in times[1:])


# -- async client -----------------------------------------------------------------------------


def test_async_client_concurrency_limit_and_single_cookie_fetch():
    async def main():
        async with AsyncVinted(max_concurrency=3) as vinted:
            log = install(vinted, ok_router, FakeAsyncSession)
            users = await asyncio.gather(*(vinted.get_user(1) for _ in range(10)))
            return users, log

    FakeAsyncSession.max_in_flight = 0
    users, log = asyncio.run(main())
    assert [user.login for user in users] == ["alice"] * 10
    assert [path for _, path in log].count("/") == 1  # concurrent tasks share one cookie fetch
    assert FakeAsyncSession.max_in_flight == 3


def test_async_failover_and_pagination():
    def router(proxy, path, params):
        if proxy == "http://bad:1":
            return CurlConnectionError("refused")
        if path == "/":
            return 200, "<html></html>"
        page = params["page"]
        return 200, {
            "items": [{"id": page, "title": "t"}],
            "pagination": {"current_page": page, "total_pages": 2, "total_entries": 2, "per_page": 1},
        }

    async def main():
        async with AsyncVinted(proxies=["http://bad:1", "http://good:1"]) as vinted:
            install(vinted, router, FakeAsyncSession)
            return [item.id async for item in vinted.iter_user_items(1, delay=0)]

    assert asyncio.run(main()) == [1, 2]


# -- live -------------------------------------------------------------------------------------


@pytest.mark.live
def test_live_sync():
    with Vinted() as vinted:
        page = vinted.search("lacoste", brand_ids=[304], order="newest_first")
        assert page.items and all(item.brand for item in page.items)
        details = vinted.get_item(page.items[0].id)
        assert details.title and details.seller_id
        assert vinted.get_user(details.seller_id).login
        assert vinted.get_user_items(details.seller_id, per_page=2).items
        assert vinted.search_brands("lacoste")[0].id == 304


@pytest.mark.live
def test_live_async():
    async def main():
        async with AsyncVinted() as vinted:
            page = await vinted.search("lacoste", brand_ids=[304])
            details = await asyncio.gather(*(vinted.get_item(item.id) for item in page.items[:4]))
            assert all(d.title for d in details)
            sellers = await asyncio.gather(*(vinted.get_user(d.seller_id) for d in details))
            assert all(s.login for s in sellers)
            items = [item async for item in vinted.iter_search("lacoste", max_pages=2, delay=0.2)]
            assert len(items) > 96

    asyncio.run(main())
