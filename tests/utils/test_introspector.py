#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from unittest.mock import patch

from pypowsybl_mcp.utils.introspector import (
    get_pypowsybl_version,
)


def test_get_pypowsybl_version():
    with patch("pypowsybl.__version__", "1.2.3"):
        assert get_pypowsybl_version() == "1.2.3"
