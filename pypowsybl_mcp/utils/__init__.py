#  Copyright (c) 2026, RTE (https://www.rte-france.com)
#  See AUTHORS.txt
#  SPDX-License-Identifier: MPL-2.0
#  This file is part of pypowsybl-mcp.
import os
import socket

from pypowsybl_mcp import DEFAULT_PORT


def get_local_ip():
    """Get the primary outbound IP (most reliable).
    This works even when multiple interfaces exist."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't need to be reachable
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ip


# Download link configuration
DOWNLOAD_HOST = get_local_ip() or "localhost"

DOWNLOAD_BASE_URL = (
    f"{os.environ.get('MCP_PUBLIC_ADDRESS')}/download"
    or f"http://{DOWNLOAD_HOST}:{DEFAULT_PORT}/download"
)
DOWNLOAD_LINK_EXPIRY_SECONDS = int(
    os.environ.get("DOWNLOAD_LINK_EXPIRY_SECONDS", "3600")
)  # 1 hour
