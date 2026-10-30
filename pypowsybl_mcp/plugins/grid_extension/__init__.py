#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

"""Grid extension plugin for pypowsybl-mcp: create and remove network elements.

Building or retiring equipment belongs to connection and development studies,
not to every grid study (a real-time one never needs it), so these tools ship as
a plugin rather than as built-in tools. It is a module of the server, loaded
through the entry points ``pypowsybl-mcp`` declares for it (see the root
``pyproject.toml``), so it is active by default and
``MCP_DISABLED_PLUGINS=grid_extension`` turns it off.
"""
