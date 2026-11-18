#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
"""File logging configured from the LOG_DIR environment variable.

Loguru writes to stderr by default; that sink is kept so `docker logs` and a
terminal still show everything. This module adds a rotated file sink so logs
survive container restarts when LOG_DIR is mounted as a volume.
"""

import os
from pathlib import Path

from loguru import logger

DEFAULT_LOG_DIR = "logs"
LOG_FILE_NAME = "pypowsybl_mcp.log"
LOG_ROTATION = "10 MB"
LOG_RETENTION = 10

_file_sink_id: int | None = None


def setup_file_logging() -> Path | None:
    """Add a rotated file sink in LOG_DIR and return the log file path.

    An empty LOG_DIR disables file logging. If the directory cannot be created
    or written, a warning is logged and the server keeps logging to stderr only:
    a logging problem must not prevent the server from starting.
    Calling this more than once does not add a second sink.
    """
    global _file_sink_id

    if _file_sink_id is not None:
        return None

    log_dir = os.getenv("LOG_DIR", DEFAULT_LOG_DIR).strip()
    if not log_dir:
        logger.info("LOG_DIR is empty; file logging disabled")
        return None

    log_file = Path(log_dir).expanduser().resolve() / LOG_FILE_NAME
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        _file_sink_id = logger.add(
            log_file,
            rotation=LOG_ROTATION,
            retention=LOG_RETENTION,
            encoding="utf-8",
            enqueue=True,
        )
    except OSError as e:
        logger.warning(f"Cannot write logs to {log_file} ({e}); logging to stderr only")
        return None

    logger.info(f"Writing logs to {log_file}")
    return log_file
