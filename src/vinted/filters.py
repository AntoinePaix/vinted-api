"""Search options: sort orders and the typed keyword filters accepted by `search()`."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Any
from typing import TypedDict


class Order(StrEnum):
    """How results are sorted. Plain strings work too: ``order="newest_first"``."""

    RELEVANCE = "relevance"
    NEWEST_FIRST = "newest_first"
    PRICE_LOW_TO_HIGH = "price_low_to_high"
    PRICE_HIGH_TO_LOW = "price_high_to_low"

    @classmethod
    def parse(cls, value: Order | str | None) -> Order | None:
        """Validate a sort order, with a helpful message listing the valid ones."""

        if value is None:
            return None
        try:
            return cls(value)
        except ValueError:
            valid = ", ".join(repr(order.value) for order in cls)
            raise ValueError(f"Unknown order {value!r}; use one of {valid} (or vinted.Order).") from None


class SearchFilters(TypedDict, total=False):
    """Keyword filters of `search()`, `iter_search()` and `search_page_url()`.

    Ids can be found with `search_brands()` and `get_colors()`, or in the URL of a search
    made on the website.
    """

    catalog_ids: Iterable[int]
    """Categories."""
    brand_ids: Iterable[int]
    size_ids: Iterable[int]
    status_ids: Iterable[int]
    """Conditions: 6 new with tags, 1 new without tags, 2 very good, 3 good, 4 satisfactory."""
    color_ids: Iterable[int]
    material_ids: Iterable[int]
    price_from: float
    price_to: float
    currency: str
    order: Order | str
    """Sort order, see `Order`."""
    extra: dict[str, Any]
    """Any other URL parameter, e.g. ``{"video_game_platform_ids[]": [1, 2]}``."""
