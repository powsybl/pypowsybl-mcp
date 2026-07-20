#  Copyright (c) 2026, RTE (https://www.rte-france.com)
#  See AUTHORS.txt
#  SPDX-License-Identifier: MPL-2.0
#  This file is part of pypowsybl-mcp.

from unittest.mock import patch
from pypowsybl_mcp.utils.introspector import (
    is_rte_internal_pypowsybl,
    get_pypowsybl_version,
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
