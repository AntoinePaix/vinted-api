import json
from decimal import Decimal

import pytest

from vinted import Item
from vinted import ItemDetails
from vinted import Order
from vinted import Vinted
from vinted import _parsing

CATALOG_ITEM = {
    "id": 42,
    "productItem": {
        "id": 42,
        "title": " Pull Lacoste ",
        "url": "/items/42-pull-lacoste",
        "favouriteCount": 3,
        "price": {"amount": "15.00", "currencyCode": "EUR"},
        "totalItemPrice": {"amount": "16.45", "currencyCode": "EUR"},
        "thumbnailUrl": "https://img/42.webp",
        "user": {"id": 7},
        "itemBox": {"firstLine": "Lacoste", "secondLine": "S · Très bon état"},
    },
}


def flight_page(payload: str, extra_html: str = "") -> str:
    """Build a fake Next.js page that pushes `payload` in two flight chunks."""

    half = len(payload) // 2
    chunks = "".join(
        f"<script>self.__next_f.push({json.dumps([1, part])})</script>" for part in (payload[:half], payload[half:])
    )
    return f"<html><body>{extra_html}<script>self.__next_f.push([0])</script>{chunks}</body></html>"


def test_flight_payload_and_find_json():
    payload = '0:{"pagination":"wrong"}\n1:{"items":{"items":[1,2],"pagination":{"current_page":1}}}'
    data = _parsing.flight_payload(flight_page(payload))
    assert data == payload
    block = _parsing.find_json(data, '"items":{"items":', '"items":')
    assert block == {"items": [1, 2], "pagination": {"current_page": 1}}
    with pytest.raises(KeyError):
        _parsing.find_json(data, '"nope":', '"nope":')


def test_item_from_catalog():
    item = Item.from_catalog(CATALOG_ITEM, "https://www.vinted.fr")
    assert item.id == 42
    assert item.title == "Pull Lacoste"
    assert item.url == "https://www.vinted.fr/items/42-pull-lacoste"
    assert (item.price, item.currency, item.total_price) == (Decimal("15.00"), "EUR", Decimal("16.45"))
    assert (item.brand, item.size, item.status) == ("Lacoste", "S", "Très bon état")
    assert item.user_id == 7 and item.favourite_count == 3


def test_item_from_catalog_without_size():
    raw = json.loads(json.dumps(CATALOG_ITEM))
    raw["productItem"]["itemBox"]["secondLine"] = "Neuf avec étiquette"
    item = Item.from_catalog(raw, "")
    assert (item.size, item.status) == (None, "Neuf avec étiquette")


def test_item_from_api():
    item = Item.from_api(
        {
            "id": 1,
            "title": "T",
            "url": "https://u",
            "user_id": 2,
            "brand": "B",
            "size": "M",
            "status": "Bon état",
            "price": {"amount": "31.5", "currency_code": "EUR"},
            "photos": [{"url": "a", "is_main": False}, {"url": "b", "is_main": True}],
        },
    )
    assert item.price == Decimal("31.5") and item.currency == "EUR" and item.photo_url == "b"


def test_item_details_from_page():
    ld = {
        "@type": "Product",
        "name": "Pull",
        "description": "Desc",
        "color": "Noir",
        "category": "Hommes Sweats",
        "brand": {"name": "Lacoste"},
        "offers": {
            "url": "https://u",
            "price": 20,
            "priceCurrency": "EUR",
            "availability": "InStock",
            "itemCondition": "https://schema.org/UsedCondition",
        },
    }
    payload = (
        '{"data":{"data":{"item_id":"9","photos":[{"thumbnails":[{"url":"big","width":800},{"url":"small","width":70}]}]},'
        '"name":"gallery","type":"gallery"}}'
        '{"data":{"data":{"favourite_count":4,"seller_id":"5"},"name":"favourite"}}'
        '{"data":{"data":{"brand_id":"304","catalog_id":"264"},"name":"breadcrumbs"}}'
    )
    page = flight_page(payload, f'<script type="application/ld+json">{json.dumps(ld)}</script>')
    found = _parsing.json_ld(page, "Product")
    details = ItemDetails.from_page(9, found, _parsing.plugins(_parsing.flight_payload(page)))
    assert details.title == "Pull" and details.price == Decimal(20) and details.available
    assert details.condition == "UsedCondition"
    assert (details.seller_id, details.brand_id, details.catalog_id, details.favourite_count) == (5, 304, 264, 4)
    assert details.photos == ["big"]


def test_build_search_params():
    params = Vinted.build_search_params(
        "pull",
        brand_ids=[304],
        status_ids=[6, 1],
        price_to=20,
        order="newest_first",
        page=2,
        extra={"video_game_platform_ids[]": [1]},
    )
    assert params == [
        ("search_text", "pull"),
        ("brand_ids[]", "304"),
        ("status_ids[]", "6"),
        ("status_ids[]", "1"),
        ("price_to", "20"),
        ("order", "newest_first"),
        ("video_game_platform_ids[]", "1"),
        ("page", "2"),
    ]
    with pytest.raises(ValueError, match="'newest_first'"):  # the message lists the valid orders
        Vinted.build_search_params(order="cheapest")


def test_order_enum_and_strings_are_equivalent():
    assert Vinted.build_search_params(order=Order.PRICE_LOW_TO_HIGH) == [("order", "price_low_to_high")]
    assert Vinted.build_search_params(order="price_low_to_high") == [("order", "price_low_to_high")]
    assert Order.NEWEST_FIRST == "newest_first"
    assert [order.value for order in Order] == ["relevance", "newest_first", "price_low_to_high", "price_high_to_low"]
    assert Vinted()._user_items_call(1, 1, 96, Order.NEWEST_FIRST).params["order"] == "newest_first"
