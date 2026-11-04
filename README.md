# vinted-api

A small Python client for Vinted's hidden (undocumented) web API, built on
[curl-cffi](https://github.com/lexiforest/curl_cffi) so requests look like a real browser.

- Search the catalog with filters (brand, size, condition, color, price, sort...) or with a URL copied from your browser.
- Get full item details: description, photos, color, category, seller.
- Get members, their wardrobe, their reviews and followers.
- Look up brand and color ids to use in searches.
- Sync (`Vinted`) and async (`AsyncVinted`) clients with the same methods.
- Proxies: one or several, with failover or round-robin rotation.
- Rate limits handled for you: automatic retries with exponential backoff.

No account is needed, only public data is accessed. Be gentle: Vinted rate limits aggressive clients.

## Installation

Requires Python 3.11+.

### With uv

To use it in your project ([uv](https://docs.astral.sh/uv/)):

```bash
uv add git+https://github.com/AntoinePaix/vinted-api
```

To work on it:

```bash
git clone https://github.com/AntoinePaix/vinted-api && cd vinted-api
uv sync
```

### Without uv

To use it in your project, with pip:

```bash
pip install git+https://github.com/AntoinePaix/vinted-api
```

To work on it:

```bash
git clone https://github.com/AntoinePaix/vinted-api && cd vinted-api
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e . --group dev  # --group needs pip 25.1+
```

Then run the commands of the [Development](#development) section without the `uv run` prefix
(e.g. `pytest`, `ruff check --fix .`).

## Quick start

```python
from vinted import Order
from vinted import Vinted

with Vinted() as vinted:  # vinted.fr by default
    for item in vinted.search("pull lacoste", price_to=20, order=Order.NEWEST_FIRST):
        print(f"{item.price} {item.currency}  {item.size or '-':>4}  {item.title}  {item.url}")
```

Run any example with `uv run python my_script.py`.

## Examples

### Search with filters

Filters take Vinted ids. Use `search_brands()` and `get_colors()` to find them, or set
the filters on vinted.fr and read the ids in the URL.

```python
from vinted import Order
from vinted import Vinted

with Vinted() as vinted:
    lacoste = vinted.search_brands("lacoste")[0]  # Brand(id=304, title='Lacoste', ...)
    black = next(c for c in vinted.get_colors() if c.code == "BLACK")

    page = vinted.search(
        brand_ids=[lacoste.id],
        catalog_ids=[79],  # category
        size_ids=[207],  # S
        status_ids=[6, 1, 2],  # new with tags, new without tags, very good
        color_ids=[black.id],
        price_from=5,
        price_to=20,
        order=Order.NEWEST_FIRST,
    )

    print(f"{page.pagination.total_entries} results, {page.pagination.total_pages} pages")
    for item in page:
        print(item.id, item.title, item.price, item.total_price, item.brand, item.size, item.status)

    # The same search in your browser:
    print(vinted.search_page_url(brand_ids=[lacoste.id], price_to=20))
```

All the filters are typed (see `vinted.SearchFilters`), so your editor autocompletes them.

| Filter | Values |
| --- | --- |
| `order` | `Order.RELEVANCE`, `Order.NEWEST_FIRST`, `Order.PRICE_LOW_TO_HIGH`, `Order.PRICE_HIGH_TO_LOW` (or the same as strings: `"newest_first"`...) |
| `status_ids` | `6` new with tags, `1` new without tags, `2` very good, `3` good, `4` satisfactory |
| `brand_ids` | from `search_brands("...")` |
| `color_ids` | from `get_colors()` |
| `catalog_ids`, `size_ids`, `material_ids` | read them in the URL of a search made on the website |
| `price_from`, `price_to`, `currency` | numbers, currency code |

`get_user_items()` accepts the same `order` values.

Any other URL parameter can be passed through `extra`:

```python
vinted.search("switch", extra={"video_game_platform_ids[]": [1, 2]})
```

### Search from a browser URL

The simplest way to use every filter Vinted offers: build the search on the website and paste the URL.

```python
from vinted import Vinted

url = "https://www.vinted.fr/catalog?catalog[]=79&brand_ids[]=304&size_ids[]=207&price_to=20&order=newest_first"

with Vinted() as vinted:
    first = vinted.search_url(url)
    second = vinted.search_url(url, page=2)
```

### Iterate over several pages

A search page holds up to 96 items. `iter_search()` follows pages for you:

```python
from vinted import Vinted

with Vinted() as vinted:
    items = list(vinted.iter_search("nike air max", max_pages=3, price_to=50))
    cheapest = min(items, key=lambda item: item.price)
    print(len(items), "items, cheapest:", cheapest.title, cheapest.price, cheapest.url)
```

### Item details

```python
from vinted import Vinted

with Vinted() as vinted:
    details = vinted.get_item(10667044407)
    print(details.title, details.price, details.currency)
    print(details.brand, details.color, details.category, details.condition, details.available)
    print(details.description)
    for photo in details.photos:
        print(photo)
```

### Members: profile, wardrobe, reviews

```python
from vinted import Vinted

with Vinted() as vinted:
    seller = vinted.get_user(162483265)
    print(
        seller.login,
        seller.country,
        f"{seller.feedback_reputation:.0%}",
        f"({seller.feedback_count} reviews)",
        seller.item_count,
        "items",
    )

    for item in vinted.iter_user_items(seller.id):  # whole wardrobe
        print(item.title, item.price, item.size)

    for review in vinted.get_user_feedbacks(seller.id, per_page=10):
        if not review.is_automatic:
            print("★" * review.rating, review.author_login, review.text)
```

### Async

`AsyncVinted` has the same methods as `Vinted`, to be awaited. Use it to fetch many things
concurrently, or from async code (FastAPI, bots...).

```python
import asyncio

from vinted import AsyncVinted


async def main():
    async with AsyncVinted(max_concurrency=4) as vinted:
        page = await vinted.search("pull lacoste", price_to=30)

        # Details of the first 10 results, fetched concurrently (at most 4 requests at once).
        details = await asyncio.gather(*(vinted.get_item(item.id) for item in page.items[:10]))
        for item in details:
            print(item.title, item.color, item.photos[0])

        # Async iteration over pages.
        async for item in vinted.iter_search("nike air max", max_pages=2):
            print(item.title, item.price)


asyncio.run(main())
```

`max_concurrency` (default 4) caps the number of requests in flight for the whole client:
Vinted rate limits IP addresses, so going higher mostly gets you HTTP 429 responses.

### Proxies

Pass one proxy URL or a list of them (`http://`, `https://`, `socks5://`...). Each proxy gets its own
session and cookies.

```python
from vinted import AsyncVinted
from vinted import Vinted

# One proxy.
with Vinted(proxies="http://user:pass@host:8080") as vinted:
    vinted.search("nike")

# Failover: use the first proxy until it fails (network error, HTTP 403 or 429), then the next one.
# `None` means a direct connection, here as a last resort.
with Vinted(proxies=["http://user:pass@proxy1:8080", "socks5://proxy2:1080", None]) as vinted:
    vinted.search("nike")


# Round-robin: spread requests over all the proxies, still with failover.
async def main():
    proxies = [f"http://user:pass@proxy{i}.example.com:8080" for i in range(10)]
    async with AsyncVinted(proxies=proxies, rotate_proxies=True, max_concurrency=10) as vinted:
        ...
```

Errors mention the proxy that failed, without its credentials.

### Rate limits and retries

When Vinted answers HTTP 429 (or 5xx) and no other proxy is left to try, the request is retried after
5, 10, 20 then 40 seconds (Vinted's `Retry-After` header is used when present). Meanwhile the whole
client pauses, so concurrent tasks don't make things worse.

```python
from vinted import Vinted

Vinted(retries=6, backoff=10)  # wait up to 10 + 20 + 40 + 80 + 160 + 320 seconds
Vinted(retries=0)  # fail right away with RateLimitError
```

### Other countries

Results, currency and language follow the domain:

```python
from vinted import Vinted

with Vinted("vinted.de") as vinted:
    vinted.search("nike")
```

### Raw data and errors

Every model keeps the original payload in `.raw`, for fields not mapped yet.

```python
from vinted import BlockedError
from vinted import NetworkError
from vinted import NotFoundError
from vinted import RateLimitError
from vinted import Vinted
from vinted import VintedError

with Vinted() as vinted:
    try:
        vinted.get_item(1)
    except NotFoundError:  # HTTP 404
        print("gone")
    except RateLimitError:  # HTTP 429, after all retries
        print("slow down")
    except BlockedError:  # HTTP 403, usually the IP address is blocked: try a proxy
        print("blocked")
    except NetworkError:  # DNS, connection, proxy or timeout error
        print("network")
    except VintedError as error:  # base class of all of the above
        print(error.status_code, error.payload)
```

## API overview

```python
Vinted(
    domain="vinted.fr",
    proxies=None,  # str, or list of str / None
    rotate_proxies=False,
    retries=4,
    backoff=5.0,
    impersonate="chrome",  # browser fingerprint mimicked by curl-cffi
    timeout=30,
)
AsyncVinted(..., max_concurrency=4)  # same options, plus max_concurrency
```

Methods are the same on both clients (`await` them on `AsyncVinted`, and use `async for` with `iter_*`):

| Method | Returns |
| --- | --- |
| `search(query=None, *, page=1, **filters)` | `Page[Item]` |
| `search_url(url, *, page=None)` | `Page[Item]` |
| `iter_search(query=None, *, max_pages=None, **filters)` | iterator of `Item` |
| `search_page_url(query=None, **filters)` | `str` |
| `get_item(item_id)` | `ItemDetails` |
| `get_user(user_id)` | `User` |
| `get_user_items(user_id, *, page=1, per_page=96)` / `iter_user_items(...)` | `Page[Item]` / iterator |
| `get_user_feedbacks(user_id, *, page=1, per_page=20)` | `Page[Feedback]` |
| `get_user_followers(user_id, *, page=1, per_page=20)` | `Page[User]` |
| `search_brands(keyword)` / `get_brand(brand_id)` | `list[Brand]` / `Brand` |
| `get_colors()` / `get_countries()` | `list[Color]` / `list[dict]` |

A `Page` is iterable and exposes `.items` and `.pagination` (`current_page`, `total_pages`, `total_entries`, `has_next`).

## How it works

- JSON endpoints (`/api/v2/users`, `/wardrobe`, `/user_feedbacks`, `/brands`, `/colors`...) need the anonymous
  `access_token_web` cookie handed out by the home page. The client fetches it on first use and renews it on 401.
  Cookies are kept per proxy, as Cloudflare ties them to the IP address.
- Vinted no longer serves search or item details as JSON to anonymous clients, so these are read from the
  data embedded in the server-rendered pages (Next.js payload and schema.org JSON-LD).
- Requests and parsing are shared by both clients (`vinted/_core.py`); `Vinted` and `AsyncVinted` only
  differ in how they send requests.

This is unofficial and may break whenever Vinted changes its website.

## Development

```bash
uv run pytest            # offline tests
uv run pytest -m live    # smoke test against the real website
uv run ruff check --fix . && uv run ruff format .   # lint + format
```
