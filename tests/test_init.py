#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

from unittest.mock import patch

import pytest

from pypowsybl_mcp import _FALSY_VALUES, _TRUTHY_VALUES, _parse_verify_ssl


@pytest.mark.parametrize(
    "raw",
    [
        "true",
        "yes",
        # Regression for bug #6: these conventional truthy spellings used to be
        # parsed as "disabled", silently leaving TLS verification off for an
        # operator who explicitly asked for it.
        "1",
        "on",
        "enabled",
        "y",
        "t",
        # Case and surrounding whitespace must not matter either.
        "TRUE",
        "Yes",
        "  1  ",
        "ON",
    ],
)
def test_truthy_values_enable_verification(raw):
    with patch("pypowsybl_mcp.logger.warning") as mock_warning:
        assert _parse_verify_ssl(raw) is True
        mock_warning.assert_not_called()


@pytest.mark.parametrize(
    "raw",
    ["false", "no", "0", "off", "disabled", "n", "f", "FALSE", "  off  "],
)
def test_falsy_values_disable_verification_without_warning(raw):
    # Disabling on purpose is a documented, legitimate choice (self-signed
    # certificates on internal hosts), so it must not be warned about here.
    with patch("pypowsybl_mcp.logger.warning") as mock_warning:
        assert _parse_verify_ssl(raw) is False
        mock_warning.assert_not_called()


def test_unset_disables_verification_without_warning():
    # Unset is the documented default (MCP_VERIFY_SSL=false in .env.template).
    with patch("pypowsybl_mcp.logger.warning") as mock_warning:
        assert _parse_verify_ssl(None) is False
        mock_warning.assert_not_called()


@pytest.mark.parametrize("raw", ["ture", "enabl", "maybe", "", "  ", "2", "oui"])
def test_unrecognized_values_disable_verification_but_warn(raw):
    """Regression for bug #6: an unrecognized value is indistinguishable from a
    deliberate "false" unless we say so. It most likely means the operator meant
    to enable verification, so it must be reported explicitly rather than
    silently downgrading TLS.
    """
    with patch("pypowsybl_mcp.logger.warning") as mock_warning:
        assert _parse_verify_ssl(raw) is False

        mock_warning.assert_called_once()
        message = mock_warning.call_args[0][0]
        assert repr(raw) in message
        assert "not a recognized boolean value" in message
        assert "DISABLED" in message


def test_truthy_and_falsy_sets_do_not_overlap():
    assert not _TRUTHY_VALUES & _FALSY_VALUES
