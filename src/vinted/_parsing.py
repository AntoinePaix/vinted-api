"""Helpers to extract data embedded in Vinted's server-rendered (Next.js) pages."""

from __future__ import annotations

import json
import re
from typing import Any

_FLIGHT_CHUNK_RE = re.compile(r"self\.__next_f\.push\((\[.*?\])\)</script>", re.DOTALL)
_JSON_LD_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL)
_PLUGIN_RE = re.compile(r'\{"data":\{"data":')

_decoder = json.JSONDecoder()


def flight_payload(page: str) -> str:
    """Concatenate the React Server Components ("flight") chunks pushed by a Next.js page."""

    parts = []
    for chunk in _FLIGHT_CHUNK_RE.findall(page):
        kind, *rest = json.loads(chunk)
        if kind == 1 and rest:
            parts.append(rest[0])
    return "".join(parts)


def find_json(payload: str, marker: str, key: str) -> Any:
    """Decode the JSON value of `key`, at the first place where `marker` appears.

    `marker` must start with `key`, e.g. key='"items":', marker='"items":{"items":' to find
    the `items` object that itself contains an `items` list.

    Raises:
        KeyError: if the marker is not present.
    """

    assert marker.startswith(key)
    start = payload.find(marker)
    if start == -1:
        raise KeyError(marker)
    value, _ = _decoder.raw_decode(payload, start + len(key))
    return value


def json_ld(page: str, type_: str) -> dict | None:
    """Return the first schema.org JSON-LD object of the given @type, if any."""

    for block in _JSON_LD_RE.findall(page):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == type_:
            return data
    return None


def plugins(payload: str) -> dict[str, dict]:
    """Return item page "plugins" (gallery, breadcrumbs, favourite, ...) keyed by name."""

    found: dict[str, dict] = {}
    for match in _PLUGIN_RE.finditer(payload):
        try:
            obj, _ = _decoder.raw_decode(payload, match.start() + len('{"data":'))
        except json.JSONDecodeError:
            continue
        name = obj.get("name") if isinstance(obj, dict) else None
        if name and name not in found and isinstance(obj.get("data"), dict):
            found[name] = obj["data"]
    return found
