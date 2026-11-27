#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import copy
import json
import logging
import os
import re
import subprocess
import sys
from unittest.mock import patch

import pytest
import uvicorn.config
from loguru import logger

from pypowsybl_mcp import logging_setup
from pypowsybl_mcp.logging_setup import (
    ACCESS_LOG_FILE_NAME,
    LOG_FILE_NAME,
    setup_logging,
    std_log_level,
)


@pytest.fixture(autouse=True)
def reset_logging(monkeypatch):
    """Undo what a test configured, so tests stay independent.

    The startup buffer is stopped too: it holds what earlier tests logged.
    The standard logging and uvicorn's default configuration are restored,
    and loguru gets a default stderr output back.
    """
    for name in ("LOG_LEVEL", "LOG_FORMAT"):
        monkeypatch.delenv(name, raising=False)
    root = logging.getLogger()
    root_state = (root.handlers[:], root.level)
    uvicorn_state = {
        name: (logging.getLogger(name).handlers[:], logging.getLogger(name).propagate)
        for name in logging_setup._UVICORN_LOGGERS
    }
    uvicorn_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)

    logging_setup._stop_early_buffer("text", 0)
    yield
    logging_setup._stop_early_buffer("text", 0)

    for sink_id in (
        logging_setup._file_sink_id,
        logging_setup._access_sink_id,
        logging_setup._stderr_sink_id,
    ):
        if sink_id is not None:
            logger.remove(sink_id)
    if logging_setup._stderr_sink_id is not None:
        # Recorded as the output setup_logging replaces, as loguru's default
        # one is, so the next test does not run with two stderr outputs.
        logging_setup._early_stderr_sink_id = logger.add(sys.stderr)
    logging_setup._configured = False
    logging_setup._level = logging_setup.DEFAULT_LOG_LEVEL
    logging_setup._stderr_sink_id = None
    logging_setup._file_sink_id = None
    logging_setup._access_sink_id = None
    logging_setup._log_file = None
    logging_setup._access_log_file = None

    root.handlers[:], root.level = root_state
    for name, (handlers, propagate) in uvicorn_state.items():
        logging.getLogger(name).handlers[:] = handlers
        logging.getLogger(name).propagate = propagate
    uvicorn.config.LOGGING_CONFIG.clear()
    uvicorn.config.LOGGING_CONFIG.update(uvicorn_config)


def _read(log_file):
    """The file content, once the queued messages are written (enqueue=True)."""
    logger.complete()
    return log_file.read_text(encoding="utf-8")


def test_logs_are_written_to_log_dir(tmp_path, monkeypatch):
    log_dir = tmp_path / "nested" / "logs"
    monkeypatch.setenv("LOG_DIR", str(log_dir))

    log_file = setup_logging()
    logger.info("hello from the test")

    assert log_file == log_dir / LOG_FILE_NAME
    assert "hello from the test" in _read(log_file)


def test_lines_carry_the_utc_offset(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))

    log_file = setup_logging()
    logger.info("what time is it")

    line = next(line for line in _read(log_file).splitlines() if "what time" in line)
    assert re.match(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [+-]\d{2}:\d{2} \|", line
    )


def test_lines_logged_in_a_session_name_it(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))

    log_file = setup_logging()
    with logger.contextualize(session_id="abc-123"):
        logger.info("inside a session")
    logger.info("outside any session")

    lines = _read(log_file).splitlines()
    assert any("session=abc-123 - inside a session" in line for line in lines)
    outside = next(line for line in lines if "outside any session" in line)
    assert "session=" not in outside


