#  Copyright (c) 2026, RTE (https://www.rte-france.com)
#  See AUTHORS.txt
#  SPDX-License-Identifier: MPL-2.0
#  This file is part of pypowsybl-mcp.

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
    assert isinstance(ctx.session.session_id, uuid.UUID)


def test_check_session_id_existing_session():
    ctx = MagicMock()
    existing_id = uuid.uuid4()
    ctx.session.session_id = existing_id

    result_ctx = check_session_id(ctx)

    assert result_ctx == ctx
    assert ctx.session.session_id == existing_id


def test_check_session_id_none_ctx():
    assert check_session_id(None) is None


def test_get_session_id():
    ctx = MagicMock()
    existing_id = uuid.uuid4()
    ctx.session.session_id = existing_id

    session_id = get_session_id(ctx)

    assert session_id == existing_id


def test_get_session_info():
    ctx = MagicMock()
    existing_id = uuid.uuid4()
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
