#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from unittest.mock import patch

from pypowsybl_mcp.utils.introspector import (
    get_pypowsybl_version,
    is_rte_internal_pypowsybl,
)


def test_is_rte_internal_pypowsybl_true():
    with patch("pypowsybl.network.get_import_formats") as mock_get_import_formats:
        mock_get_import_formats.return_value = ["DIE", "CGMES"]
        assert is_rte_internal_pypowsybl() is True


def test_is_rte_internal_pypowsybl_false():
    with patch("pypowsybl.network.get_import_formats") as mock_get_import_formats:
        mock_get_import_formats.return_value = ["CGMES"]
        assert is_rte_internal_pypowsybl() is False


def test_get_pypowsybl_version():
    with patch("pypowsybl.__version__", "1.2.3"):
        assert get_pypowsybl_version() == "1.2.3"
