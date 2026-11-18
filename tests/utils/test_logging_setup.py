#  Copyright (c) 2026, RTE (http://www.rte-france.com)
#  This Source Code Form is subject to the terms of the Mozilla Public
#  License, v. 2.0. If a copy of the MPL was not distributed with this
#  file, You can obtain one at http://mozilla.org/MPL/2.0/.
#  SPDX-License-Identifier: MPL-2.0

import os
from unittest.mock import patch

import pytest
from loguru import logger

from pypowsybl_mcp.utils import logging_setup
from pypowsybl_mcp.utils.logging_setup import LOG_FILE_NAME, setup_file_logging


@pytest.fixture(autouse=True)
def reset_file_sink():
    """Remove the sinks a test added so tests stay independent.

    The startup buffer is stopped too: it holds what earlier tests logged.
    """
    logging_setup._stop_early_buffer()
    logging_setup._file_sink_id = None
    logging_setup._log_file = None
    yield
    logging_setup._stop_early_buffer()
    if logging_setup._file_sink_id is not None:
        logger.remove(logging_setup._file_sink_id)
    logging_setup._file_sink_id = None
    logging_setup._log_file = None


def test_logs_are_written_to_log_dir(tmp_path, monkeypatch):
    log_dir = tmp_path / "nested" / "logs"
    monkeypatch.setenv("LOG_DIR", str(log_dir))

    log_file = setup_file_logging()
    logger.info("hello from the test")

    assert log_file == log_dir / LOG_FILE_NAME
    assert "hello from the test" in log_file.read_text(encoding="utf-8")


def test_messages_logged_before_setup_are_written_first(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    logging_setup._start_early_buffer()
    logger.warning("logged while importing")

    log_file = setup_file_logging()
    logger.info("logged once started")

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert "logged while importing" in lines[0]
    assert any("logged once started" in line for line in lines[1:])
    # The buffer is stopped: it does not keep growing once the file exists.
    assert logging_setup._early_sink_id is None
    assert not logging_setup._early_messages


def test_startup_buffer_is_stopped_when_file_logging_is_disabled(monkeypatch):
    monkeypatch.setenv("LOG_DIR", "")
    logging_setup._start_early_buffer()
    logger.info("logged while importing")

    assert setup_file_logging() is None
    assert logging_setup._early_sink_id is None
    assert not logging_setup._early_messages


def test_relative_log_dir_is_resolved_against_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOG_DIR", "logs")

    assert setup_file_logging() == tmp_path / "logs" / LOG_FILE_NAME


def test_default_log_dir_is_logs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOG_DIR", raising=False)

    assert setup_file_logging() == tmp_path / "logs" / LOG_FILE_NAME


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_log_dir_disables_file_logging(tmp_path, monkeypatch, value):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOG_DIR", value)

    assert setup_file_logging() is None
    assert logging_setup._file_sink_id is None
    assert list(tmp_path.iterdir()) == []


def test_second_call_does_not_add_another_sink(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))

    log_file = setup_file_logging()
    first_sink = logging_setup._file_sink_id
    assert setup_file_logging() == log_file
    logger.info("only once")

    assert logging_setup._file_sink_id == first_sink
    content = (tmp_path / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert content.count("only once") == 1


def test_tracebacks_in_the_file_do_not_show_variable_values(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    log_file = setup_file_logging()

    token = "s3cr3t-t0ken"
    try:
        # The failing line names the variable: loguru's `diagnose` would print
        # its value under it.
        raise RuntimeError("bad token" if token else "no token")
    except RuntimeError:
        logger.exception("check failed")

    content = log_file.read_text(encoding="utf-8")
    assert "check failed" in content
    assert "RuntimeError: bad token" in content
    assert "s3cr3t-t0ken" not in content


def test_unwritable_log_dir_falls_back_to_stderr(tmp_path, monkeypatch):
    # A regular file where the directory should be makes mkdir fail.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("")
    monkeypatch.setenv("LOG_DIR", str(blocker / "logs"))

    with patch("pypowsybl_mcp.utils.logging_setup.logger.warning") as mock_warning:
        assert setup_file_logging() is None

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
        with patch("pypowsybl_mcp.utils.logging_setup.logger.warning") as mock_warning:
            assert setup_file_logging() is None
    finally:
        log_dir.chmod(0o755)

    mock_warning.assert_called_once()
    assert "stderr only" in mock_warning.call_args[0][0]
    assert logging_setup._file_sink_id is None
    assert list(log_dir.iterdir()) == []