def test_messages_logged_before_setup_are_written_first(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    logging_setup.start_early_buffer()
    logger.warning("logged while importing")

    log_file = setup_logging()
    logger.info("logged once started")

    lines = _read(log_file).splitlines()
    assert "logged while importing" in lines[0]
    assert any("logged once started" in line for line in lines[1:])
    # The buffer is stopped: it does not keep growing once the file exists.
    assert not logging_setup._early_sink_ids
    assert not any(logging_setup._early_messages.values())


def test_messages_logged_before_setup_follow_log_level(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_LEVEL", "warning")
    logging_setup.start_early_buffer()
    logger.info("early detail")
    logger.warning("early problem")

    content = _read(setup_logging())

    assert "early problem" in content
    assert "early detail" not in content


def test_startup_buffer_is_stopped_when_file_logging_is_disabled(monkeypatch):
    monkeypatch.setenv("LOG_DIR", "")
    logging_setup.start_early_buffer()
    logger.info("logged while importing")

    assert setup_logging() is None
    assert not logging_setup._early_sink_ids
    assert not any(logging_setup._early_messages.values())


def test_log_level_defaults_to_info(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))

    log_file = setup_logging()
    logger.debug("some detail")
    logger.info("some news")

    content = _read(log_file)
    assert "some news" in content
    assert "some detail" not in content
    stderr = capsys.readouterr().err
    assert "some news" in stderr
    assert "some detail" not in stderr


def test_log_level_applies_to_the_file_and_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_LEVEL", "debug")

    log_file = setup_logging()
    logger.trace("finest detail")
    logger.debug("some detail")

    content = _read(log_file)
    assert "some detail" in content
    assert "finest detail" not in content
    stderr = capsys.readouterr().err
    assert "some detail" in stderr
    assert "finest detail" not in stderr


def test_invalid_log_level_falls_back_to_info_with_a_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_LEVEL", "verbose")

    log_file = setup_logging()
    logger.debug("some detail")

    content = _read(log_file)
    assert "LOG_LEVEL='verbose' is not a log level; using INFO" in content
    assert "some detail" not in content


def test_json_format_writes_one_object_per_line(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_FORMAT", "JSON")
    logging_setup.start_early_buffer()
    logger.info("logged while importing")

    log_file = setup_logging()
    with logger.contextualize(session_id="abc-123"):
        logger.info("inside a session")

    entries = [json.loads(line) for line in _read(log_file).splitlines()]
    records = [entry["record"] for entry in entries]
    assert records[0]["message"] == "logged while importing"
    in_session = next(r for r in records if r["message"] == "inside a session")
    assert in_session["extra"]["session_id"] == "abc-123"
    assert in_session["level"]["name"] == "INFO"


def test_invalid_log_format_falls_back_to_text_with_a_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_FORMAT", "xml")

    content = _read(setup_logging())

    assert "LOG_FORMAT='xml' is not a log format; using text" in content


def test_standard_logging_reaches_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    # What the MCP SDK does when the server is created, before setup_logging.
    logging.basicConfig(
        handlers=[logging.StreamHandler()], level=logging.INFO, force=True
    )

    log_file = setup_logging()
    logging.getLogger("powsybl").warning("from the Java side")
    logging.getLogger("mcp.server.lowlevel").info("from the MCP SDK")
    logging.getLogger("mcp.server.lowlevel").debug("SDK detail")
    try:
        raise RuntimeError("request failed")
    except RuntimeError:
        logging.getLogger("mcp.server").exception("unhandled error")

    content = _read(log_file)
    assert "from the Java side" in content
    assert "from the MCP SDK" in content
    assert "SDK detail" not in content
    assert "unhandled error" in content
    assert "RuntimeError: request failed" in content
    # The caller is the code that logged, not the logging module.
    assert f"{__name__}:test_standard_logging_reaches_the_file" in content


def test_uvicorn_logs_reach_the_files_once_uvicorn_is_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))

    log_file = setup_logging()
    # The MCP SDK creates the uvicorn config when the server runs, after
    # setup_logging; uvicorn applies its default logging config then.
    uvicorn.config.Config(app=lambda scope, receive, send: None, log_level="info")
    logging.getLogger("uvicorn.error").error("[Errno 98] address already in use")
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "POST", "/mcp", "1.1", 200
    )
    logger.info("Serving download from temp file")

    content = _read(log_file)
    access = (tmp_path / ACCESS_LOG_FILE_NAME).read_text(encoding="utf-8")
    # Server errors stay with the server's own messages; requests go apart.
    assert "[Errno 98] address already in use" in content
    assert "Serving download from temp file" in content
    assert '"POST /mcp HTTP/1.1"' not in content
    assert '127.0.0.1:5000 - "POST /mcp HTTP/1.1" 200' in access
    assert "address already in use" not in access
    assert "Serving download" not in access
    for name in logging_setup._UVICORN_LOGGERS:
        assert logging.getLogger(name).handlers == []


