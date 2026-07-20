#  Copyright (c) 2026, RTE (https://www.rte-france.com)
#  See AUTHORS.txt
#  SPDX-License-Identifier: MPL-2.0
#  This file is part of pypowsybl-mcp.

import pypowsybl as pp


def is_rte_internal_pypowsybl() -> bool:
    """
    Check if the imported pypowsybl is the RTE internal version.

    Returns:
        bool: True if the installed pypowsybl is the RTE internal version,
              False if it's the open-source version.
    """
    # RTE internal versions include "DIE" in the import formats
    return "DIE" in pp.network.get_import_formats()


def get_pypowsybl_version():
    """Retrieve the version of pypowsybl library being used"""
    return pp.__version__ + "-rte" if is_rte_internal_pypowsybl() else pp.__version__
