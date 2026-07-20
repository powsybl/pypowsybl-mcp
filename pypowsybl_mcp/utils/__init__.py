#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
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
