#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import os
import ssl

import pypowsybl as pp
from dotenv import load_dotenv
from loguru import logger

# Global flag to track initialization
_initialized = False

# Accepted spellings for MCP_VERIFY_SSL. Anything outside both sets is treated
# as disabled, but warned about explicitly (see _parse_verify_ssl).
_TRUTHY_VALUES = {"true", "yes", "1", "on", "enabled", "y", "t"}
_FALSY_VALUES = {"false", "no", "0", "off", "disabled", "n", "f"}


def _parse_verify_ssl(raw: str | None) -> bool:
    """Parse the MCP_VERIFY_SSL environment variable.

    Unset means disabled (the documented default). An unrecognized value also
    means disabled, but is warned about explicitly: it most likely means the
    operator meant to *enable* verification, and silently disabling TLS checks
    is the one direction this must never fail in quietly.
    """
    if raw is None:
        return False

    value = raw.strip().lower()
    if value in _TRUTHY_VALUES:
        return True
    if value not in _FALSY_VALUES:
        logger.warning(
            f"MCP_VERIFY_SSL={raw!r} is not a recognized boolean value; "
            f"falling back to DISABLED TLS certificate verification. "
            f"Use one of {sorted(_TRUTHY_VALUES)} to enable it."
        )
    return False


# Auto-load .env if python-dotenv is available
if not _initialized:
    if load_dotenv(override=True):
        logger.info("Loaded .env file")
    else:
        logger.warning("Failed to load .env file")

    # Global SSL certificate verification configuration
    verify_ssl = _parse_verify_ssl(os.getenv("MCP_VERIFY_SSL"))

    if not verify_ssl:
        logger.warning(
            f"SSL certificate verification is disabled globally (MCP_VERIFY_SSL={os.getenv('MCP_VERIFY_SSL', 'false')})"
        )
        # Globally disable SSL verification for urllib/requests
        try:
            ssl._create_default_https_context = ssl._create_unverified_context
        except AttributeError:
            # Handle systems where this might not be available
            pass

    _initialized = True

DEFAULT_PORT = 9992

VOLTAGE_MODES = {
    "UNIFORM_VALUES": pp.loadflow.VoltageInitMode.UNIFORM_VALUES,
    "PREVIOUS_VALUES": pp.loadflow.VoltageInitMode.PREVIOUS_VALUES,
    "DC_VALUES": pp.loadflow.VoltageInitMode.DC_VALUES,
}
BALANCE_TYPES = {
    "PROPORTIONAL_TO_LOAD": pp.loadflow.BalanceType.PROPORTIONAL_TO_LOAD,
    "PROPORTIONAL_TO_GENERATION_P": pp.loadflow.BalanceType.PROPORTIONAL_TO_GENERATION_P,
    "PROPORTIONAL_TO_GENERATION_P_MAX": pp.loadflow.BalanceType.PROPORTIONAL_TO_GENERATION_P_MAX,
    "PROPORTIONAL_TO_GENERATION_PARTICIPATION_FACTOR": pp.loadflow.BalanceType.PROPORTIONAL_TO_GENERATION_PARTICIPATION_FACTOR,
    "PROPORTIONAL_TO_GENERATION_REMAINING_MARGIN": pp.loadflow.BalanceType.PROPORTIONAL_TO_GENERATION_REMAINING_MARGIN,
    "PROPORTIONAL_TO_CONFORM_LOAD": pp.loadflow.BalanceType.PROPORTIONAL_TO_CONFORM_LOAD,
}
COMPONENT_MODES = {
    "MAIN_SYNCHRONOUS": pp.loadflow.ComponentMode.MAIN_SYNCHRONOUS,
    "MAIN_CONNECTED": pp.loadflow.ComponentMode.MAIN_CONNECTED,
    "ALL_CONNECTED": pp.loadflow.ComponentMode.ALL_CONNECTED,
}
CONNECTED_COMPONENT_MODES = {
    "MAIN": pp.loadflow.ConnectedComponentMode.MAIN,
    "ALL": pp.loadflow.ConnectedComponentMode.ALL,
}

__all__ = [
    "BALANCE_TYPES",
    "COMPONENT_MODES",
    "CONNECTED_COMPONENT_MODES",
    "VOLTAGE_MODES",
]
