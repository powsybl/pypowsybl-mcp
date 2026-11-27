#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0
"""Server logging, configured from the LOG_LEVEL, LOG_FORMAT and LOG_DIR
environment variables.

`setup_logging`, called when the server starts:

- replaces loguru's default stderr output with one at LOG_LEVEL, so `docker
  logs` and a terminal still show the logs;
- routes the standard `logging` module into loguru: uvicorn, the MCP SDK and
  pypowsybl (the Java logs of powsybl) log through it, not through loguru;
- adds rotated, compressed file sinks in LOG_DIR, so the logs survive
  container restarts when LOG_DIR is mounted as a volume: the HTTP request
  lines of uvicorn go to `access.log`, everything else to `pypowsybl_mcp.log`.

The package logs before the server starts, while it is imported (the .env
loading, the TLS verification warning, the plugin discovery). The package
calls `start_early_buffer` first thing, so those messages are kept in memory
and `setup_logging` writes them at the top of the file: the file tells the
startup, without importing the package writing files on its own (tests,
scripts). Only loguru messages are kept: what libraries log through the
standard `logging` module before `setup_logging` reaches stderr only.

This module only imports loguru and the standard library: the package imports
it before loading .env, so it must not import anything that reads the
environment at import time (pypowsybl_mcp.utils does).
"""

import inspect
import logging
import os
import sys
from pathlib import Path

from loguru import logger

DEFAULT_LOG_DIR = "logs"
DEFAULT_LOG_LEVEL = "INFO"
LOG_FORMATS = ("text", "json")
DEFAULT_LOG_FORMAT = "text"
LOG_FILE_NAME = "pypowsybl_mcp.log"
ACCESS_LOG_FILE_NAME = "access.log"
LOG_ROTATION = "10 MB"
# Rotated files kept, besides the current one, for each log file.
LOG_RETENTION = 10
LOG_COMPRESSION = "gz"
# Startup messages kept until the file sink exists. Bounded because a process
# that never calls setup_logging (tests, scripts) would otherwise keep every
# message it logs.
EARLY_BUFFER_SIZE = 1000

# The standard logger of uvicorn's HTTP request lines, sent to access.log.
ACCESS_LOGGER = "uvicorn.access"
# Extra field naming the standard logger a record came from (see
# InterceptHandler); loguru's own `name` is the module that logged.
STD_LOGGER_FIELD = "std_logger"

_configured = False
_level = DEFAULT_LOG_LEVEL
_stderr_sink_id: int | None = None
_file_sink_id: int | None = None
_access_sink_id: int | None = None
_log_file: Path | None = None
_access_log_file: Path | None = None


def _format(record) -> str:
    """Loguru's default layout, with the UTC offset and the MCP session.

    The session is shown when the record carries one, i.e. when it is logged
    during a tool call (see pypowsybl_mcp.utils.instrumentation). It goes
    through a `{extra[...]}` field rather than into the template itself, so
    braces in its value are not read as format fields.
    """
    session = (
        " | session={extra[session_id]}" if record["extra"].get("session_id") else ""
    )
    return (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS Z}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan>"
        + session
        + " - <level>{message}</level>\n{exception}"
    )


def _is_access(record) -> bool:
    return record["extra"].get(STD_LOGGER_FIELD) == ACCESS_LOGGER


def _is_not_access(record) -> bool:
    return not _is_access(record)


# Startup messages, as (level number, line), in both file formats: LOG_FORMAT
# is only known once .env is loaded, and the buffer starts before, to keep the
# message telling whether it was.
_early_messages: dict[str, list[tuple[int, str]]] = {
    log_format: [] for log_format in LOG_FORMATS
}
_early_sink_ids: list[int] = []


def start_early_buffer() -> None:
    """Keep the messages logged from now on until the file sink is added.

    The first EARLY_BUFFER_SIZE messages at LOG_LEVEL or above are kept: the
    start of the startup matters most. Both checks happen in the sink
    filters, which loguru runs before formatting: once the buffer is full, a
    process that never calls setup_logging (a script) pays almost nothing for
    these sinks.
    """
    if _early_sink_ids:
        return
    for log_format, messages in _early_messages.items():

        def keep(message, messages=messages) -> None:
            # Keep the level and the formatted text only, not the record (and
            # its traceback).
            messages.append((message.record["level"].no, str(message)))

        def wanted(record, messages=messages) -> bool:
            # LOG_LEVEL is read on each message: .env is loaded after the
            # buffer starts.
            return (
                len(messages) < EARLY_BUFFER_SIZE
                and record["level"].no >= logger.level(_read_level()[0]).no
            )

        _early_sink_ids.append(
            logger.add(
                keep,
                level=0,
                filter=wanted,
                format=_format,
                serialize=log_format == "json",
                colorize=False,
                diagnose=False,
            )
        )