def test_requests_are_logged_to_stderr_too(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))

    setup_logging()
    logging.getLogger("uvicorn.access").info('127.0.0.1:5000 - "GET /x HTTP/1.1" 200')

    assert '"GET /x HTTP/1.1" 200' in capsys.readouterr().err


def test_unwritable_access_log_keeps_the_main_log(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    # A directory where the access log should be makes opening it fail.
    (tmp_path / ACCESS_LOG_FILE_NAME).mkdir()

    log_file = setup_logging()
    logger.info("still logged")

    content = _read(log_file)
    assert log_file == tmp_path / LOG_FILE_NAME
    assert "still logged" in content
    assert "Cannot write the HTTP access log" in content
    assert logging_setup._access_sink_id is None


@pytest.mark.parametrize(
    "level, expected",
    [("TRACE", "DEBUG"), ("debug", "DEBUG"), ("SUCCESS", "INFO"), ("ERROR", "ERROR")],
)
def test_std_log_level_follows_log_level(monkeypatch, tmp_path, level, expected):
    monkeypatch.setenv("LOG_DIR", "")
    monkeypatch.setenv("LOG_LEVEL", level)

    setup_logging()

    assert std_log_level() == expected


def test_uvicorn_debug_logs_follow_log_level(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    log_file = setup_logging()
    # main() passes std_log_level() to the MCP SDK, which hands it to uvicorn.
    uvicorn.config.Config(
        app=lambda scope, receive, send: None, log_level=std_log_level().lower()
    )
    logging.getLogger("uvicorn.error").debug("ASGI detail")

    assert "ASGI detail" in _read(log_file)


def test_a_broken_standard_log_call_does_not_raise(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    setup_logging()

    # One argument for two placeholders: formatting the message fails.
    logging.getLogger("some.library").warning("%s and %s", "one")  # noqa: PLE1206

    assert "--- Logging error ---" in capsys.readouterr().err


def test_stderr_tracebacks_do_not_show_variable_values(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOG_DIR", "")
    setup_logging()

    token = "s3cr3t-t0ken"
    try:
        raise RuntimeError("bad token" if token else "no token")
    except RuntimeError:
        logger.exception("check failed")

    stderr = capsys.readouterr().err
    assert "RuntimeError: bad token" in stderr
    assert "s3cr3t-t0ken" not in stderr


def test_startup_buffer_keeps_the_first_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setattr(logging_setup, "EARLY_BUFFER_SIZE", 3)
    logging_setup.start_early_buffer()
    for i in range(5):
        logger.info(f"startup step {i}")

    content = _read(setup_logging())

    assert all(f"startup step {i}" in content for i in range(3))
    assert "startup step 3" not in content
    assert "startup step 4" not in content


def test_messages_below_log_level_do_not_fill_the_startup_buffer(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setattr(logging_setup, "EARLY_BUFFER_SIZE", 3)
    logging_setup.start_early_buffer()
    for i in range(5):
        logger.debug(f"detail {i}")
    logger.warning("the warning that matters")

    content = _read(setup_logging())

    assert "the warning that matters" in content
    assert "detail" not in content


def test_rotated_files_are_compressed_and_pruned(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setattr(logging_setup, "LOG_ROTATION", 2_000)  # bytes
    monkeypatch.setattr(logging_setup, "LOG_RETENTION", 2)

    setup_logging()
    for i in range(100):
        logger.info(f"line {i} " + "x" * 100)
    logger.complete()

    rotated = sorted(
        p.name
        for p in tmp_path.iterdir()
        if p.name.startswith("pypowsybl_mcp.") and p.name != LOG_FILE_NAME
    )
    assert rotated, "nothing was rotated"
    assert all(name.endswith(".log.gz") for name in rotated)
    assert len(rotated) <= 2


def test_relative_log_dir_is_resolved_against_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOG_DIR", "logs")

    assert setup_logging() == tmp_path / "logs" / LOG_FILE_NAME


def test_default_log_dir_is_logs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOG_DIR", raising=False)

    assert setup_logging() == tmp_path / "logs" / LOG_FILE_NAME


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_log_dir_disables_file_logging(tmp_path, monkeypatch, value):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOG_DIR", value)

    assert setup_logging() is None
    assert logging_setup._file_sink_id is None
    assert list(tmp_path.iterdir()) == []


def test_second_call_does_not_add_another_sink(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))

    log_file = setup_logging()
    sinks = (logging_setup._file_sink_id, logging_setup._stderr_sink_id)
    assert setup_logging() == log_file
    logger.info("only once")

    assert (logging_setup._file_sink_id, logging_setup._stderr_sink_id) == sinks
    assert _read(log_file).count("only once") == 1


def test_tracebacks_in_the_file_do_not_show_variable_values(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    log_file = setup_logging()

    token = "s3cr3t-t0ken"
    try:
        # The failing line names the variable: loguru's `diagnose` would print
        # its value under it.
        raise RuntimeError("bad token" if token else "no token")
    except RuntimeError:
        logger.exception("check failed")

    content = _read(log_file)
    assert "check failed" in content
    assert "RuntimeError: bad token" in content
    assert "s3cr3t-t0ken" not in content


def test_unwritable_log_dir_falls_back_to_stderr(tmp_path, monkeypatch):
    # A regular file where the directory should be makes mkdir fail.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("")
    monkeypatch.setenv("LOG_DIR", str(blocker / "logs"))

    with patch("pypowsybl_mcp.logging_setup.logger.warning") as mock_warning:
        assert setup_logging() is None

    mock_warning.assert_called_once()
    assert "stderr only" in mock_warning.call_args[0][0]
    assert logging_setup._file_sink_id is None


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs POSIX permissions that apply (root writes anywhere)",
)
def test_read_only_log_dir_falls_back_to_stderr(tmp_path, monkeypatch):
    # The directory exists, so mkdir succeeds; opening the file is what fails.
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_dir.chmod(0o555)
    monkeypatch.setenv("LOG_DIR", str(log_dir))

    try:
        with patch("pypowsybl_mcp.logging_setup.logger.warning") as mock_warning:
            assert setup_logging() is None
    finally:
        log_dir.chmod(0o755)

    mock_warning.assert_called_once()
    assert "stderr only" in mock_warning.call_args[0][0]
    assert logging_setup._file_sink_id is None
    assert list(log_dir.iterdir()) == []


@pytest.mark.parametrize("name", ["{env}", "{time}", "a}b{"])
def test_braces_in_log_dir_are_literal(tmp_path, monkeypatch, name):
    log_dir = tmp_path / name
    monkeypatch.setenv("LOG_DIR", str(log_dir))

    log_file = setup_logging()
    logger.info("in a braced directory")

    assert log_file == log_dir / LOG_FILE_NAME
    assert "in a braced directory" in _read(log_file)
    assert [p.name for p in tmp_path.iterdir()] == [name]


def test_any_file_sink_error_falls_back_to_stderr(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))

    with patch.object(logging_setup, "_add_file_sink", side_effect=KeyError("x")):
        assert setup_logging() is None

    assert logging_setup._file_sink_id is None


def test_stderr_follows_log_level_from_import(tmp_path):
    # A fresh interpreter, as the package sets stderr up when imported.
    dotenv = tmp_path / ".env"
    dotenv.write_text("LOG_LEVEL=WARNING\n")
    script = (
        "import sys, dotenv\n"
        "load = dotenv.load_dotenv\n"
        "dotenv.load_dotenv = lambda **kwargs: load(sys.argv[1], **kwargs)\n"
        "import pypowsybl_mcp\n"
        "from loguru import logger\n"
        "logger.debug('import-time detail')\n"
        "logger.warning('import-time problem')\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", script, str(dotenv)],
        capture_output=True,
        check=True,
        text=True,
        timeout=120,
    )

    assert "Loaded .env file" not in result.stderr  # INFO
    assert "import-time detail" not in result.stderr
    line = next(l for l in result.stderr.splitlines() if "import-time problem" in l)
    # Loguru colors stderr on CI (e.g. GitHub Actions) even through a pipe.
    line = re.sub(r"\x1b\[[0-9;]*m", "", line)
    assert re.match(r"\S+ \S+ [+-]\d{2}:\d{2} \| WARNING", line)
