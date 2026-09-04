#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from unittest.mock import patch

import pandas as pd
import pytest

from pypowsybl_mcp.utils.pagination import (
    DEFAULT_PAGINATION_LIMIT,
    MAX_PAGINATION_LIMIT,
    attach_pagination,
    clamp_limit,
    paginate,
    paginate_dataframe,
    paginate_dict_field,
    paginate_list,
    parse_cursor,
)


def test_clamp_limit_none_and_bounds():
    assert clamp_limit(None) is None
    assert clamp_limit(50) == 50
    assert clamp_limit(0) == 100
    assert clamp_limit(MAX_PAGINATION_LIMIT + 500) == MAX_PAGINATION_LIMIT


def test_parse_cursor():
    assert parse_cursor(None) == 0
    assert parse_cursor("") == 0
    assert parse_cursor("10") == 10
    assert parse_cursor(5) == 5
    with pytest.raises(ValueError):
        parse_cursor("abc")


def test_parse_cursor_negative_offsets_reset_to_start():
    """A negative cursor goes back to the start, it never skips data."""

    # Going one page back from the very first page.
    assert parse_cursor(-1) == 0
    # A big negative jump lands at the start too.
    assert parse_cursor("-2847") == 0


def test_paginate_list_no_limit():
    items = [1, 2, 3]
    page, meta = paginate_list(items, limit=None)
    assert page == items
    assert meta is None


def test_paginate_list_with_cursor():
    page, meta = paginate_list(list(range(10)), limit=3, cursor="4")
    assert page == [4, 5, 6]
    assert meta["total"] == 10
    assert meta["nextCursor"] == "7"
    assert meta["returned"] == 3


def test_paginate_list_last_page():
    page, meta = paginate_list(list(range(5)), limit=10, cursor="0")
    assert page == [0, 1, 2, 3, 4]
    assert meta["nextCursor"] is None


def test_paginate_list_cursor_beyond_filtered_line_ids():
    """Asking for a page past the last line gives an empty page, not an error."""

    LINE_COUNT = 847
    PAGE_LIMIT = 100
    CURSOR_BEYOND_TOTAL = str(LINE_COUNT + 153)
    line_ids = [f"FR_LINE_{i:04d}" for i in range(LINE_COUNT)]

    page, meta = paginate_list(line_ids, limit=PAGE_LIMIT, cursor=CURSOR_BEYOND_TOTAL)

    assert page == []
    assert meta == {
        "limit": PAGE_LIMIT,
        "cursor": CURSOR_BEYOND_TOTAL,
        "total": LINE_COUNT,
        "returned": 0,
        "nextCursor": None,
    }


def test_paginate_list_empty_contingency_ids():
    """An empty list still returns clean pagination info."""

    PAGE_LIMIT = 50

    page, meta = paginate_list([], limit=PAGE_LIMIT, cursor="0")

    assert page == []
    assert meta == {
        "limit": PAGE_LIMIT,
        "cursor": "0",
        "total": 0,
        "returned": 0,
        "nextCursor": None,
    }


def test_paginate_dataframe():
    df = pd.DataFrame({"x": range(5)}, index=[f"r{i}" for i in range(5)])
    page, meta = paginate_dataframe(df, limit=2, cursor="1")
    assert list(page.index) == ["r1", "r2"]
    assert meta["total"] == 5
    assert meta["nextCursor"] == "3"


def test_paginate_dataframe_cursor_beyond_generators():
    """Asking for a page past the last generator gives an empty table."""

    GENERATOR_COUNT = 150
    PAGE_LIMIT = 25
    CURSOR_BEYOND_TOTAL = str(GENERATOR_COUNT + 40)
    generators = pd.DataFrame(
        {"p": [float(i) for i in range(GENERATOR_COUNT)]},
        index=[f"GEN_{i}" for i in range(GENERATOR_COUNT)],
    )

    page, meta = paginate_dataframe(
        generators, limit=PAGE_LIMIT, cursor=CURSOR_BEYOND_TOTAL
    )

    assert page.empty
    assert list(page.columns) == ["p"]
    assert meta == {
        "limit": PAGE_LIMIT,
        "cursor": CURSOR_BEYOND_TOTAL,
        "total": GENERATOR_COUNT,
        "returned": 0,
        "nextCursor": None,
    }


def test_paginate_dataframe_empty_network_elements():
    """A network variant with no generators must paginate without crashing."""

    PAGE_LIMIT = 100
    generators = pd.DataFrame(columns=["p"])

    page, meta = paginate_dataframe(generators, limit=PAGE_LIMIT, cursor="0")

    assert page.empty
    assert meta == {
        "limit": PAGE_LIMIT,
        "cursor": "0",
        "total": 0,
        "returned": 0,
        "nextCursor": None,
    }