def _stop_early_buffer(log_format: str, min_level: int) -> list[str]:
    """Stop keeping startup messages and return the ones kept so far, in
    `log_format`, from level `min_level` up."""
    for sink_id in _early_sink_ids:
        try:
            logger.remove(sink_id)
        except ValueError:
            pass  # Already removed by a global logger.remove().
    _early_sink_ids.clear()
    lines = [line for no, line in _early_messages[log_format] if no >= min_level]
    for messages in _early_messages.values():
        messages.clear()
    return lines


def _read_level() -> tuple[str, str | None]:
    """LOG_LEVEL, upper-cased, and the problem to warn about if it is invalid."""
    raw = os.getenv("LOG_LEVEL", "").strip()
    if not raw:
        return DEFAULT_LOG_LEVEL, None
    try:
        return logger.level(raw.upper()).name, None
    except ValueError:
        return DEFAULT_LOG_LEVEL, (
            f"LOG_LEVEL={raw!r} is not a log level; using {DEFAULT_LOG_LEVEL}. "
            f"Use one of TRACE, DEBUG, INFO, SUCCESS, WARNING, ERROR, CRITICAL."
        )


def _read_format() -> tuple[str, str | None]:
    """LOG_FORMAT, lower-cased, and the problem to warn about if it is invalid."""
    raw = os.getenv("LOG_FORMAT", "").strip()
    if not raw:
        return DEFAULT_LOG_FORMAT, None
    if raw.lower() in LOG_FORMATS:
        return raw.lower(), None
    return DEFAULT_LOG_FORMAT, (
        f"LOG_FORMAT={raw!r} is not a log format; using {DEFAULT_LOG_FORMAT}. "
        f"Use one of {', '.join(LOG_FORMATS)}."
    )


# Loguru levels without a standard `logging` counterpart, mapped to the
# closest standard level that lets their messages through.
_STD_LEVELS = {"TRACE": "DEBUG", "SUCCESS": "INFO"}


def std_log_level() -> str:
    """The configured LOG_LEVEL as a standard `logging` level name.

    For libraries that take a level of their own, such as the MCP SDK, which
    passes it on to uvicorn.
    """
    return _STD_LEVELS.get(_level, _level)


class InterceptHandler(logging.Handler):
    """Send the records of the standard `logging` module to loguru.

    Loguru's documented recipe: the record keeps its level, and the caller
    reported is the code that logged, not the logging module. The name of the
    standard logger is kept in the STD_LOGGER_FIELD extra field.
    """

    def emit(self, record: logging.LogRecord) -> None:
        # Like the standard handlers, never let a logging problem (e.g. a
        # message whose %-arguments do not match) reach the code that logged.
        try:
            try:
                level: str | int = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno

            frame, depth = inspect.currentframe(), 0
            while frame:
                filename = frame.f_code.co_filename
                is_logging = filename == logging.__file__
                is_frozen = "importlib" in filename and "_bootstrap" in filename
                if depth > 0 and not (is_logging or is_frozen):
                    break
                frame = frame.f_back
                depth += 1

            logger.bind(**{STD_LOGGER_FIELD: record.name}).opt(
                depth=depth, exception=record.exc_info
            ).log(level, record.getMessage())
        except Exception:  # noqa: BLE001 - reported by the standard way below
            self.handleError(record)


# The loggers uvicorn gives handlers of its own.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _route_std_logging(level_no: int) -> None:
    """Send everything logged through the standard `logging` module to loguru."""
    # `force=True`: the MCP SDK installs its own root handler when the server
    # is created. The root level is LOG_LEVEL rather than 0, so libraries skip
    # building the records loguru would drop anyway.
    logging.basicConfig(handlers=[InterceptHandler()], level=level_no, force=True)

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # Clearing the handlers above is not enough: the MCP SDK starts uvicorn
    # without a `log_config`, so uvicorn applies its default one when the
    # server runs, after this setup, and installs its handlers again. That
    # default is a module-level dict; editing it in place makes uvicorn's
    # loggers propagate to the root handler instead.
    try:
        from uvicorn.config import LOGGING_CONFIG
    except ImportError:  # pragma: no cover - uvicorn comes with the MCP SDK
        return
    for logger_config in LOGGING_CONFIG.get("loggers", {}).values():
        logger_config["handlers"] = []
        logger_config["propagate"] = True


