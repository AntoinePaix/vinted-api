"""Typed views over Vinted payloads. Every model keeps the original payload in `raw`."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from typing import Any
from typing import Generic
from typing import TypeVar

T = TypeVar("T")


def _money(value: Any) -> tuple[Decimal | None, str | None]:
    """Parse {"amount": "12.5", "currency_code"|"currencyCode": "EUR"} payloads."""

    if not isinstance(value, dict) or value.get("amount") is None:
        return None, None
    return Decimal(str(value["amount"])), value.get("currency_code") or value.get("currencyCode")


@dataclass
class Pagination:
    current_page: int
    total_pages: int
    total_entries: int
    per_page: int

    @classmethod
    def from_api(cls, data: dict) -> Pagination:
        return cls(
            current_page=int(data.get("current_page", 1)),
            total_pages=int(data.get("total_pages", 1)),
            total_entries=int(data.get("total_entries", 0)),
            per_page=int(data.get("per_page", 0)),
        )

    @property
    def has_next(self) -> bool:
        return self.current_page < self.total_pages


@dataclass
class Page(Generic[T]):
    """One page of results. Iterable, and `len()` gives the number of results on the page."""

    items: list[T]
    pagination: Pagination

    def __iter__(self) -> Iterator[T]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)


@dataclass
class Item:
    """An item as listed in search results or in a member's wardrobe."""

    id: int
    title: str
    url: str
    price: Decimal | None
    currency: str | None
    total_price: Decimal | None  # price + Vinted buyer protection fee
    brand: str | None
    size: str | None
    status: str | None  # condition, e.g. "Très bon état"
    user_id: int | None
    favourite_count: int
    photo_url: str | None
    raw: dict = field(repr=False)

    @classmethod
    def from_catalog(cls, raw: dict, base_url: str) -> Item:
        """Build from the item objects embedded in a catalog (search) page."""

        product = raw["productItem"]
        box = product.get("itemBox") or {}
        # secondLine is "size · condition", or just "condition" for size-less categories.
        details = [part.strip() for part in (box.get("secondLine") or "").split("·") if part.strip()]
        size, status = (details[0], details[-1]) if len(details) > 1 else (None, details[0] if details else None)
        price, currency = _money(product.get("price"))
        total, _ = _money(product.get("totalItemPrice"))
        return cls(
            id=int(product["id"]),
            title=product.get("title", "").strip(),
            url=base_url + product["url"],
            price=price,
            currency=currency,
            total_price=total,
            brand=box.get("firstLine") or None,
            size=size,
            status=status,
            user_id=(product.get("user") or {}).get("id"),
            favourite_count=int(product.get("favouriteCount") or 0),
            photo_url=product.get("thumbnailUrl"),
            raw=raw,
        )

    @classmethod
    def from_api(cls, raw: dict) -> Item:
        """Build from the JSON API item objects (e.g. `/api/v2/wardrobe/{id}/items`)."""

        price, currency = _money(raw.get("price"))
        total, _ = _money(raw.get("total_item_price"))
        photos = raw.get("photos") or []
        main = next((p for p in photos if p.get("is_main")), photos[0] if photos else {})
        return cls(
            id=int(raw["id"]),
            title=raw.get("title", "").strip(),
            url=raw.get("url", ""),
            price=price,
            currency=currency or raw.get("currency"),
            total_price=total,
            brand=raw.get("brand") or None,
            size=raw.get("size") or None,
            status=raw.get("status") or None,
            user_id=raw.get("user_id"),
            favourite_count=int(raw.get("favourite_count") or 0),
            photo_url=main.get("url"),
            raw=raw,
        )


