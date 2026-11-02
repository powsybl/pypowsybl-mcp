#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Settings for the whole test suite."""

import pytest


@pytest.fixture(autouse=True)
def no_online_enum_check(monkeypatch):
    """Tests never reach GitHub: the online enum check of the grid extension,
    on by default, is off unless a test turns it on."""
    monkeypatch.setenv("MCP_CREATION_ONLINE_ENUM_CHECK", "false")
