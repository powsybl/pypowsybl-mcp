#  Copyright (c) 2026, RTE (https://www.rte-france.com)
#  See AUTHORS.txt
#  SPDX-License-Identifier: MPL-2.0
#  This file is part of pypowsybl-mcp.

import os
import ssl

import pypowsybl as pp
from dotenv import load_dotenv
from loguru import logger

# Global flag to track initialization
_initialized = False

# Auto-load .env if python-dotenv is available
if not _initialized:
    if load_dotenv(override=True):
        logger.info("Loaded .env file")
    else:
        logger.warning("Failed to load .env file")

    # Global SSL certificate verification configuration
    verify_ssl_env = os.getenv("MCP_VERIFY_SSL")
    if verify_ssl_env is None:
        # Default to false if not set in .env or environment
        verify_ssl = False
    else:
        verify_ssl = verify_ssl_env.strip().lower() in {
            "true",
            "yes",
        }

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
    "VOLTAGE_MODES",
    "BALANCE_TYPES",
    "COMPONENT_MODES",
    "CONNECTED_COMPONENT_MODES",
]