@dataclass
class ItemDetails:
    """Full information scraped from an item page."""

    id: int
    title: str
    description: str
    url: str
    price: Decimal | None
    currency: str | None
    brand: str | None
    color: str | None
    category: str | None
    condition: str | None  # schema.org value, e.g. "UsedCondition", "NewCondition"
    available: bool
    seller_id: int | None
    catalog_id: int | None
    brand_id: int | None
    favourite_count: int
    photos: list[str]
    raw: dict = field(repr=False)  # {"json_ld": ..., "plugins": {...}}

    @classmethod
    def from_page(cls, item_id: int, ld: dict, plugins: dict[str, dict]) -> ItemDetails:
        offers = ld.get("offers") or {}
        crumbs = plugins.get("breadcrumbs", {})
        favourite = plugins.get("favourite", {})
        gallery = plugins.get("gallery", {})
        photos = []
        for photo in gallery.get("photos") or []:
            thumbs = sorted(photo.get("thumbnails") or [], key=lambda t: t.get("width") or 0)
            if thumbs:
                photos.append(thumbs[-1]["url"])

        def as_int(value: Any) -> int | None:
            return int(value) if value not in (None, "") else None

        condition = offers.get("itemCondition")
        return cls(
            id=item_id,
            title=ld.get("name", ""),
            description=ld.get("description", ""),
            url=offers.get("url", ""),
            price=Decimal(str(offers["price"])) if offers.get("price") is not None else None,
            currency=offers.get("priceCurrency"),
            brand=(ld.get("brand") or {}).get("name"),
            color=ld.get("color"),
            category=ld.get("category"),
            condition=condition.rsplit("/", 1)[-1] if condition else None,
            available=str(offers.get("availability", "")).endswith("InStock"),
            seller_id=as_int(favourite.get("seller_id")),
            catalog_id=as_int(crumbs.get("catalog_id")),
            brand_id=as_int(crumbs.get("brand_id")),
            favourite_count=int(favourite.get("favourite_count") or 0),
            photos=photos or ([ld["image"]] if ld.get("image") else []),
            raw={"json_ld": ld, "plugins": plugins},
        )


@dataclass
class User:
    id: int
    login: str
    profile_url: str
    country: str | None
    city: str | None
    item_count: int
    followers_count: int
    following_count: int
    feedback_count: int
    feedback_reputation: float  # 0.0 – 1.0
    positive_feedback_count: int
    negative_feedback_count: int
    is_business: bool
    is_on_holiday: bool
    last_logged_on: str | None  # ISO 8601
    about: str
    photo_url: str | None
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, raw: dict) -> User:
        return cls(
            id=int(raw["id"]),
            login=raw.get("login", ""),
            profile_url=raw.get("profile_url", ""),
            country=(raw.get("country_title") or "").strip() or None,
            city=raw.get("city") or None,
            item_count=int(raw.get("item_count") or 0),
            followers_count=int(raw.get("followers_count") or 0),
            following_count=int(raw.get("following_count") or 0),
            feedback_count=int(raw.get("feedback_count") or 0),
            feedback_reputation=float(raw.get("feedback_reputation") or 0),
            positive_feedback_count=int(raw.get("positive_feedback_count") or 0),
            negative_feedback_count=int(raw.get("negative_feedback_count") or 0),
            is_business=bool(raw.get("business")),
            is_on_holiday=bool(raw.get("is_on_holiday")),
            last_logged_on=raw.get("last_loged_on_ts"),  # sic
            about=raw.get("about") or "",
            photo_url=(raw.get("photo") or {}).get("url"),
            raw=raw,
        )


@dataclass
class Feedback:
    id: int
    rating: int  # 1 – 5
    text: str
    created_at: str  # ISO 8601
    item_id: int | None
    author_id: int | None
    author_login: str | None
    is_automatic: bool
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, raw: dict) -> Feedback:
        author = raw.get("user") or {}
        return cls(
            id=int(raw["id"]),
            rating=int(raw.get("rating") or 0),
            text=raw.get("feedback") or "",
            created_at=raw.get("created_at_ts", ""),
            item_id=raw.get("item_id"),
            author_id=author.get("id"),
            author_login=author.get("login"),
            is_automatic=bool(raw.get("system_feedback")),
            raw=raw,
        )


@dataclass
class Brand:
    id: int
    title: str
    slug: str
    url: str
    item_count: int
    favourite_count: int
    is_luxury: bool
    raw: dict = field(repr=False)

    @classmethod
    def from_api(cls, raw: dict) -> Brand:
        return cls(
            id=int(raw["id"]),
            title=raw.get("title", ""),
            slug=raw.get("slug", ""),
            url=raw.get("url", ""),
            item_count=int(raw.get("item_count") or 0),
            favourite_count=int(raw.get("favourite_count") or 0),
            is_luxury=bool(raw.get("is_luxury")),
            raw=raw,
        )


@dataclass
class Color:
    id: int
    title: str
    hex: str
    code: str

    @classmethod
    def from_api(cls, raw: dict) -> Color:
        return cls(id=int(raw["id"]), title=raw.get("title", ""), hex=raw.get("hex", ""), code=raw.get("code", ""))
