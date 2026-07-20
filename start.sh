#!/bin/bash
#
# Copyright (c) 2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# SPDX-License-Identifier: MPL-2.0
# This file is part of pypowsybl-mcp.
#

echo
echo "This script starts the MCP server"
echo

# Use Python to load .env file and export variables
eval $(eval $VIRTUAL_ENV/bin/python << 'EOF'
from dotenv import dotenv_values
from dotenv import find_dotenv

# Find .env file starting from current directory
dotenv_path = find_dotenv(usecwd=True)

if dotenv_path:
    # Load only the variables from .env file (not all environment)
    env_vars = dotenv_values(dotenv_path)
    # Export only the variables from .env
    for key, value in env_vars.items():
        if value is not None:  # Skip empty values
            # Escape special characters in the value
            value_escaped = value.replace('"', '\\"')
            print(f'export {key}="{value_escaped}"')
else:
    import sys
    print('echo "Warning: .env file not found"', file=sys.stderr)
EOF
)

mkdir -p $LOG_DIR
export MCP_HOME=$(python -c "import os; import pypowsybl_mcp; print(os.path.dirname(os.path.dirname(pypowsybl_mcp.__file__)))")
supervisord -c supervisord.conf
