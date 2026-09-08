#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import uuid
from unittest.mock import MagicMock

from pypowsybl_mcp.utils.user_session_management import (
    check_session_id,
    get_session_id,
    get_session_info,
)


def test_check_session_id_new_session():
    # Mock Context and Session
    ctx = MagicMock()
    # Simulate session not having session_id attribute
    del ctx.session.session_id

    result_ctx = check_session_id(ctx)

    assert result_ctx == ctx
    assert hasattr(ctx.session, "session_id")
    assert isinstance(ctx.session.session_id, str)
    assert uuid.UUID(ctx.session.session_id).version == 4


def test_check_session_id_existing_session():
    ctx = MagicMock()
    existing_id = str(uuid.uuid4())
    ctx.session.session_id = existing_id

    result_ctx = check_session_id(ctx)

    assert result_ctx == ctx
    assert ctx.session.session_id == existing_id


def test_check_session_id_none_ctx():
    assert check_session_id(None) is None


def test_get_session_id():
    ctx = MagicMock()
    existing_id = str(uuid.uuid4())
    ctx.session.session_id = existing_id

    session_id = get_session_id(ctx)

    assert session_id == existing_id


def test_get_session_info():
    ctx = MagicMock()
    existing_id = str(uuid.uuid4())
    ctx.session.session_id = existing_id
    ctx.request_id = "req-123"
    ctx.client_id = "client-456"

    # This function just logs things and doesn't return anything
    get_session_info(ctx)

    # We can at least check if it called check_session_id (implicitly)
    assert ctx.session.session_id == existing_id


def test_get_session_info_no_ctx():
    # Should not raise exception
    get_session_info(None)


def test_generated_session_id_is_usable_as_str_cache_key():
    """Regression (issue #7, bug #5): the generated id must already be a str.

    The proxy caches are typed ``TTLCache[str, ...]`` and callers such as
    duplicate_session receive the id as text, so an id that only *renders* as
    the right string is not enough -- it has to be equal to it.
    """
    ctx = MagicMock()
    del ctx.session.session_id

    session_id = get_session_id(ctx)

    assert isinstance(session_id, str)
    proxies = {session_id: "proxy"}
    assert str(session_id) in proxies
