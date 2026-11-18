#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
"""File logging configured from the LOG_DIR environment variable.

Loguru writes to stderr by default; that sink is kept so `docker logs` and a
terminal still show everything. This module adds a rotated file sink so logs
survive container restarts when LOG_DIR is mounted as a volume.

The file sink is only added when the server starts, but the package logs
earlier, while it is imported (the .env loading, the TLS verification
warning, the plugin discovery). Importing this module starts keeping those
messages in memory, and `setup_file_logging` writes them at the top of the
file, so the file tells the whole startup without importing the package
writing files on its own (tests, scripts).
"""

import os
from collections import deque
from pathlib import Path

from loguru import logger

DEFAULT_LOG_DIR = "logs"
LOG_FILE_NAME = "pypowsybl_mcp.log"
LOG_ROTATION = "10 MB"
# Rotated files kept, besides the current one.
LOG_RETENTION = 10
# Startup messages kept until the file sink exists. Bounded because a process
# that never calls setup_file_logging (tests, scripts) would otherwise keep
# every message it logs.
EARLY_BUFFER_SIZE = 1000

_file_sink_id: int | None = None
_log_file: Path | None = None

_early_messages: deque[str] = deque(maxlen=EARLY_BUFFER_SIZE)


def _keep_early_message(message) -> None:
    # Keep the formatted text only, not the record (and its traceback).
    _early_messages.append(str(message))


_early_sink_id: int | None = None


def _start_early_buffer() -> None:
    """Keep the messages logged from now on until the file sink is added."""
    global _early_sink_id

    _early_sink_id = logger.add(_keep_early_message, colorize=False, diagnose=False)


_start_early_buffer()


def _stop_early_buffer() -> list[str]:
    """Stop keeping startup messages and return the ones kept so far."""
    global _early_sink_id

    if _early_sink_id is not None:
        try:
            logger.remove(_early_sink_id)
        except ValueError:
            pass  # Already removed by a global logger.remove().
        _early_sink_id = None
    messages = list(_early_messages)
    _early_messages.clear()
    return messages


def setup_file_logging() -> Path | None:
    """Add a rotated file sink in LOG_DIR and return the log file path.

    The messages logged before this call are written first. An empty LOG_DIR
    disables file logging. If the directory cannot be created or written, a
    warning is logged and the server keeps logging to stderr only: a logging
    problem must not prevent the server from starting. In both cases, None is
    returned. Calling this again does not add a second sink and returns the
    file already in use.
    """
    global _file_sink_id, _log_file

    if _file_sink_id is not None:
        return _log_file

    early_messages = _stop_early_buffer()

    log_dir = os.getenv("LOG_DIR", DEFAULT_LOG_DIR).strip()
    if not log_dir:
        logger.info("LOG_DIR is empty; file logging disabled")
        return None

    log_file = Path(log_dir).expanduser().resolve() / LOG_FILE_NAME
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        # Written before the sink exists, so they cannot trigger a rotation.
        with log_file.open("a", encoding="utf-8") as f:
            f.writelines(early_messages)
        # The server is a single process: loguru is thread-safe without
        # `enqueue`, which is only needed when several processes log.
        # `diagnose=False` keeps the values of local variables, which may hold
        # secrets, out of the tracebacks persisted on disk.
        _file_sink_id = logger.add(
            log_file,
            rotation=LOG_ROTATION,
            retention=LOG_RETENTION,
            encoding="utf-8",
            diagnose=False,
        )
    except OSError as e:
        logger.warning(f"Cannot write logs to {log_file} ({e}); logging to stderr only")
        return None

    _log_file = log_file
    logger.info(f"Writing logs to {log_file}")
    return log_file
