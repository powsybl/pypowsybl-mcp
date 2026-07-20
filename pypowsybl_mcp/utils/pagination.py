#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import os

import pandas as pd

# Page size defaults can be overridden via environment variables see .env.template
DEFAULT_PAGINATION_LIMIT = int(os.getenv("DEFAULT_PAGINATION_LIMIT", "100"))
MAX_PAGINATION_LIMIT = int(os.getenv("MAX_PAGINATION_LIMIT", "1000"))


def clamp_limit(limit: int | None) -> int | None:
    """Turn a requested page size into something we can actually use.
    If limit is None, pagination stays off and we return None. Zero or negative
    values fall back to DEFAULT_PAGINATION_LIMIT. Anything above MAX_PAGINATION_LIMIT
    gets capped at that ceiling.
    """
    if limit is None:
        return None

    if limit <= 0:
        return DEFAULT_PAGINATION_LIMIT

    if limit > MAX_PAGINATION_LIMIT:
        return MAX_PAGINATION_LIMIT

    return limit


def parse_cursor(cursor: int | str | None) -> int:
    """Read the cursor the client sent and turn it into a row offset.
    None or an empty string means "start from the beginning". Negative values
    are treated as zero. Integers and numeric strings both work; anything else
    raises ValueError so the caller knows the cursor was wrong.
    """
    if cursor is None or cursor == "":
        return 0

    if isinstance(cursor, int):
        if cursor < 0:
            return 0
        return cursor

    cursor_text = str(cursor).strip()
    try:
        offset = int(cursor_text)
    except ValueError:
        # Raise a clear error so the caller knows the cursor was wrong.
        raise ValueError("Invalid cursor: " + repr(cursor))

    if offset < 0:
        return 0
    return offset


def pagination_meta(
    total: int, limit: int, offset: int, returned: int
) -> dict[str, int | str | None]:
    """Build the pagination block that goes back to the client.
    Tells the client how many items exist in total, how many we just returned, and
    which cursor to pass on the next call — or None when there is no next page.
    """
    next_offset = offset + returned

    # If we already reached or passed the end, there is no next page.
    if next_offset < total:
        next_cursor = str(next_offset)
    else:
        next_cursor = None

    meta = {
        "limit": limit,
        "cursor": str(offset),
        "total": total,
        "returned": returned,
        "nextCursor": next_cursor,
    }
    return meta


def paginate_list(items, limit=None, cursor=None):
    """Cut a list into a single page.
    Returns a tuple (page, meta). When no limit is asked, we return the whole
    list and meta is None.
    """
    # If no limit, caller wants everything, we do not paginate.
    if limit is None:
        return list(items), None

    offset = parse_cursor(cursor)
    page_size = clamp_limit(limit)
    if page_size is None:
        page_size = DEFAULT_PAGINATION_LIMIT

    total = len(items)
    # Slicing the list to get the page. For empty lists, it will return an empty list.
    start = offset
    end = offset + page_size
    page = list(items[start:end])

    meta = pagination_meta(
        total=total,
        limit=page_size,
        offset=offset,
        returned=len(page),
    )
    return page, meta


def paginate_dataframe(df, limit=None, cursor=None):
    """Same idea as paginate_list, but for a pandas DataFrame.
    We cut the rows with iloc so the row order stays exactly the same.
    Returns page_dataframe and meta, or df and None when there is no limit.
    """
    if limit is None:
        return df, None

    offset = parse_cursor(cursor)
    page_size = clamp_limit(limit)
    if page_size is None:
        page_size = DEFAULT_PAGINATION_LIMIT

    total = len(df)

    start = offset
    end = offset + page_size
    page = df.iloc[start:end]

    meta = pagination_meta(
        total=total,
        limit=page_size,
        offset=offset,
        returned=len(page),
    )
    return page, meta


def paginate_dict_field(payload, field, limit=None, cursor=None):
    """Paginate one list that lives inside a result dict.
    Example: the result has a "contingencies" list and some totals next to it.
    We only cut the list and we keep every other field untouched. The page info
    is added under payload["pagination"].
    """

    # If no limit return the payload as it is.
    if limit is None:
        return payload

    value = payload.get(field)

    # If the field is missing or empty, we still answer with an empty page so the response shape stays the same for the client.
    if value is None:
        page_size = clamp_limit(limit)
        if page_size is None:
            page_size = DEFAULT_PAGINATION_LIMIT

        out = dict(payload)
        out["pagination"] = pagination_meta(
            total=0,
            limit=page_size,
            offset=0,
            returned=0,
        )
        return out

    # here we only know how to paginate a list. If it is something else, it is a programming mistake on the caller side so we tell them.
    if not isinstance(value, list):
        raise TypeError(
            "Field " + repr(field) + " must be a list, got " + type(value).__name__
        )

    page, meta = paginate_list(value, limit, cursor)

    # Copy the payload first so we never mutate the dict we were given.

    out = dict(payload)
    out[field] = page
    out["pagination"] = meta
    return out


def paginate(
    value: pd.DataFrame | list | dict,
    limit: int | None = None,
    cursor: int | str | None = None,
    field: str | None = None,
):
    """Single entry point for pagination — picks the right helper based on value type.

    Pass a DataFrame, a list, or a dict (with field set to the list key inside it).
    For dict payloads, field is required. Anything else raises TypeError.
    """
    if isinstance(value, pd.DataFrame):
        return paginate_dataframe(value, limit=limit, cursor=cursor)

    if isinstance(value, list):
        return paginate_list(value, limit=limit, cursor=cursor)

    if isinstance(value, dict):
        if field is None:
            raise ValueError("field is required when paginating a dict payload")
        return paginate_dict_field(value, field, limit=limit, cursor=cursor)

    raise TypeError(f"Unsupported type for pagination: {type(value).__name__}")


def attach_pagination(payload, meta):
    """Add the pagination dict to a payload, but only when there is one.
    When meta is None so no pagination happened we return the payload as it is.
    """
    if meta is None:
        return payload

    out = dict(payload)
    out["pagination"] = meta
    return out