def setup_logging() -> Path | None:
    """Configure the server logging and return the log file path.

    Applies LOG_LEVEL to the terminal output and to the files, routes the
    standard `logging` module into loguru, and adds rotated file sinks in
    LOG_DIR: `access.log` for the HTTP request lines, `pypowsybl_mcp.log` for
    the rest, where the messages logged before this call are written first.

    An empty LOG_DIR disables file logging. If the directory cannot be created
    or written, a warning is logged and the server keeps logging to stderr
    only: a logging problem must not prevent the server from starting. In both
    cases, None is returned. An invalid LOG_LEVEL or LOG_FORMAT is warned about
    and replaced by its default. Calling this again changes nothing and
    returns the file already in use.
    """
    global _configured, _level, _stderr_sink_id, _log_file

    if _configured:
        return _log_file
    _configured = True

    _level, level_problem = _read_level()
    log_format, format_problem = _read_format()
    level_no = logger.level(_level).no

    # Loguru's default output (handler 0) logs everything from DEBUG up.
    try:
        logger.remove(0)
    except ValueError:
        pass  # Already removed.
    # `diagnose=False`, as for the files: `docker logs` keeps stderr on disk,
    # and the values of local variables in tracebacks may hold secrets.
    _stderr_sink_id = logger.add(
        sys.stderr, level=_level, format=_format, diagnose=False
    )

    _route_std_logging(level_no)

    early_messages = _stop_early_buffer(log_format, level_no)
    _log_file = _add_file_sinks(early_messages, _level, log_format)

    # Logged once the file exists, so they are kept in it.
    for problem in (level_problem, format_problem):
        if problem:
            logger.warning(problem)
    return _log_file


def _add_file_sink(path: Path, level: str, log_format: str, record_filter) -> int:
    """Add a rotated file sink at `path`; raise OSError if it cannot be opened."""
    # `enqueue=True` hands the messages to a worker thread, which formats and
    # writes them and also rotates the file (renaming, compressing, deleting
    # the oldest files). The calling thread, often the asyncio event loop,
    # never waits on the disk. The queue is flushed when the server stops (see
    # main()).
    # `diagnose=False` keeps the values of local variables, which may hold
    # secrets, out of the tracebacks persisted on disk.
    return logger.add(
        path,
        level=level,
        filter=record_filter,
        format=_format,
        serialize=log_format == "json",
        rotation=LOG_ROTATION,
        retention=LOG_RETENTION,
        compression=LOG_COMPRESSION,
        encoding="utf-8",
        enqueue=True,
        diagnose=False,
    )


def _add_file_sinks(
    early_messages: list[str], level: str, log_format: str
) -> Path | None:
    """Add the file sinks in LOG_DIR, after writing `early_messages` to the
    main log file, and return its path."""
    global _file_sink_id, _access_sink_id, _access_log_file

    log_dir = os.getenv("LOG_DIR", DEFAULT_LOG_DIR).strip()
    if not log_dir:
        logger.info("LOG_DIR is empty; file logging disabled")
        return None

    directory = Path(log_dir).expanduser().resolve()
    log_file = directory / LOG_FILE_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Written before the sink exists, so they cannot trigger a rotation.
        with log_file.open("a", encoding="utf-8") as f:
            f.writelines(early_messages)
        _file_sink_id = _add_file_sink(log_file, level, log_format, _is_not_access)
    except OSError as e:
        logger.warning(f"Cannot write logs to {log_file} ({e}); logging to stderr only")
        return None
    logger.info(f"Writing logs to {log_file}")

    access_log_file = directory / ACCESS_LOG_FILE_NAME
    try:
        _access_sink_id = _add_file_sink(access_log_file, level, log_format, _is_access)
    except OSError as e:
        logger.warning(
            f"Cannot write the HTTP access log to {access_log_file} ({e}); "
            f"requests are logged to stderr only"
        )
    else:
        _access_log_file = access_log_file
        logger.info(f"Writing HTTP request lines to {access_log_file}")
    return log_file