def test_paginate_dict_field():
    payload = {"success": True, "items": list(range(8)), "total": 8}
    out = paginate_dict_field(payload, "items", limit=3, cursor="3")
    assert out["items"] == [3, 4, 5]
    assert out["total"] == 8
    assert out["pagination"]["nextCursor"] == "6"


def test_paginate_dict_field_wrong_field_type_raises():
    """Trying to paginate a number instead of a list raises a clear error."""

    payload = {
        "success": True,
        "network_id": "ieee_14",
        "post_contingency": {"contingencies_with_violations": 3},
        "contingencies_with_violations": 3,
    }

    with pytest.raises(TypeError, match="must be a list"):
        paginate_dict_field(
            payload, "contingencies_with_violations", limit=10, cursor="0"
        )


def test_paginate_dict_field_missing_contingencies():
    """A missing field leaves the answer as-is and just adds empty pagination."""

    PAGE_LIMIT = 100
    payload = {
        "success": True,
        "network_id": "ieee_14",
        "element_type": "line",
        "filtered_count": 0,
    }

    out = paginate_dict_field(payload, "contingencies", limit=PAGE_LIMIT, cursor="0")

    assert "contingencies" not in out
    assert out["filtered_count"] == 0
    assert out["pagination"] == {
        "limit": PAGE_LIMIT,
        "cursor": "0",
        "total": 0,
        "returned": 0,
        "nextCursor": None,
    }


def test_paginate_dict_field_none_contingencies():
    """A field set to None is treated like a missing list."""

    PAGE_LIMIT = 50
    payload = {
        "success": True,
        "network_id": "ieee_14",
        "contingencies": None,
        "filtered_count": 0,
    }

    out = paginate_dict_field(payload, "contingencies", limit=PAGE_LIMIT, cursor="0")

    assert out["contingencies"] is None
    assert out["pagination"] == {
        "limit": PAGE_LIMIT,
        "cursor": "0",
        "total": 0,
        "returned": 0,
        "nextCursor": None,
    }


def test_attach_pagination():
    assert attach_pagination({"a": 1}, None) == {"a": 1}
    meta = {"limit": 10, "cursor": "0", "total": 1, "returned": 1, "nextCursor": None}
    assert attach_pagination({"a": 1}, meta)["pagination"] == meta


def test_paginate_dispatches_by_type():
    df = pd.DataFrame({"a": [1, 2]})
    page, meta = paginate(df, limit=1)
    assert list(page["a"]) == [1]
    assert meta["total"] == 2

    page, meta = paginate([1, 2, 3], limit=2)
    assert page == [1, 2]
    assert meta["total"] == 3

    out = paginate({"items": [1, 2, 3]}, limit=2, field="items")
    assert out["items"] == [1, 2]


def test_paginate_dict_without_field_raises():
    with pytest.raises(ValueError, match="field is required"):
        paginate({"items": [1, 2, 3]}, limit=2)


def test_paginate_unsupported_type_raises():
    with pytest.raises(TypeError, match="Unsupported type for pagination"):
        paginate("not-a-collection", limit=2)


def test_paginate_list_falls_back_to_default_when_clamp_returns_none():
    # clamp_limit only returns None when limit is None, which paginate_list
    # already special-cases before calling it - so this defensive fallback
    # is normally unreachable. Force it to confirm the fallback itself works.
    with patch("pypowsybl_mcp.utils.pagination.clamp_limit", return_value=None):
        page, meta = paginate_list([1, 2, 3, 4, 5], limit=2)
    assert meta["limit"] == DEFAULT_PAGINATION_LIMIT
    assert len(page) == min(5, DEFAULT_PAGINATION_LIMIT)


def test_paginate_dataframe_falls_back_to_default_when_clamp_returns_none():
    df = pd.DataFrame({"a": [1, 2, 3, 4, 5]})
    with patch("pypowsybl_mcp.utils.pagination.clamp_limit", return_value=None):
        page, meta = paginate_dataframe(df, limit=2)
    assert meta["limit"] == DEFAULT_PAGINATION_LIMIT
    assert len(page) == min(5, DEFAULT_PAGINATION_LIMIT)


def test_paginate_dict_field_falls_back_to_default_when_clamp_returns_none():
    with patch("pypowsybl_mcp.utils.pagination.clamp_limit", return_value=None):
        out = paginate_dict_field({"items": None}, "items", limit=2)
    assert out["pagination"]["limit"] == DEFAULT_PAGINATION_LIMIT
    assert out["pagination"]["total"] == 0
