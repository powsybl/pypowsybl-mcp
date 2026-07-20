#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

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
