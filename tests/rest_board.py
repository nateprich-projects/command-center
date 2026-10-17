"""Test fakes for the REST Project read (#2417, #2614).

The funnel reads its board from ``REST_PROJECT_ITEMS_PATH`` through
``funnel._rest_get_page``. Older fixtures describe rows the GraphQL way
(``status: {"name": ...}`` beside a GraphQL ``content``); ``rest_row`` turns
one into the live REST row shape (``fields`` entries keyed by database id
with ``value.name.raw``), and ``RestBoard`` answers paged, ``q``-filtered
item requests from those rows. Nothing here reaches GitHub.
"""

from __future__ import annotations

import pathlib
import sys
import urllib.parse
import zlib
from typing import Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


def _field_ids() -> Dict[str, Tuple[str, int]]:
    ids = funnel.REST_FIELD_DATABASE_IDS
    named = funnel.REST_NAMED_FIELD_DATABASE_IDS
    return {
        "status": ("Status", ids[funnel.STATUS_FIELD_ID]),
        "class": ("Class", ids[funnel.CLASS_FIELD_ID]),
        "needs": ("Needs", ids[funnel.NEEDS_FIELD_ID]),
        "pinned": ("Pinned", ids[funnel.PINNED_FIELD_ID]),
        "origin": ("Origin", named["Origin"]),
        "risk": ("Risk", named["Risk"]),
        "domain": ("Domain", named["Domain"]),
    }


#: GraphQL fixture aliases that name the same field as another alias.
_ALIASES = {"gate": "needs", "claim": "lock", "startable": "content"}


def _select(name: str, field_id: int, value: object) -> dict:
    raw = value.get("name") if isinstance(value, dict) else None
    entry = {"id": field_id, "name": name, "data_type": "single_select",
             "value": None}
    if isinstance(raw, str):
        entry["value"] = {"id": "opt-" + raw,
                          "name": {"raw": raw, "html": raw}}
        if isinstance(value, dict) and value.get("updatedAt"):
            entry["updated_at"] = value["updatedAt"]
    return entry


def rest_row(node: dict) -> dict:
    """One GraphQL-shaped fixture node as a live REST item row."""
    node = {_ALIASES.get(key, key): value for key, value in node.items()}
    content = dict(node.get("content") or {})
    fields = [
        _select(name, field_id, node.get(alias))
        for alias, (name, field_id) in _field_ids().items()
    ]
    lock = node.get("lock")
    fields.append({
        "id": funnel.REST_FIELD_DATABASE_IDS[funnel.LOCK_FIELD_ID],
        "name": funnel.LOCK_FIELD, "data_type": "text",
        "value": lock.get("text") if isinstance(lock, dict) else lock,
    })
    item_id = node.get("id") or "item-{}".format(content.get("number"))
    return {
        "id": zlib.crc32(str(item_id).encode()) + 1,
        "node_id": item_id,
        "content_type": "Issue",
        "content": content,
        "fields": fields,
    }


def query_of(endpoint: str) -> Optional[str]:
    """The ``q`` filter a REST items request carries, or None."""
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(endpoint).query)
    return params["q"][0] if "q" in params else None


def cursor_of(endpoint: str) -> Optional[str]:
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(endpoint).query)
    return params["after"][0] if "after" in params else None


class RestBoard:
    """Answer ``_rest_get_page`` from fixture rows.

    ``pages[q]`` is a list of ``(nodes, next_cursor)`` per filter (None for
    the unfiltered board); the page served is the one after the request's
    ``after`` cursor. A filter with no pages answers no rows. ``rows_for``
    may compute a filter's rows instead. Every endpoint is recorded in
    ``calls``.
    """

    def __init__(
        self,
        pages: Optional[Dict[Optional[str], Sequence[Tuple[list, Optional[str]]]]] = None,
        rows_for: Optional[Callable[[Optional[str]], List[dict]]] = None,
    ):
        self.pages = {key: list(value) for key, value in (pages or {}).items()}
        self.rows_for = rows_for
        self.calls: List[str] = []

    def queries(self) -> List[Optional[str]]:
        return [query_of(endpoint) for endpoint in self.calls
                if endpoint.startswith(funnel.REST_PROJECT_ITEMS_PATH)]

    def __call__(self, endpoint: str) -> dict:
        self.calls.append(endpoint)
        if not endpoint.startswith(funnel.REST_PROJECT_ITEMS_PATH):
            raise AssertionError("unexpected REST read {}".format(endpoint))
        query = query_of(endpoint)
        if self.rows_for is not None and query not in self.pages:
            return {"items": [rest_row(node) for node in self.rows_for(query)],
                    "next_cursor": None}
        pages = self.pages.get(query) or [([], None)]
        cursor = cursor_of(endpoint)
        index = 0
        if cursor is not None:
            index = [next_cursor for _nodes, next_cursor in pages].index(
                cursor) + 1
        nodes, next_cursor = pages[index]
        return {"items": [rest_row(node) for node in nodes],
                "next_cursor": next_cursor}


def install(monkeypatch, board: RestBoard) -> RestBoard:
    monkeypatch.setattr(funnel, "_rest_get_page", board)
    return board
